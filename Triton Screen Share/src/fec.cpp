#include "fec.h"

#include "common.h"  // NowUs
#include "secure.h"

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <vector>

namespace lfrd {
namespace {

using Bytes = std::vector<uint8_t>;

constexpr uint8_t kParityMagic = 0xF5;  // first byte of a parity packet; RTP v2 has 0x80..0xBF there
constexpr size_t kParityHeader = 12;    // magic, row, k, m, SSRC, first sequence number, shard length
constexpr int kMaxBlock = 100;          // data packets per parity block
constexpr int kParityPercent = 12;      // Sunshine defaults to 20%; 12% covers typical Wi-Fi bursts
constexpr uint64_t kHoldUs = 40'000;    // longest a frame waits for its parity

std::atomic<uint64_t> g_recovered{0};

// GF(2^8) with the polynomial 0x11d.
struct Gf {
  uint8_t exp[512];
  uint8_t log[256];
  Gf() {
    int x = 1;
    for (int i = 0; i < 255; ++i) {
      exp[i] = static_cast<uint8_t>(x);
      log[x] = static_cast<uint8_t>(i);
      x <<= 1;
      if (x & 0x100) x ^= 0x11d;
    }
    for (int i = 255; i < 512; ++i) exp[i] = exp[i - 255];
    log[0] = 0;
  }
  uint8_t Mul(uint8_t a, uint8_t b) const { return a && b ? exp[log[a] + log[b]] : 0; }
  uint8_t Inv(uint8_t a) const { return exp[255 - log[a]]; }
};
const Gf& G() {
  static const Gf g;
  return g;
}

// Systematic Cauchy Reed-Solomon: parity row j weights data shard i by 1 / (j XOR (128 + i)). With
// the identity rows of the data that is an MDS code: any k of a block's k + m shards rebuild it
// (j < 128 <= 128 + i, so the divisor is never 0).
uint8_t Coef(int j, int i) { return G().Inv(static_cast<uint8_t>(j ^ (128 + i))); }

// dst ^= c * src, or p *= c, over n bytes, through a table for c.
void MulAdd(uint8_t* dst, const uint8_t* src, size_t n, uint8_t c) {
  if (!c) return;
  uint8_t t[256];
  for (int x = 0; x < 256; ++x) t[x] = G().Mul(c, static_cast<uint8_t>(x));
  for (size_t b = 0; b < n; ++b) dst[b] ^= t[src[b]];
}
void Scale(uint8_t* p, size_t n, uint8_t c) {
  uint8_t t[256];
  for (int x = 0; x < 256; ++x) t[x] = G().Mul(c, static_cast<uint8_t>(x));
  for (size_t b = 0; b < n; ++b) p[b] = t[p[b]];
}

uint16_t Be16(const uint8_t* p) { return static_cast<uint16_t>((p[0] << 8) | p[1]); }
uint32_t Be32(const uint8_t* p) {
  return (static_cast<uint32_t>(p[0]) << 24) | (static_cast<uint32_t>(p[1]) << 16) | (p[2] << 8) | p[3];
}
void Put16(uint8_t* p, size_t v) {
  p[0] = static_cast<uint8_t>(v >> 8);
  p[1] = static_cast<uint8_t>(v);
}
void Put32(uint8_t* p, uint32_t v) {
  for (int i = 0; i < 4; ++i) p[i] = static_cast<uint8_t>(v >> (24 - 8 * i));
}

// A packet as a shard: [length][packet][zeros up to shardLen].
void ToShard(const Bytes& packet, size_t shardLen, uint8_t* out) {
  std::memset(out, 0, shardLen);
  Put16(out, packet.size());
  std::memcpy(out + 2, packet.data(), packet.size());
}

Bytes ReadAll(GstBuffer* b) {
  Bytes v(gst_buffer_get_size(b));
  gst_buffer_extract(b, 0, v.data(), v.size());
  return v;
}

GstBuffer* NewBuffer(const uint8_t* data, size_t n, GstBuffer* like) {
  GstBuffer* b = gst_buffer_new_allocate(nullptr, n, nullptr);
  gst_buffer_fill(b, 0, data, n);
  if (like) gst_buffer_copy_into(b, like, GST_BUFFER_COPY_METADATA, 0, static_cast<gsize>(-1));
  return b;
}

// ---------------------------------------------------------------------------
// tritonfecenc

struct Encoder {
  std::unique_ptr<RtpCipher> cipher;
  std::vector<Bytes> block;  // encrypted packets of the block being collected
  uint32_t ssrc = 0;
  uint16_t firstSeq = 0;
};

struct TritonFecEnc {
  GstElement parent;
  GstPad* sink;
  GstPad* src;
  Encoder* st;
};
struct TritonFecEncClass {
  GstElementClass parent_class;
};
G_DEFINE_TYPE(TritonFecEnc, triton_fec_enc, GST_TYPE_ELEMENT)

GstStaticPadTemplate g_encSink = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);
GstStaticPadTemplate g_encSrc = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

void EmitParity(TritonFecEnc* self, GstBuffer* like) {
  Encoder& e = *self->st;
  const int k = static_cast<int>(e.block.size());
  const int m = std::max(1, (k * kParityPercent + 99) / 100);
  size_t shardLen = 0;
  for (const Bytes& p : e.block) shardLen = std::max(shardLen, p.size() + 2);
  std::vector<Bytes> shards(k, Bytes(shardLen));
  for (int i = 0; i < k; ++i) ToShard(e.block[i], shardLen, shards[i].data());
  for (int j = 0; j < m; ++j) {
    Bytes pkt(kParityHeader + shardLen, 0);
    pkt[0] = kParityMagic;
    pkt[1] = static_cast<uint8_t>(j);
    pkt[2] = static_cast<uint8_t>(k);
    pkt[3] = static_cast<uint8_t>(m);
    Put32(&pkt[4], e.ssrc);
    Put16(&pkt[8], e.firstSeq);
    Put16(&pkt[10], shardLen);
    for (int i = 0; i < k; ++i) MulAdd(&pkt[kParityHeader], shards[i].data(), shardLen, Coef(j, i));
    gst_pad_push(self->src, NewBuffer(pkt.data(), pkt.size(), like));
  }
  e.block.clear();
}

GstFlowReturn EncChain(GstPad*, GstObject* parent, GstBuffer* in) {
  auto* self = reinterpret_cast<TritonFecEnc*>(parent);
  Encoder& e = *self->st;
  const Bytes plain = ReadAll(in);
  const size_t header = RtpHeaderLength(plain.data(), plain.size());
  Bytes sealed(plain.size() + 16);
  // Nothing ever leaves unencrypted: without a key (or for a non-RTP buffer) it is dropped.
  if (!header || !e.cipher || !e.cipher->Seal(plain.data(), plain.size(), sealed.data())) {
    gst_buffer_unref(in);
    return GST_FLOW_OK;
  }
  const uint16_t seq = Be16(&plain[2]);
  const uint32_t ssrc = Be32(&plain[8]);
  const bool marker = (plain[1] & 0x80) != 0;  // a frame's last packet
  if (!e.block.empty() && (ssrc != e.ssrc || seq != static_cast<uint16_t>(e.firstSeq + e.block.size()))) {
    e.block.clear();  // the video restarted: that block can't be completed
  }
  if (e.block.empty()) {
    e.ssrc = ssrc;
    e.firstSeq = seq;
  }
  e.block.push_back(sealed);
  const GstFlowReturn ret = gst_pad_push(self->src, NewBuffer(sealed.data(), sealed.size(), in));
  if (marker || static_cast<int>(e.block.size()) >= kMaxBlock) EmitParity(self, in);
  gst_buffer_unref(in);
  return ret;
}

void EncFinalize(GObject* obj) {
  delete reinterpret_cast<TritonFecEnc*>(obj)->st;
  G_OBJECT_CLASS(triton_fec_enc_parent_class)->finalize(obj);
}

void triton_fec_enc_class_init(TritonFecEncClass* klass) {
  auto* ec = GST_ELEMENT_CLASS(klass);
  gst_element_class_set_static_metadata(ec, "Triton FEC encoder", "Codec/Network",
                                        "Encrypts RTP packets and adds Reed-Solomon parity per frame", "Triton");
  gst_element_class_add_static_pad_template(ec, &g_encSink);
  gst_element_class_add_static_pad_template(ec, &g_encSrc);
  G_OBJECT_CLASS(klass)->finalize = EncFinalize;
}

void triton_fec_enc_init(TritonFecEnc* self) {
  self->st = new Encoder();
  self->sink = gst_pad_new_from_static_template(&g_encSink, "sink");
  self->src = gst_pad_new_from_static_template(&g_encSrc, "src");
  gst_pad_set_chain_function(self->sink, EncChain);
  for (GstPad* p : {self->sink, self->src}) {
    GST_PAD_SET_PROXY_CAPS(p);
    GST_PAD_SET_PROXY_ALLOCATION(p);
    gst_element_add_pad(GST_ELEMENT(self), p);
  }
}

// ---------------------------------------------------------------------------
// tritonfecdec

struct ParityBlock {
  uint32_t ssrc;
  uint16_t firstSeq;
  int k, m;
  size_t shardLen;
  std::map<int, Bytes> rows;  // parity row -> shard
};

struct Decoder {
  std::unique_ptr<RtpCipher> cipher;
  bool started = false, holding = false;
  uint32_t ssrc = 0;
  uint16_t expected = 0;  // the next sequence number to pass on
  uint16_t holdBase = 0;  // held packets are keyed by their distance from this
  uint64_t holdSince = 0;
  std::map<int, GstBuffer*> held;
  // The last 1024 packets by sequence number (still encrypted, as parity covers them).
  Bytes history[1024];
  uint16_t historySeq[1024] = {};
  bool historyValid[1024] = {};
  std::deque<ParityBlock> blocks;  // recent parity, newest last

