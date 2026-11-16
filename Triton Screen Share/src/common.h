// Shared types, wire protocol and networking helpers for Triton Screen Share.
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace lfrd {

// Ports. Control is TCP; video/audio are RTP over UDP; discovery is UDP broadcast.
constexpr uint16_t kControlPort = 47990;
constexpr uint16_t kPullPort = 47991;  // TCP: "please share your screen with me" requests
constexpr uint16_t kDiscoveryPort = 47989;
constexpr uint16_t kVideoPort = 47992;
constexpr uint16_t kAudioPort = 47994;
constexpr uint16_t kMicPort = 47996;
constexpr uint16_t kCameraPort = 47998;  // UDP on the SENDER: the receiver's webcam, as H.264 RTP  // UDP on the SENDER: the receiver's microphone, as Opus RTP
// 8: paired, encrypted (secure.h); 9: media AES-256-GCM, not SRTP; 10: video FEC (fec.h), pairing only
// while "Pair a new PC" is open
constexpr uint32_t kProtocolVersion = 10;

enum class Codec : uint8_t { H264 = 0, H265 = 1 };

// Control-channel message types. Each message is framed as
// [uint16 payload length][uint8 type][payload].
enum MsgType : uint8_t {
  MSG_HELLO = 1,     // sender -> receiver: stream description
  MSG_READY,         // receiver -> sender: pipelines are listening
  MSG_STATS,         // receiver -> sender: network report (every 500 ms)
  MSG_KEYFRAME,      // receiver -> sender: picture is broken, send an IDR
  MSG_PING,          // sender -> receiver
  MSG_PONG,          // receiver -> sender (echoes the ping)
  MSG_STATUS,        // sender -> receiver: current bitrate / RTT for the overlay
  MSG_MOUSE_MOVE,    // receiver -> sender: input back-channel
  MSG_MOUSE_BUTTON,
  MSG_MOUSE_WHEEL,
  MSG_KEY,
  MSG_BYE,
  MSG_KEEPALIVE,     // receiver -> sender every ~20 ms: keeps the sender's Wi-Fi out of power save
  MSG_CURSOR,        // sender -> receiver: current cursor shape, so the receiver can draw it locally
  MSG_CURSOR_MODE,   // receiver -> sender: 1 = receiver draws the cursor, leave it out of the video
  MSG_SIZE,          // sender -> receiver: the shared screen's real size (extend mode sets it late)
  MSG_SET_MODE,      // receiver -> sender: switch to extend (1) or mirror (0)
  MSG_MODE,          // sender -> receiver: current mode and whether extend is possible there
  MSG_PULL,          // on kPullPort, showing PC -> sharing PC: share your screen with me
  MSG_PULL_REPLY,    // sharing PC -> showing PC: PullReply
  MSG_MIC,           // receiver -> sender: 1 = my mic is streaming to your kMicPort, 0 = stopped
  MSG_MIC_STATUS,    // sender -> receiver: MicStatus (can it offer the mic to its apps?)
  MSG_CAMERA,        // receiver -> sender: 1 = my webcam is streaming to your kCameraPort, 0 = stopped
  MSG_CAMERA_STATUS, // sender -> receiver: MicStatus layout (can it offer "Triton Camera"?)
  // The handshake (secure.cpp), before anything else on a new connection:
  MSG_KEX = 0xE0,    // both: device key + one-time key (in the clear: public keys only)
  MSG_KEX_CONFIRM,   // both: proof of having the same session keys
  MSG_PAIR,          // both, encrypted: 1 = already paired, or the user confirmed the code
  MSG_SEALED = 0xEE, // every message after the handshake: AES-256-GCM of [type][payload]
};

// Cursor shapes the receiver can reproduce; MSG_CURSOR carries an index into this table.
// Anything else (an app's custom cursor) is shown as the arrow.
inline const LPCWSTR kCursorShapes[] = {IDC_ARROW, IDC_IBEAM,  IDC_HAND,    IDC_WAIT,    IDC_APPSTARTING,
                                        IDC_CROSS, IDC_SIZEALL, IDC_SIZENWSE, IDC_SIZENESW, IDC_SIZEWE,
                                        IDC_SIZENS, IDC_NO,     IDC_HELP};
constexpr uint8_t kCursorShapeCount = static_cast<uint8_t>(sizeof(kCursorShapes) / sizeof(kCursorShapes[0]));

#pragma pack(push, 1)
struct Hello {
  uint32_t version;
  uint16_t width, height;
  uint8_t fps;
  uint8_t codec;
  uint8_t audio;
  char name[32];
};
struct Ready {
  uint16_t videoPort;
  uint16_t audioPort;  // 0 = receiver does not want audio
  // The receiver's screen, so extend mode can make the virtual screen exactly that size.
  uint16_t screenWidth, screenHeight, screenHz;
};
struct VideoSize {
  uint16_t width, height;
};
struct SetMode {
  uint8_t extend;
};
struct PullRequest {
  uint32_t version;
  uint16_t controlPort;  // where the asking PC listens (its "Show here")
  char name[32];
};
struct PullReply {
  uint8_t result;  // 0 declined, 1 accepted (the sharing PC connects next), 2 busy
};
struct MicStatus {
  uint8_t available;  // the sender has a virtual cable to play the mic into (VB-Audio cable, "Triton Mic Input")
  uint8_t active;     // it is playing the receiver's mic right now
};
struct ModeReport {
  uint8_t extended;
  uint8_t canExtend;  // the sender has the Virtual Display Driver
};
struct Stats {
  uint32_t recvKbps;
  uint16_t lossPermille;
  uint32_t jitterUs;
  uint16_t fps;
  uint32_t lostPackets;
  // Standing queue delay: how much later than the fastest-seen packet this report's least-delayed
  // packet arrived. Grows when the link (or this receiver) can't keep up with the bitrate.
  uint32_t queueDelayUs;
};
struct Ping {
  uint64_t t;
};
struct Status {
  uint32_t bitrateKbps;
  uint32_t rttUs;
  char encoder[24];
};
struct MouseMove {
  uint16_t x, y;  // 0..65535 across the shared monitor
};
struct MouseButton {
  uint8_t button;  // 0 left, 1 right, 2 middle, 3 X1, 4 X2
  uint8_t down;
};
struct MouseWheel {
  int16_t delta;
  uint8_t horizontal;
};
struct Key {
  uint16_t scan;
  uint8_t flags;  // bit0 = down, bit1 = extended key
};
struct Cursor {
  uint8_t shape;  // index into kCursorShapes
  uint8_t visible;
};
struct CursorMode {
  uint8_t local;
};
#pragma pack(pop)

struct SenderOptions {
  std::string to;
  uint16_t port = kControlPort;
  int monitor = -1;  // -1 = primary
  int fps = 60;
  int bitrate = 12000;  // kbps, starting point (low, so a slow link never starts with a queue)
  int minBitrate = 3000;
  int maxBitrate = 40000;
  Codec codec = Codec::H264;
  std::string encoder;  // empty = auto
  bool audio = true;
  bool extend = false;  // share a new virtual screen (extended desktop) instead of `monitor`
  bool adaptive = true;
  int seconds = 0;  // 0 = run until stopped
};

struct ReceiverOptions {
  uint16_t port = kControlPort;
  int jitterMs = 0;  // 0 = no jitter buffer (lowest latency); raise on very unstable Wi-Fi
  bool input = true;
  bool audio = true;
  bool fullscreen = false;
  std::string decoder;  // empty = auto
  int seconds = 0;
};

struct MonitorInfo {
  HMONITOR handle;
  RECT rect;
  bool primary;
  std::string device;
};

struct LocalAddr {
  std::string ip;
  std::string broadcast;
  std::string adapter;
};

struct FoundReceiver {
  std::string ip;
  std::string name;
  uint16_t port;
  bool sender = false;  // a PC offering its screen (port = kPullPort), not one showing screens
  std::string fp;       // first 16 hex chars of its device key fingerprint (see IsPaired)
};

// Set by Ctrl+C; the receiver also gets a WM_CLOSE posted to this window.
extern std::atomic<bool> g_stop;
extern std::atomic<HWND> g_notifyHwnd;

uint64_t NowUs();
bool InitWinsock();
std::string Narrow(const wchar_t* s);
std::string ComputerName();

class AeadKey;

class Connection {
 public:
  explicit Connection(SOCKET s);
  ~Connection();
  Connection(const Connection&) = delete;
  Connection& operator=(const Connection&) = delete;

  bool Send(uint8_t type, const void* data, uint16_t len);
  template <class T>
  bool Send(uint8_t type, const T& v) {
    return Send(type, &v, static_cast<uint16_t>(sizeof(T)));
  }
  // Blocks until one whole message arrives. False on disconnect.
  bool Recv(uint8_t& type, std::vector<uint8_t>& payload);
  bool WaitReadable(int timeoutMs);
  // Unblocks any thread waiting in Recv. The socket is closed by the destructor.
  void Shutdown();
  std::string PeerIp() const;
  // From now on every message is sealed (AES-256-GCM, a key per direction, the message count as
  // nonce); one that fails to open drops the connection. Called once, by SecureHandshake.
  void EnableEncryption(const uint8_t sendKey[32], const uint8_t recvKey[32]);

 private:
  SOCKET sock_;
  std::mutex sendMu_;
  std::unique_ptr<AeadKey> sendKey_, recvKey_;
  uint64_t sent_ = 0, received_ = 0;
};

SOCKET ConnectTcp(const std::string& host, uint16_t port, int timeoutMs);
SOCKET ListenTcp(uint16_t port);
void SetNoDelay(SOCKET s);

std::vector<LocalAddr> LocalAddresses();
// Receivers announce "LFRD1|port|name|fp"; idle PCs that can be asked to share announce "LFRS1|...".
// fp: the first 16 hex chars of the device key fingerprint, so auto-share can pick paired PCs.
std::string BeaconText(uint16_t port, const std::string& name, bool sender);
void SendBeacon(uint16_t port, const std::string& name, bool sender = false);
// Collects those for timeoutMs. It also asks ("LFRQ1") on every subnet, at `ask` and at the PCs in
// Windows' neighbour cache; a PC that hears it answers straight back with its `answer` (its beacon).
// An answer is a reply to our own packet, so it gets through firewalls that drop incoming broadcasts
// (e.g. company laptops).
std::vector<FoundReceiver> Discover(int timeoutMs, const std::string& answer = {},
                                    const std::vector<std::string>& ask = {});

std::vector<MonitorInfo> ListMonitors();

int RunSender(const SenderOptions& opt);
int RunReceiver(const ReceiverOptions& opt);
// The bubble + panel shown when the exe is started without arguments; openPanel = false (--background)
// starts with just the bubble, e.g. when Nova Launcher starts it at sign-in.
int RunLauncher(bool openPanel);
// Controls a running "show" mode from the panel: 'H' hide/show the picture, 'F' full screen/window,
// 'Q' end the current session (ignored when no session is on screen); 'M' mute/unmute the sound.
void ReceiverCommand(char command);
bool ReceiverMuted();
// True while another PC's screen is on show (not just waiting for one).
bool ReceiverInSession();
// The picture window is hidden (from the panel, "hide", or Ctrl+Alt+Shift+H).
bool ReceiverHidden();
// The receiver's mic, offered to the sharing PC's apps. available: that PC has a virtual cable.
bool ReceiverMicOn(bool* available);
void ReceiverToggleMic();
// The receiver's webcam, shown as "Triton Camera" on the sharing PC. available: that PC can.
bool ReceiverCameraOn(bool* available);
void ReceiverToggleCamera();
// Which microphone / webcam to share (MediaDevice::id; empty = Windows' default). Switches over at
// once if it is being shared right now.
void ReceiverSetMicDevice(const std::string& id);
void ReceiverSetCameraDevice(const std::string& id);
// The sharing PC's mode: true = extended desktop. canExtend: it has the Virtual Display Driver.
bool ReceiverExtended(bool* canExtend);
// Asks the sharing PC to switch mirror <-> extend (no-op if it can't extend).
void ReceiverSwitchMode();

}  // namespace lfrd
