#include "secure.h"

#include "common.h"  // windows.h, Connection

#include <bcrypt.h>
#include <wincrypt.h>  // DPAPI

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <vector>

namespace lfrd {
namespace {

using Bytes = std::vector<uint8_t>;

constexpr wchar_t kKey[] = L"Software\\TritonScreenShare";
constexpr int kPairTimeoutMs = 120'000;

std::wstring Wide(const std::string& s);

// Test hooks: LFRD_TEST_DEVICE_KEY=<name> gives this process a second identity (its own device key
// and paired list), so two copies on one PC can pair; LFRD_TEST_PAIR=yes|no answers the question.
std::wstring TestSuffix() {
  const char* name = std::getenv("LFRD_TEST_DEVICE_KEY");
  return name ? L"-" + Wide(name) : L"";
}
std::wstring PairedKey() {  // value name = fingerprint, data = PC name
  return L"Software\\TritonScreenShare\\Paired" + TestSuffix();
}

// The first message both PCs send on a new connection (in the clear: it holds only public keys).
#pragma pack(push, 1)
struct Kex {
  uint32_t version;
  uint8_t staticPub[64];  // device key (P-256 X, Y)
  uint8_t ephPub[64];     // one-time key for this connection
  uint8_t nonce[32];
  char name[32];
};
#pragma pack(pop)

BCRYPT_ALG_HANDLE OpenAlg(LPCWSTR id, ULONG flags = 0) {
  BCRYPT_ALG_HANDLE h = nullptr;
  BCryptOpenAlgorithmProvider(&h, id, nullptr, flags);
  return h;
}
BCRYPT_ALG_HANDLE EcdhAlg() {
  static BCRYPT_ALG_HANDLE h = OpenAlg(BCRYPT_ECDH_P256_ALGORITHM);
  return h;
}
BCRYPT_ALG_HANDLE ShaAlg() {
  static BCRYPT_ALG_HANDLE h = OpenAlg(BCRYPT_SHA256_ALGORITHM);
  return h;
}
BCRYPT_ALG_HANDLE HmacAlg() {
  static BCRYPT_ALG_HANDLE h = OpenAlg(BCRYPT_SHA256_ALGORITHM, BCRYPT_ALG_HANDLE_HMAC_FLAG);
  return h;
}
BCRYPT_ALG_HANDLE GcmAlg() {
  static BCRYPT_ALG_HANDLE h = [] {
    BCRYPT_ALG_HANDLE a = OpenAlg(BCRYPT_AES_ALGORITHM);
    if (a) {
      BCryptSetProperty(a, BCRYPT_CHAINING_MODE, reinterpret_cast<PUCHAR>(const_cast<wchar_t*>(BCRYPT_CHAIN_MODE_GCM)),
                        sizeof(BCRYPT_CHAIN_MODE_GCM), 0);
    }
    return a;
  }();
  return h;
}

Bytes Sha256(const Bytes& data) {
  Bytes out(32);
  BCryptHash(ShaAlg(), nullptr, 0, const_cast<PUCHAR>(data.data()), static_cast<ULONG>(data.size()), out.data(), 32);
  return out;
}

Bytes Hmac(const Bytes& key, const Bytes& data) {
  Bytes out(32);
  BCryptHash(HmacAlg(), const_cast<PUCHAR>(key.data()), static_cast<ULONG>(key.size()), const_cast<PUCHAR>(data.data()),
             static_cast<ULONG>(data.size()), out.data(), 32);
  return out;
}

std::string Hex(const uint8_t* p, size_t n) {
  static const char digits[] = "0123456789abcdef";
  std::string s;
  for (size_t i = 0; i < n; ++i) {
    s += digits[p[i] >> 4];
    s += digits[p[i] & 15];
  }
  return s;
}

std::wstring Wide(const std::string& s) {
  std::wstring w(s.size(), L'\0');
  w.resize(MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), static_cast<int>(w.size())));
  return w;
}