  void Remember(uint16_t seq, const Bytes& pkt) {
    history[seq & 1023] = pkt;
    historySeq[seq & 1023] = seq;
    historyValid[seq & 1023] = true;
  }
  const Bytes* Find(uint16_t seq) const {
    const int i = seq & 1023;
    return historyValid[i] && historySeq[i] == seq ? &history[i] : nullptr;
  }
  void Reset() {
    for (auto& [offset, buf] : held) gst_buffer_unref(buf);
    held.clear();
    holding = false;
    blocks.clear();
    std::fill(std::begin(historyValid), std::end(historyValid), false);
  }
};

struct TritonFecDec {
  GstElement parent;
  GstPad* sink;
  GstPad* src;
  Decoder* st;
};
struct TritonFecDecClass {
  GstElementClass parent_class;
};
G_DEFINE_TYPE(TritonFecDec, triton_fec_dec, GST_TYPE_ELEMENT)

GstStaticPadTemplate g_decSink = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);
GstStaticPadTemplate g_decSrc = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

// Decrypts one packet and passes it on (a forged or damaged one is dropped). Takes `buf`.
GstFlowReturn Output(TritonFecDec* self, GstBuffer* buf) {
  Decoder& d = *self->st;
  const Bytes pkt = ReadAll(buf);
  Bytes plain(pkt.size() > 16 ? pkt.size() - 16 : 0);
  GstFlowReturn ret = GST_FLOW_OK;
  if (d.cipher && pkt.size() > 16 && d.cipher->Open(pkt.data(), pkt.size(), plain.data())) {
    ret = gst_pad_push(self->src, NewBuffer(plain.data(), plain.size(), buf));
  }
  gst_buffer_unref(buf);
  return ret;
}

