// Winsock helpers: framed TCP control channel and UDP broadcast discovery.
#include "common.h"

#include <iphlpapi.h>

#include "secure.h"

#include <cstdio>
#include <cstring>

namespace lfrd {

std::atomic<bool> g_stop{false};
std::atomic<HWND> g_notifyHwnd{nullptr};

uint64_t NowUs() {
  static const int64_t freq = [] {
    LARGE_INTEGER f;
    QueryPerformanceFrequency(&f);
    return f.QuadPart;
  }();
  LARGE_INTEGER c;
  QueryPerformanceCounter(&c);
  return static_cast<uint64_t>(c.QuadPart / freq * 1000000 + (c.QuadPart % freq) * 1000000 / freq);
}

bool InitWinsock() {
  WSADATA wsa;
  return WSAStartup(MAKEWORD(2, 2), &wsa) == 0;
}

std::string Narrow(const wchar_t* s) {
  if (!s) return {};
  int n = WideCharToMultiByte(CP_UTF8, 0, s, -1, nullptr, 0, nullptr, nullptr);
  if (n <= 1) return {};
  std::string out(n - 1, '\0');
  WideCharToMultiByte(CP_UTF8, 0, s, -1, out.data(), n, nullptr, nullptr);
  return out;
}

std::string ComputerName() {
  wchar_t buf[MAX_COMPUTERNAME_LENGTH + 1];
  DWORD size = MAX_COMPUTERNAME_LENGTH + 1;
  if (!GetComputerNameW(buf, &size)) return "unknown";
  return Narrow(buf);
}

// ---------------------------------------------------------------------------
// Connection

static bool SendAll(SOCKET s, const char* p, int n) {
  while (n > 0) {
    int r = send(s, p, n, 0);
    if (r <= 0) return false;
    p += r;
    n -= r;
  }
  return true;
}

static bool RecvAll(SOCKET s, char* p, int n) {
  while (n > 0) {
    int r = recv(s, p, n, 0);
    if (r <= 0) return false;
    p += r;
    n -= r;
  }
  return true;
}

Connection::Connection(SOCKET s) : sock_(s) {}

Connection::~Connection() {
  if (sock_ != INVALID_SOCKET) closesocket(sock_);
}

void Connection::EnableEncryption(const uint8_t sendKey[32], const uint8_t recvKey[32]) {
  std::lock_guard<std::mutex> lock(sendMu_);
  sendKey_ = std::make_unique<AeadKey>(sendKey);
  recvKey_ = std::make_unique<AeadKey>(recvKey);
}

bool Connection::Send(uint8_t type, const void* data, uint16_t len) {
  // One buffer so header and payload leave in a single segment (TCP_NODELAY is on).
  uint8_t buf[3 + 1 + 512 + 16];
  if (len > 512) return false;
  std::lock_guard<std::mutex> lock(sendMu_);
  if (sendKey_) {  // [len][MSG_SEALED][ciphertext of type + payload][16-byte tag]
    uint8_t plain[1 + 512];
    plain[0] = type;
    if (len) std::memcpy(plain + 1, data, len);
    const uint16_t sealed = static_cast<uint16_t>(1 + len + 16);
    std::memcpy(buf, &sealed, 2);
    buf[2] = MSG_SEALED;
    if (!sendKey_->Seal(sent_++, plain, 1 + len, buf + 3)) return false;
    return SendAll(sock_, reinterpret_cast<const char*>(buf), 3 + sealed);
  }
  std::memcpy(buf, &len, 2);
  buf[2] = type;
  if (len) std::memcpy(buf + 3, data, len);
  return SendAll(sock_, reinterpret_cast<const char*>(buf), 3 + len);
}

bool Connection::Recv(uint8_t& type, std::vector<uint8_t>& payload) {
  char hdr[3];
  if (!RecvAll(sock_, hdr, 3)) return false;
  uint16_t len;
  std::memcpy(&len, hdr, 2);
  type = static_cast<uint8_t>(hdr[2]);
  payload.resize(len);
  if (len && !RecvAll(sock_, reinterpret_cast<char*>(payload.data()), len)) return false;
  if (!recvKey_) return true;
  // Encrypted from here on: anything unsealed, tampered with, replayed or out of order ends it.
  if (type != MSG_SEALED || len < 1 + 16) return false;
  std::vector<uint8_t> plain(len - 16);
  if (!recvKey_->Open(received_++, payload.data(), len, plain.data())) return false;
  type = plain[0];
  payload.assign(plain.begin() + 1, plain.end());
  return true;
}

bool Connection::WaitReadable(int timeoutMs) {
  fd_set rd;
  FD_ZERO(&rd);
  FD_SET(sock_, &rd);
  timeval tv{timeoutMs / 1000, (timeoutMs % 1000) * 1000};
  return select(0, &rd, nullptr, nullptr, &tv) > 0;
}

void Connection::Shutdown() { shutdown(sock_, SD_BOTH); }

std::string Connection::PeerIp() const {
  sockaddr_in addr{};
  int len = sizeof(addr);
  if (getpeername(sock_, reinterpret_cast<sockaddr*>(&addr), &len) != 0) return {};
  char ip[INET_ADDRSTRLEN] = {};
  inet_ntop(AF_INET, &addr.sin_addr, ip, sizeof(ip));
  return ip;
}

void SetNoDelay(SOCKET s) {
  BOOL on = TRUE;
  setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&on), sizeof(on));
}