// ---------------------------------------------------------------------------
// Keys

BCRYPT_KEY_HANDLE NewKeyPair() {
  BCRYPT_KEY_HANDLE h = nullptr;
  if (BCryptGenerateKeyPair(EcdhAlg(), &h, 256, 0) != 0) return nullptr;
  if (BCryptFinalizeKeyPair(h, 0) != 0) {
    BCryptDestroyKey(h);
    return nullptr;
  }
  return h;
}

bool ExportPublic(BCRYPT_KEY_HANDLE h, uint8_t pub[64]) {
  uint8_t blob[sizeof(BCRYPT_ECCKEY_BLOB) + 64];
  ULONG cb = 0;
  if (!h || BCryptExportKey(h, nullptr, BCRYPT_ECCPUBLIC_BLOB, blob, sizeof(blob), &cb, 0) != 0 || cb != sizeof(blob)) {
    return false;
  }
  std::memcpy(pub, blob + sizeof(BCRYPT_ECCKEY_BLOB), 64);
  return true;
}

// Fails for anything that isn't a point on the curve.
BCRYPT_KEY_HANDLE ImportPublic(const uint8_t pub[64]) {
  uint8_t blob[sizeof(BCRYPT_ECCKEY_BLOB) + 64];
  auto* header = reinterpret_cast<BCRYPT_ECCKEY_BLOB*>(blob);
  header->dwMagic = BCRYPT_ECDH_PUBLIC_P256_MAGIC;
  header->cbKey = 32;
  std::memcpy(blob + sizeof(BCRYPT_ECCKEY_BLOB), pub, 64);
  BCRYPT_KEY_HANDLE h = nullptr;
  return BCryptImportKeyPair(EcdhAlg(), nullptr, BCRYPT_ECCPUBLIC_BLOB, &h, blob, sizeof(blob), 0) == 0 ? h : nullptr;
}

Bytes Agree(BCRYPT_KEY_HANDLE mine, BCRYPT_KEY_HANDLE theirs) {
  BCRYPT_SECRET_HANDLE secret = nullptr;
  if (!mine || !theirs || BCryptSecretAgreement(mine, theirs, &secret, 0) != 0) return {};
  Bytes out(32);
  ULONG cb = 0;
  const NTSTATUS st = BCryptDeriveKey(secret, BCRYPT_KDF_RAW_SECRET, nullptr, out.data(), 32, &cb, 0);
  BCryptDestroySecret(secret);
  return st == 0 && cb == 32 ? out : Bytes{};
}

struct Device {
  BCRYPT_KEY_HANDLE key = nullptr;
  uint8_t pub[64] = {};
  std::string fp;
};

// This PC's device key: loaded from HKCU (DPAPI-protected, so only this Windows account can use it),
// or made and stored on first use.
const Device& Me() {
  static const Device device = [] {
    Device d;
    const std::wstring value = L"DeviceKey" + TestSuffix();
    DWORD size = 0;
    if (RegGetValueW(HKEY_CURRENT_USER, kKey, value.c_str(), RRF_RT_REG_BINARY, nullptr, nullptr, &size) == ERROR_SUCCESS &&
        size) {
      Bytes stored(size);
      if (RegGetValueW(HKEY_CURRENT_USER, kKey, value.c_str(), RRF_RT_REG_BINARY, nullptr, stored.data(), &size) ==
          ERROR_SUCCESS) {
        DATA_BLOB in{size, stored.data()}, out{};
        if (CryptUnprotectData(&in, nullptr, nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out)) {
          BCryptImportKeyPair(EcdhAlg(), nullptr, BCRYPT_ECCPRIVATE_BLOB, &d.key, out.pbData, out.cbData, 0);
          SecureZeroMemory(out.pbData, out.cbData);
          LocalFree(out.pbData);
        }
      }
    }
    if (!d.key && (d.key = NewKeyPair())) {
      ULONG cb = 0;
      BCryptExportKey(d.key, nullptr, BCRYPT_ECCPRIVATE_BLOB, nullptr, 0, &cb, 0);
      Bytes blob(cb);
      if (BCryptExportKey(d.key, nullptr, BCRYPT_ECCPRIVATE_BLOB, blob.data(), cb, &cb, 0) == 0) {
        DATA_BLOB in{cb, blob.data()}, out{};
        if (CryptProtectData(&in, L"Triton Screen Share device key", nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN,
                             &out)) {
          RegSetKeyValueW(HKEY_CURRENT_USER, kKey, value.c_str(), REG_BINARY, out.pbData, out.cbData);
          LocalFree(out.pbData);
        }
      }
      SecureZeroMemory(blob.data(), blob.size());
    }
    if (ExportPublic(d.key, d.pub)) {
      const Bytes h = Sha256(Bytes(d.pub, d.pub + 64));
      d.fp = Hex(h.data(), h.size());
    }
    return d;
  }();
  return device;
}