// Passes on held packets that are next in order; `force` (given up waiting) passes them all,
// leaving the gaps as they are.
GstFlowReturn Drain(TritonFecDec* self, bool force) {
  Decoder& d = *self->st;
  GstFlowReturn ret = GST_FLOW_OK;
  while (!d.held.empty()) {
    auto it = d.held.begin();
    const uint16_t seq = static_cast<uint16_t>(d.holdBase + it->first);
    if (!force && seq != d.expected) break;
    GstBuffer* buf = it->second;
    d.held.erase(it);
    d.expected = static_cast<uint16_t>(seq + 1);
    const GstFlowReturn r = Output(self, buf);
    if (r != GST_FLOW_OK) ret = r;
  }
  if (d.held.empty()) d.holding = false;
  return ret;
}

// Rebuilds the packets in `missing` (indexes into block b) from what arrived plus enough parity.
void Rebuild(Decoder& d, const ParityBlock& b, const std::vector<int>& missing) {
  const int e = static_cast<int>(missing.size());
  const size_t n = b.shardLen;
  std::vector<int> rows;
  for (const auto& [row, shard] : b.rows) {
    if (static_cast<int>(rows.size()) < e) rows.push_back(row);
  }
  // s_r = parity_r minus the contribution of every data shard that did arrive.
  std::vector<Bytes> s(e);
  Bytes shard(n);
  for (int r = 0; r < e; ++r) s[r] = b.rows.at(rows[r]);
  for (int i = 0; i < b.k; ++i) {
    if (std::find(missing.begin(), missing.end(), i) != missing.end()) continue;
    const Bytes* pkt = d.Find(static_cast<uint16_t>(b.firstSeq + i));
    if (!pkt || pkt->size() + 2 > n) return;
    ToShard(*pkt, n, shard.data());
    for (int r = 0; r < e; ++r) MulAdd(s[r].data(), shard.data(), n, Coef(rows[r], i));
  }
  // Solve Coef(rows, missing) * x = s (Gauss-Jordan over GF(2^8)).
  std::vector<std::vector<uint8_t>> a(e, std::vector<uint8_t>(e));
  for (int r = 0; r < e; ++r) {
    for (int c = 0; c < e; ++c) a[r][c] = Coef(rows[r], missing[c]);
  }
  for (int col = 0; col < e; ++col) {
    int pivot = col;
    while (pivot < e && !a[pivot][col]) ++pivot;
    if (pivot == e) return;
    std::swap(a[pivot], a[col]);
    std::swap(s[pivot], s[col]);
    const uint8_t inv = G().Inv(a[col][col]);
    for (int c = 0; c < e; ++c) a[col][c] = G().Mul(a[col][c], inv);
    Scale(s[col].data(), n, inv);
    for (int r = 0; r < e; ++r) {
      const uint8_t f = a[r][col];
      if (r == col || !f) continue;
      for (int c = 0; c < e; ++c) a[r][c] ^= G().Mul(f, a[col][c]);
      MulAdd(s[r].data(), s[col].data(), n, f);
    }
  }
  for (int c = 0; c < e; ++c) {
    const size_t len = Be16(s[c].data());
    if (len + 2 > n || len < 12) continue;
    const uint16_t seq = static_cast<uint16_t>(b.firstSeq + missing[c]);
    const Bytes pkt(s[c].begin() + 2, s[c].begin() + 2 + len);
    d.Remember(seq, pkt);
    const int16_t ahead = static_cast<int16_t>(seq - d.expected);
    if (d.holding && ahead >= 0) {
      const int offset = static_cast<int16_t>(seq - d.holdBase);
      if (!d.held.count(offset)) d.held[offset] = NewBuffer(pkt.data(), pkt.size(), nullptr);
    }
    ++g_recovered;
  }
}