SOCKET ConnectTcp(const std::string& host, uint16_t port, int timeoutMs) {
  addrinfo hints{};
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* res = nullptr;
  if (getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints, &res) != 0 || !res) {
    return INVALID_SOCKET;
  }
  SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == INVALID_SOCKET) {
    freeaddrinfo(res);
    return INVALID_SOCKET;
  }
  u_long nonBlocking = 1;
  ioctlsocket(s, FIONBIO, &nonBlocking);
  connect(s, res->ai_addr, static_cast<int>(res->ai_addrlen));
  freeaddrinfo(res);

  fd_set wr, ex;
  FD_ZERO(&wr);
  FD_ZERO(&ex);
  FD_SET(s, &wr);
  FD_SET(s, &ex);
  timeval tv{timeoutMs / 1000, (timeoutMs % 1000) * 1000};
  if (select(0, nullptr, &wr, &ex, &tv) <= 0 || FD_ISSET(s, &ex)) {
    closesocket(s);
    return INVALID_SOCKET;
  }
  nonBlocking = 0;
  ioctlsocket(s, FIONBIO, &nonBlocking);
  SetNoDelay(s);
  return s;
}

SOCKET ListenTcp(uint16_t port) {
  SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == INVALID_SOCKET) return s;
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(port);
  if (bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 || listen(s, 4) != 0) {
    closesocket(s);
    return INVALID_SOCKET;
  }
  return s;
}

// ---------------------------------------------------------------------------
// Discovery: the receiver broadcasts "LFRD1|<control port>|<name>" once a second
// on every active IPv4 interface; the sender listens for a couple of seconds. Listeners also ask
// ("LFRQ1") and get answered directly, for PCs whose firewall drops incoming broadcasts.

static std::string Ipv4ToString(uint32_t hostOrder) {
  in_addr a;
  a.s_addr = htonl(hostOrder);
  char buf[INET_ADDRSTRLEN] = {};
  inet_ntop(AF_INET, &a, buf, sizeof(buf));
  return buf;
}

std::vector<LocalAddr> LocalAddresses() {
  std::vector<LocalAddr> out;
  ULONG size = 16 * 1024;
  std::vector<uint8_t> buf(size);
  const ULONG flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER;
  ULONG rc;
  while ((rc = GetAdaptersAddresses(AF_INET, flags, nullptr,
                                    reinterpret_cast<IP_ADAPTER_ADDRESSES*>(buf.data()), &size)) ==
         ERROR_BUFFER_OVERFLOW) {
    buf.resize(size);
  }
  if (rc != NO_ERROR) return out;

  for (auto* a = reinterpret_cast<IP_ADAPTER_ADDRESSES*>(buf.data()); a; a = a->Next) {
    if (a->OperStatus != IfOperStatusUp || a->IfType == IF_TYPE_SOFTWARE_LOOPBACK) continue;
    for (auto* u = a->FirstUnicastAddress; u; u = u->Next) {
      auto* sin = reinterpret_cast<sockaddr_in*>(u->Address.lpSockaddr);
      uint32_t ip = ntohl(sin->sin_addr.s_addr);
      if ((ip >> 16) == 0xA9FE) continue;  // 169.254.x.x link-local
      uint8_t prefix = u->OnLinkPrefixLength;
      uint32_t mask = prefix == 0 ? 0 : 0xFFFFFFFFu << (32 - prefix);
      out.push_back({Ipv4ToString(ip), Ipv4ToString(ip | ~mask), Narrow(a->FriendlyName)});
    }
  }
  return out;
}

std::string BeaconText(uint16_t port, const std::string& name, bool sender) {
  return (sender ? "LFRS1|" : "LFRD1|") + std::to_string(port) + "|" + name + "|" + DeviceFingerprint().substr(0, 16);
}

void SendBeacon(uint16_t port, const std::string& name, bool sender) {
  const std::string msg = BeaconText(port, name, sender);
  for (const auto& la : LocalAddresses()) {
    SOCKET s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s == INVALID_SOCKET) continue;
    BOOL on = TRUE;
    setsockopt(s, SOL_SOCKET, SO_BROADCAST, reinterpret_cast<const char*>(&on), sizeof(on));
    sockaddr_in src{};
    src.sin_family = AF_INET;
    inet_pton(AF_INET, la.ip.c_str(), &src.sin_addr);
    if (bind(s, reinterpret_cast<sockaddr*>(&src), sizeof(src)) == 0) {
      sockaddr_in dst{};
      dst.sin_family = AF_INET;
      dst.sin_port = htons(kDiscoveryPort);
      inet_pton(AF_INET, la.broadcast.c_str(), &dst.sin_addr);
      sendto(s, msg.data(), static_cast<int>(msg.size()), 0, reinterpret_cast<sockaddr*>(&dst),
             sizeof(dst));
    }
    closesocket(s);
  }
}