// ---------------------------------------------------------------------------
// Pairing

std::mutex g_promptMu;
PairPrompt g_prompt;

void StorePairing(const std::string& fp, const std::string& name) {
  const std::wstring value = Wide(name);
  RegSetKeyValueW(HKEY_CURRENT_USER, PairedKey().c_str(), Wide(fp).c_str(), REG_SZ, value.c_str(),
                  static_cast<DWORD>((value.size() + 1) * sizeof(wchar_t)));
}

bool ConsolePrompt(const std::string& name, const std::string& code) {
  DWORD mode;
  if (!GetConsoleMode(GetStdHandle(STD_INPUT_HANDLE), &mode)) return false;
  std::printf("Pair with %s? If it shows the code %s too, type y and press Enter: ", name.c_str(), code.c_str());
  std::fflush(stdout);
  char line[16] = {};
  return std::fgets(line, sizeof(line), stdin) && (line[0] == 'y' || line[0] == 'Y');
}

// Waits for one message of type `want` (the handshake's own messages; nothing else comes first).
bool RecvTyped(Connection& conn, uint8_t want, Bytes& p, size_t minSize, int timeoutMs) {
  const uint64_t deadline = NowUs() + static_cast<uint64_t>(timeoutMs) * 1000;
  while (NowUs() < deadline && !g_stop) {
    if (!conn.WaitReadable(200)) continue;
    uint8_t type = 0;
    if (!conn.Recv(type, p)) return false;
    return type == want && p.size() >= minSize;
  }
  return false;
}

}  // namespace

// ---------------------------------------------------------------------------

AeadKey::AeadKey(const uint8_t key[32]) {
  BCRYPT_KEY_HANDLE h = nullptr;
  if (BCryptGenerateSymmetricKey(GcmAlg(), &h, nullptr, 0, const_cast<PUCHAR>(key), 32, 0) == 0) key_ = h;
}

AeadKey::~AeadKey() {
  if (key_) BCryptDestroyKey(static_cast<BCRYPT_KEY_HANDLE>(key_));
}

bool AeadKey::Seal(const uint8_t nonce[12], const uint8_t* aad, size_t aadLen, const uint8_t* in, size_t n,
                   uint8_t* out) {
  BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO info;
  BCRYPT_INIT_AUTH_MODE_INFO(info);
  info.pbNonce = const_cast<PUCHAR>(nonce);
  info.cbNonce = 12;
  info.pbAuthData = const_cast<PUCHAR>(aad);
  info.cbAuthData = static_cast<ULONG>(aadLen);
  info.pbTag = out + n;
  info.cbTag = 16;
  ULONG cb = 0;
  return key_ && BCryptEncrypt(static_cast<BCRYPT_KEY_HANDLE>(key_), const_cast<PUCHAR>(in), static_cast<ULONG>(n), &info,
                               nullptr, 0, out, static_cast<ULONG>(n), &cb, 0) == 0;
}