// After a gap: rebuild what parity allows, pass on what is in order, give up after kHoldUs.
GstFlowReturn Recover(TritonFecDec* self) {
  Decoder& d = *self->st;
  if (!d.holding) return GST_FLOW_OK;
  for (const ParityBlock& b : d.blocks) {
    if (b.ssrc != d.ssrc) continue;
    std::vector<int> missing;
    bool useful = false;  // something we're still waiting for
    for (int i = 0; i < b.k; ++i) {
      const uint16_t seq = static_cast<uint16_t>(b.firstSeq + i);
      if (d.Find(seq)) continue;
      missing.push_back(i);
      if (static_cast<int16_t>(seq - d.expected) >= 0) useful = true;
    }
    if (useful && static_cast<int>(missing.size()) <= static_cast<int>(b.rows.size())) Rebuild(d, b, missing);
  }
  GstFlowReturn ret = Drain(self, false);
  if (d.holding && NowUs() - d.holdSince > kHoldUs) ret = Drain(self, true);
  return ret;
}

void AddParity(Decoder& d, const Bytes& pkt) {
  if (pkt.size() < kParityHeader) return;
  const int row = pkt[1], k = pkt[2], m = pkt[3];
  const uint32_t ssrc = Be32(&pkt[4]);
  const uint16_t firstSeq = Be16(&pkt[8]);
  const size_t shardLen = Be16(&pkt[10]);
  if (k < 1 || k > kMaxBlock || m < 1 || row >= m || m > 40 || shardLen > 2048 || pkt.size() != kParityHeader + shardLen) {
    return;
  }
  ParityBlock* b = nullptr;
  for (ParityBlock& x : d.blocks) {
    if (x.ssrc == ssrc && x.firstSeq == firstSeq && x.k == k && x.shardLen == shardLen) b = &x;
  }
  if (!b) {
    d.blocks.push_back({ssrc, firstSeq, k, m, shardLen, {}});
    if (d.blocks.size() > 64) d.blocks.pop_front();
    b = &d.blocks.back();
  }
  b->rows[row] = Bytes(pkt.begin() + kParityHeader, pkt.end());
}