// The other machines Windows has recently talked to on this PC's networks (its ARP cache).
static std::vector<std::string> Neighbours() {
  std::vector<std::string> out;
  PMIB_IPNET_TABLE2 table = nullptr;
  if (GetIpNetTable2(AF_INET, &table) != NO_ERROR) return out;
  for (ULONG i = 0; i < table->NumEntries && out.size() < 64; ++i) {
    const MIB_IPNET_ROW2& row = table->Table[i];
    if (row.State != NlnsReachable && row.State != NlnsStale && row.State != NlnsDelay && row.State != NlnsProbe) {
      continue;
    }
    const uint32_t ip = ntohl(row.Address.Ipv4.sin_addr.s_addr);
    if (ip >> 28 >= 0xE || (ip & 0xFF) == 0xFF || (ip >> 16) == 0xA9FE) continue;  // multicast, broadcast, link-local
    out.push_back(Ipv4ToString(ip));
  }
  FreeMibTable(table);
  return out;
}

std::vector<FoundReceiver> Discover(int timeoutMs, const std::string& answer, const std::vector<std::string>& ask) {
  std::vector<FoundReceiver> found;
  SOCKET s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (s == INVALID_SOCKET) return found;
  BOOL on = TRUE;
  setsockopt(s, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&on), sizeof(on));
  setsockopt(s, SOL_SOCKET, SO_BROADCAST, reinterpret_cast<const char*>(&on), sizeof(on));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(kDiscoveryPort);
  if (bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    addr.sin_port = 0;  // the port is taken: no broadcasts then, but answers to our questions still arrive
    if (bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
      closesocket(s);
      return found;
    }
  }

  auto sendTo = [s](const std::string& ip, const std::string& msg) {
    sockaddr_in dst{};
    dst.sin_family = AF_INET;
    dst.sin_port = htons(kDiscoveryPort);
    if (inet_pton(AF_INET, ip.c_str(), &dst.sin_addr) == 1) {
      sendto(s, msg.data(), static_cast<int>(msg.size()), 0, reinterpret_cast<sockaddr*>(&dst), sizeof(dst));
    }
  };
  for (const auto& la : LocalAddresses()) sendTo(la.broadcast, "LFRQ1");
  for (const auto& ip : ask) sendTo(ip, "LFRQ1");
  for (const auto& ip : Neighbours()) sendTo(ip, "LFRQ1");

  const uint64_t deadline = NowUs() + static_cast<uint64_t>(timeoutMs) * 1000;
  while (NowUs() < deadline) {
    int remainingMs = static_cast<int>((deadline - NowUs()) / 1000);
    fd_set rd;
    FD_ZERO(&rd);
    FD_SET(s, &rd);
    timeval tv{remainingMs / 1000, (remainingMs % 1000) * 1000};
    if (select(0, &rd, nullptr, nullptr, &tv) <= 0) break;

    char buf[256];
    sockaddr_in from{};
    int fromLen = sizeof(from);
    int n = recvfrom(s, buf, sizeof(buf) - 1, 0, reinterpret_cast<sockaddr*>(&from), &fromLen);
    if (n <= 0) continue;
    buf[n] = '\0';

    std::string msg(buf);
    if (msg.rfind("LFRQ1", 0) == 0) {  // someone is looking: answer them directly
      if (!answer.empty()) {
        sendto(s, answer.data(), static_cast<int>(answer.size()), 0, reinterpret_cast<sockaddr*>(&from), fromLen);
      }
      continue;
    }
    const bool sender = msg.rfind("LFRS1|", 0) == 0;
    if (!sender && msg.rfind("LFRD1|", 0) != 0) continue;
    size_t bar = msg.find('|', 6);
    if (bar == std::string::npos) continue;
    FoundReceiver r;
    r.sender = sender;
    r.port = static_cast<uint16_t>(std::atoi(msg.substr(6, bar - 6).c_str()));
    r.name = msg.substr(bar + 1);
    const size_t fpBar = r.name.rfind('|');  // "|fp" since protocol v8
    if (fpBar != std::string::npos) {
      r.fp = r.name.substr(fpBar + 1);
      r.name.resize(fpBar);
    }
    char ip[INET_ADDRSTRLEN] = {};
    inet_ntop(AF_INET, &from.sin_addr, ip, sizeof(ip));
    r.ip = ip;
    bool dup = false;
    for (const auto& f : found) dup |= (f.ip == r.ip && f.sender == r.sender);
    if (!dup && r.port) found.push_back(r);
  }
  closesocket(s);
  return found;
}

}  // namespace lfrd