bool AeadKey::Open(const uint8_t nonce[12], const uint8_t* aad, size_t aadLen, const uint8_t* in, size_t n,
                   uint8_t* out) {
  if (n < 16) return false;
  BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO info;
  BCRYPT_INIT_AUTH_MODE_INFO(info);
  info.pbNonce = const_cast<PUCHAR>(nonce);
  info.cbNonce = 12;
  info.pbAuthData = const_cast<PUCHAR>(aad);
  info.cbAuthData = static_cast<ULONG>(aadLen);
  info.pbTag = const_cast<PUCHAR>(in + n - 16);
  info.cbTag = 16;
  ULONG cb = 0;
  return key_ && BCryptDecrypt(static_cast<BCRYPT_KEY_HANDLE>(key_), const_cast<PUCHAR>(in), static_cast<ULONG>(n - 16),
                               &info, nullptr, 0, out, static_cast<ULONG>(n - 16), &cb, 0) == 0;
}

bool AeadKey::Seal(uint64_t counter, const uint8_t* in, size_t n, uint8_t* out) {
  uint8_t nonce[12] = {};
  std::memcpy(nonce + 4, &counter, 8);
  return Seal(nonce, nullptr, 0, in, n, out);
}

bool AeadKey::Open(uint64_t counter, const uint8_t* in, size_t n, uint8_t* out) {
  uint8_t nonce[12] = {};
  std::memcpy(nonce + 4, &counter, 8);
  return Open(nonce, nullptr, 0, in, n, out);
}

// ---------------------------------------------------------------------------
// Media packets

size_t RtpHeaderLength(const uint8_t* p, size_t n) {
  if (n < 12 || (p[0] >> 6) != 2) return 0;
  size_t len = 12 + 4 * static_cast<size_t>(p[0] & 0x0F);  // CSRCs
  if (p[0] & 0x10) {                                        // header extension
    if (n < len + 4) return 0;
    len += 4 + 4 * static_cast<size_t>((p[len + 2] << 8) | p[len + 3]);
  }
  return len <= n ? len : 0;
}

RtpCipher::RtpCipher(const uint8_t key[kSrtpKeyBytes]) : key_(key) { std::memcpy(salt_, key + 32, sizeof(salt_)); }

RtpCipher::Stream* RtpCipher::Find(uint32_t ssrc) {
  for (int i = 0; i < 4; ++i) {
    if (streams_[i].started && ssrc_[i] == ssrc) return &streams_[i];
  }
  return nullptr;
}

RtpCipher::Stream& RtpCipher::For(uint32_t ssrc) {
  if (Stream* s = Find(ssrc)) return *s;
  const int i = next_;  // a new stream takes the oldest slot
  next_ = (next_ + 1) % 4;
  ssrc_[i] = ssrc;
  streams_[i] = Stream{};
  return streams_[i];
}

void RtpCipher::Nonce(uint32_t ssrc, uint64_t index, uint8_t nonce[12]) const {
  uint8_t iv[12] = {0, 0, static_cast<uint8_t>(ssrc >> 24), static_cast<uint8_t>(ssrc >> 16),
                    static_cast<uint8_t>(ssrc >> 8), static_cast<uint8_t>(ssrc)};
  for (int b = 0; b < 6; ++b) iv[6 + b] = static_cast<uint8_t>(index >> (40 - 8 * b));
  for (int b = 0; b < 12; ++b) nonce[b] = iv[b] ^ salt_[b];
}

bool RtpCipher::Seal(const uint8_t* packet, size_t n, uint8_t* out) {
  const size_t header = RtpHeaderLength(packet, n);
  if (!header) return false;
  const uint16_t seq = static_cast<uint16_t>((packet[2] << 8) | packet[3]);
  const uint32_t ssrc = (static_cast<uint32_t>(packet[8]) << 24) | (packet[9] << 16) | (packet[10] << 8) | packet[11];
  Stream& s = For(ssrc);
  if (s.started && seq < s.seq && s.seq - seq > 0x8000) ++s.roc;  // the sequence number wrapped
  s.seq = seq;
  s.started = true;
  uint8_t nonce[12];
  Nonce(ssrc, (s.roc << 16) | seq, nonce);
  std::memcpy(out, packet, header);
  return key_.Seal(nonce, packet, header, packet + header, n - header, out + header);
}