// Test hook: LFRD_TEST_FEC_LOSS=<percent> drops that share of incoming video packets on purpose.
bool TestDrop() {
  static const int percent = [] {
    const char* v = std::getenv("LFRD_TEST_FEC_LOSS");
    return v ? std::atoi(v) : 0;
  }();
  return percent > 0 && std::rand() % 100 < percent;
}

GstFlowReturn DecChain(GstPad*, GstObject* parent, GstBuffer* in) {
  auto* self = reinterpret_cast<TritonFecDec*>(parent);
  Decoder& d = *self->st;
  const Bytes pkt = ReadAll(in);
  if (!pkt.empty() && pkt[0] == kParityMagic) {
    if (!TestDrop()) AddParity(d, pkt);
    gst_buffer_unref(in);
    return Recover(self);
  }
  if (TestDrop() || !RtpHeaderLength(pkt.data(), pkt.size())) {
    gst_buffer_unref(in);
    return Recover(self);
  }
  const uint16_t seq = Be16(&pkt[2]);
  const uint32_t ssrc = Be32(&pkt[8]);
  if (!d.started || ssrc != d.ssrc) {  // first packet, or the sender restarted its video
    d.Reset();
    d.started = true;
    d.ssrc = ssrc;
    d.expected = seq;
  }
  d.Remember(seq, pkt);
  const int16_t ahead = static_cast<int16_t>(seq - d.expected);
  if (ahead < 0) {  // late or duplicate: passed on already, or given up on
    gst_buffer_unref(in);
    return GST_FLOW_OK;
  }
  if (!d.holding && ahead == 0) {
    d.expected = static_cast<uint16_t>(seq + 1);
    return Output(self, in);
  }
  if (!d.holding) {  // a gap before this packet: hold the rest until parity fills it
    d.holding = true;
    d.holdBase = d.expected;
    d.holdSince = NowUs();
  }
  const int offset = static_cast<int16_t>(seq - d.holdBase);
  if (offset > 2000 || d.held.count(offset)) {  // nonsense distance, or a duplicate
    gst_buffer_unref(in);
  } else {
    d.held[offset] = in;
  }
  return Recover(self);
}

void DecFinalize(GObject* obj) {
  auto* self = reinterpret_cast<TritonFecDec*>(obj);
  self->st->Reset();
  delete self->st;
  G_OBJECT_CLASS(triton_fec_dec_parent_class)->finalize(obj);
}

void triton_fec_dec_class_init(TritonFecDecClass* klass) {
  auto* ec = GST_ELEMENT_CLASS(klass);
  gst_element_class_set_static_metadata(ec, "Triton FEC decoder", "Codec/Network",
                                        "Rebuilds lost RTP packets from Reed-Solomon parity and decrypts them",
                                        "Triton");
  gst_element_class_add_static_pad_template(ec, &g_decSink);
  gst_element_class_add_static_pad_template(ec, &g_decSrc);
  G_OBJECT_CLASS(klass)->finalize = DecFinalize;
}

void triton_fec_dec_init(TritonFecDec* self) {
  self->st = new Decoder();
  self->sink = gst_pad_new_from_static_template(&g_decSink, "sink");
  self->src = gst_pad_new_from_static_template(&g_decSrc, "src");
  gst_pad_set_chain_function(self->sink, DecChain);
  for (GstPad* p : {self->sink, self->src}) {
    GST_PAD_SET_PROXY_CAPS(p);
    GST_PAD_SET_PROXY_ALLOCATION(p);
    gst_element_add_pad(GST_ELEMENT(self), p);
  }
}

}  // namespace

void RegisterFecElements() {
  static std::once_flag once;
  std::call_once(once, [] {
    gst_element_register(nullptr, "tritonfecenc", GST_RANK_NONE, triton_fec_enc_get_type());
    gst_element_register(nullptr, "tritonfecdec", GST_RANK_NONE, triton_fec_dec_get_type());
  });
}

void ConfigureFec(GstElement* element, const uint8_t* key) {
  if (!element) return;
  if (G_TYPE_CHECK_INSTANCE_TYPE(element, triton_fec_enc_get_type())) {
    reinterpret_cast<TritonFecEnc*>(element)->st->cipher = std::make_unique<RtpCipher>(key);
  } else if (G_TYPE_CHECK_INSTANCE_TYPE(element, triton_fec_dec_get_type())) {
    reinterpret_cast<TritonFecDec*>(element)->st->cipher = std::make_unique<RtpCipher>(key);
  }
}

uint64_t FecRecovered() { return g_recovered; }

}  // namespace lfrd