bool RtpCipher::Open(const uint8_t* packet, size_t n, uint8_t* out) {
  const size_t header = RtpHeaderLength(packet, n);
  if (!header || n < header + 16) return false;
  const uint16_t seq = static_cast<uint16_t>((packet[2] << 8) | packet[3]);
  const uint32_t ssrc = (static_cast<uint32_t>(packet[8]) << 24) | (packet[9] << 16) | (packet[10] << 8) | packet[11];
  // A stream is only tracked once a packet of it proves genuine, so forged packets can't push the
  // real streams' state out.
  Stream* s = Find(ssrc);
  // Which wrap this packet belongs to, judged against the newest one so far (RFC 3711, 3.3.1).
  uint64_t roc = s ? s->roc : 0;
  if (s) {
    if (s->seq < 0x8000) {
      if (seq > s->seq && seq - s->seq > 0x8000 && roc > 0) --roc;  // a late one from before the wrap
    } else if (seq < s->seq && s->seq - seq > 0x8000) {
      ++roc;  // just wrapped
    }
  }
  const uint64_t index = (roc << 16) | seq, newest = s ? (s->roc << 16) | s->seq : 0;
  if (s && index + 4096 < newest) return false;  // far too old: a replay
  uint8_t nonce[12];
  Nonce(ssrc, index, nonce);
  std::memcpy(out, packet, header);
  if (!key_.Open(nonce, packet, header, packet + header, n - header, out + header)) return false;
  if (!s) s = &For(ssrc);
  if (!s->started || index > newest) {  // only a genuine packet moves the window
    s->roc = roc;
    s->seq = seq;
    s->started = true;
  }
  return true;
}

std::string DeviceFingerprint() { return Me().fp; }

bool IsPaired(const std::string& fp) {
  if (fp.size() < 16) return false;
  if (Me().fp.rfind(fp, 0) == 0) return true;
  HKEY key;
  if (RegOpenKeyExW(HKEY_CURRENT_USER, PairedKey().c_str(), 0, KEY_READ, &key) != ERROR_SUCCESS) return false;
  bool found = false;
  wchar_t name[128];
  for (DWORD i = 0; !found; ++i) {
    DWORD n = 128;
    if (RegEnumValueW(key, i, name, &n, nullptr, nullptr, nullptr, nullptr) != ERROR_SUCCESS) break;
    found = Narrow(name).rfind(fp, 0) == 0;
  }
  RegCloseKey(key);
  return found;
}

void ForgetPairings() { RegDeleteTreeW(HKEY_CURRENT_USER, PairedKey().c_str()); }

void SetPairPrompt(PairPrompt prompt) {
  std::lock_guard<std::mutex> lock(g_promptMu);
  g_prompt = std::move(prompt);
}

namespace {
std::atomic<uint64_t> g_pairingUntil{0};  // GetTickCount64
}  // namespace

void OpenPairing(int seconds) { g_pairingUntil = GetTickCount64() + static_cast<uint64_t>(seconds) * 1000; }

bool PairingOpen() { return GetTickCount64() < g_pairingUntil; }

bool SecureHandshake(Connection& conn, bool initiator, SessionKeys& keys, std::string& peerName) {
  const Device& me = Me();
  const std::string peer = conn.PeerIp();
  if (!me.key) {
    std::fprintf(stderr, "Can't encrypt: Windows' crypto (CNG) is unavailable on this PC.\n");
    return false;
  }
  // Every key handle made here goes when this function returns.
  struct Owned {
    std::vector<BCRYPT_KEY_HANDLE> keys;
    ~Owned() {
      for (BCRYPT_KEY_HANDLE k : keys) {
        if (k) BCryptDestroyKey(k);
      }
    }
  } owned;
  BCRYPT_KEY_HANDLE eph = NewKeyPair();
  owned.keys.push_back(eph);

  Kex mine{};
  mine.version = kProtocolVersion;
  std::memcpy(mine.staticPub, me.pub, 64);
  BCryptGenRandom(nullptr, mine.nonce, sizeof(mine.nonce), BCRYPT_USE_SYSTEM_PREFERRED_RNG);
  strncpy_s(mine.name, ComputerName().c_str(), _TRUNCATE);
  if (!ExportPublic(eph, mine.ephPub) || !conn.Send(MSG_KEX, mine)) return false;

  Bytes p;
  if (!RecvTyped(conn, MSG_KEX, p, sizeof(Kex), 10'000)) {
    std::fprintf(stderr, "%s didn't start a secure connection (does it run this version?).\n", peer.c_str());
    return false;
  }
  Kex theirs;
  std::memcpy(&theirs, p.data(), sizeof(theirs));
  theirs.name[sizeof(theirs.name) - 1] = '\0';
  peerName = theirs.name;
  if (theirs.version != kProtocolVersion) {
    std::fprintf(stderr, "%s (%s) uses protocol v%u; this app speaks v%u. Update both PCs.\n", peerName.c_str(),
                 peer.c_str(), theirs.version, kProtocolVersion);
    return false;
  }
  BCRYPT_KEY_HANDLE peerStatic = ImportPublic(theirs.staticPub), peerEph = ImportPublic(theirs.ephPub);
  owned.keys.push_back(peerStatic);
  owned.keys.push_back(peerEph);
  if (!peerStatic || !peerEph) {
    std::fprintf(stderr, "%s sent an invalid key.\n", peer.c_str());
    return false;
  }

  // One-time x one-time (forward secrecy), plus each side's device key x the other's one-time key:
  // only the holders of both device keys get the same result.
  const Bytes ee = Agree(eph, peerEph);
  const Bytes se = initiator ? Agree(me.key, peerEph) : Agree(eph, peerStatic);  // initiator's device key
  const Bytes es = initiator ? Agree(eph, peerStatic) : Agree(me.key, peerEph);  // responder's device key
  if (ee.empty() || se.empty() || es.empty()) return false;
  Bytes ikm = ee;
  ikm.insert(ikm.end(), se.begin(), se.end());
  ikm.insert(ikm.end(), es.begin(), es.end());

  const Kex& first = initiator ? mine : theirs;
  const Kex& second = initiator ? theirs : mine;
  Bytes transcript(reinterpret_cast<const uint8_t*>(&first), reinterpret_cast<const uint8_t*>(&first) + sizeof(Kex));
  transcript.insert(transcript.end(), reinterpret_cast<const uint8_t*>(&second),
                    reinterpret_cast<const uint8_t*>(&second) + sizeof(Kex));
  const Bytes th = Sha256(transcript);
  const Bytes prk = Hmac(th, ikm);  // HKDF-extract, salted with the transcript
  SecureZeroMemory(ikm.data(), ikm.size());
  auto derive = [&prk](const char* label, uint8_t* out, size_t n) {  // HKDF-expand
    Bytes prev;
    for (uint8_t block = 1; n; ++block) {
      Bytes info = prev;
      info.insert(info.end(), label, label + std::strlen(label));
      info.push_back(block);
      prev = Hmac(prk, info);
      const size_t take = std::min<size_t>(n, prev.size());
      std::memcpy(out, prev.data(), take);
      out += take;
      n -= take;
    }
  };
  uint8_t toResponder[32], toInitiator[32], confirmI[32], confirmR[32], sas[32];
  derive("control i>r", toResponder, 32);
  derive("control r>i", toInitiator, 32);
  derive("confirm i", confirmI, 32);
  derive("confirm r", confirmR, 32);
  derive("pairing code", sas, 32);
  derive("srtp video", keys.video, kSrtpKeyBytes);
  derive("srtp audio", keys.audio, kSrtpKeyBytes);
  derive("srtp mic", keys.mic, kSrtpKeyBytes);
  derive("srtp camera", keys.camera, kSrtpKeyBytes);

  // Key confirmation: proves the other PC computed the same keys, i.e. holds the device key it sent.
  const Bytes mineConfirm = Hmac(Bytes(initiator ? confirmI : confirmR, (initiator ? confirmI : confirmR) + 32), th);
  const Bytes wantConfirm = Hmac(Bytes(initiator ? confirmR : confirmI, (initiator ? confirmR : confirmI) + 32), th);
  if (!conn.Send(MSG_KEX_CONFIRM, mineConfirm.data(), 32)) return false;
  if (!RecvTyped(conn, MSG_KEX_CONFIRM, p, 32, 10'000) || !std::equal(wantConfirm.begin(), wantConfirm.end(), p.begin())) {
    std::fprintf(stderr, "Couldn't verify %s (%s): its keys don't match. Connection refused.\n", peerName.c_str(),
                 peer.c_str());
    return false;
  }
  conn.EnableEncryption(initiator ? toResponder : toInitiator, initiator ? toInitiator : toResponder);

  // Pairing: a PC whose device key isn't known yet needs the user's OK, after comparing the code
  // both PCs show (a PC in between would make the codes differ).
  const Bytes peerHash = Sha256(Bytes(theirs.staticPub, theirs.staticPub + 64));
  const std::string peerFp = Hex(peerHash.data(), peerHash.size());
  const bool known = IsPaired(peerFp);
  const uint32_t v = ((static_cast<uint32_t>(sas[0]) << 24) | (sas[1] << 16) | (sas[2] << 8) | sas[3]) % 1'000'000;
  char code[8];
  std::snprintf(code, sizeof(code), "%03u %03u", v / 1000, v % 1000);
  bool ok = known;
  if (!known) {
    PairPrompt prompt;
    {
      std::lock_guard<std::mutex> lock(g_promptMu);
      prompt = g_prompt;
    }
    const char* testAnswer = std::getenv("LFRD_TEST_PAIR");
    if (prompt && !testAnswer && !PairingOpen()) {
      // Strict: nobody gets as far as a question on screen unless pairing was opened here.
      std::printf("%s (%s) isn't paired with this PC: refused. To pair it, click \"Pair a new PC\" in the panel "
                  "on both PCs, then connect within 2 minutes.\n", peerName.c_str(), peer.c_str());
    } else {
      std::printf("Pairing with %s (%s): code %s. Check that both PCs show this code.\n", peerName.c_str(),
                  peer.c_str(), code);
      if (testAnswer) ok = std::string(testAnswer) == "yes";
      else ok = prompt ? prompt(peerName, code) : ConsolePrompt(peerName, code);
    }
  }
  if (!conn.Send(MSG_PAIR, static_cast<uint8_t>(ok))) return false;
  if (!ok) {  // refused here: hang up at once (waiting for the other side's answer let a PC that never
              // answers hold up this PC's connections for minutes)
    if (known || PairingOpen()) std::printf("Not paired with %s: connection refused.\n", peerName.c_str());
    return false;
  }
  if (!RecvTyped(conn, MSG_PAIR, p, 1, kPairTimeoutMs + 30'000)) {
    std::fprintf(stderr, "%s didn't confirm pairing in time. Connection refused.\n", peerName.c_str());
    return false;
  }
  if (!p[0]) {
    std::printf("%s didn't accept pairing with this PC: connection refused.\n", peerName.c_str());
    return false;
  }
  if (!known) {
    StorePairing(peerFp, peerName);
    std::printf("Paired with %s. Next time they connect without a code.\n", peerName.c_str());
  }
  std::printf("Secure connection with %s: everything is encrypted.\n", peerName.c_str());
  return true;
}

}  // namespace lfrd
