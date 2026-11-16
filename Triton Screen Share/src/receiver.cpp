// "Show a screen here": wait for a sender, play its RTP stream with no buffering, report network
// stats back, and forward this device's mouse/keyboard to it while the window is focused.
#include <gst/gst.h>
#include <gst/video/videooverlay.h>
#include <objbase.h>

#include <algorithm>
#include <bitset>
#include <cmath>
#include <cstdio>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <set>
#include <thread>
#include <tuple>

#include "common.h"
#include "fec.h"
#include "gst_util.h"
#include "secure.h"

namespace lfrd {
namespace {

constexpr UINT WM_APP_START = WM_APP + 1;
constexpr UINT WM_APP_STOP = WM_APP + 2;
constexpr UINT WM_APP_HOTKEY = WM_APP + 3;
constexpr UINT WM_APP_CURSOR = WM_APP + 4;
constexpr UINT WM_APP_SIZE = WM_APP + 5;  // wParam x lParam: the shared screen's new size
constexpr UINT WM_APP_CAMERA_READY = WM_APP + 6;  // lParam: the opened webcam pipeline, or null
constexpr UINT WM_APP_MIC_READY = WM_APP + 7;     // lParam: MicResult*
// Audio playout buffer. It must outlast the longest Wi-Fi stall or the burst that follows the stall
// overflows it and gets dropped (measured with a stall simulator: 30 ms dropped 4%, 70 ms ~0). It
// starts small, grows on drops and shrinks back after a quiet spell.
constexpr int kAudioBufferMinMs = 40;
constexpr int kAudioBufferMaxMs = 160;
constexpr int kAudioBufferStepUpMs = 20;
constexpr int kAudioBufferStepDownMs = 10;
constexpr UINT_PTR kStatsTimer = 1;
constexpr UINT_PTR kHousekeepingTimer = 2;
constexpr wchar_t kWindowClass[] = L"TritonScreenShareReceiver";

struct State {
  ReceiverOptions opt;
  HWND hwnd = nullptr;
  SOCKET listenSock = INVALID_SOCKET;
  std::atomic<bool> quitting{false};
  uint64_t startedAt = 0;

  // Session hand-off between the network thread and the UI thread.
  HANDLE started = nullptr, stopped = nullptr;
  bool startOk = false;
  Hello hello{};
  std::string peer;
  std::mutex connMu;
  std::shared_ptr<Connection> conn;
  SessionKeys keys;  // this session's stream keys (SecureHandshake), set before the session starts

  // Pipelines (UI thread only).
  GstElement* video = nullptr;
  GstElement* audio = nullptr;
  bool inSession = false;
  uint64_t sessionStart = 0;
  std::string decoder;

  // Stats written from GStreamer streaming threads.
  std::atomic<uint64_t> bytes{0}, packets{0}, lost{0}, frames{0};
  std::atomic<uint64_t> audioPackets{0}, audioPlayed{0}, audioDrops{0};
  uint64_t prevAudioPackets = 0, prevAudioPlayed = 0, prevAudioDrops = 0, prevAudioDropsPrinted = 0;
  int audioBufferMs = kAudioBufferMinMs;
  // Audio watchdog (UI thread): restarts the audio pipeline if it errors or stops playing.
  uint64_t audioRestartAt = 0;   // 0 = no restart pending
  uint64_t audioStallSince = 0;  // packets arriving but nothing reaching the sound card since then
  uint64_t audioWatchPackets = 0, audioWatchPlayed = 0;
  int audioRestartDelayMs = 1000;
  uint64_t lastAudioDrop = 0, lastAudioChange = 0;
  std::atomic<uint32_t> jitterUs{0};
  std::atomic<uint64_t> lastKeyReq{0};
  int32_t udpSeq = -1, depaySeq = -1;
  double jitter = 0;
  int32_t lastTransit = 0;
  bool haveTransit = false;

  // Queueing delay (see Stats::queueDelayUs). "Transit" = arrival time minus the sender's RTP
  // timestamp, in 90 kHz ticks; its minimum over the last 30 s is the no-queue baseline, so
  // transit - baseline is how late a packet is. Arrival is when udpsrc reads the packet, so a
  // backlog in this PC's socket (decoder too slow) counts too.
  static constexpr int64_t kNoTransit = INT64_MAX;
  std::atomic<int64_t> intervalMinTransit{kNoTransit};  // streaming thread -> timer
  std::atomic<int32_t> baselineTransit{0};               // timer -> streaming thread
  std::atomic<bool> haveBaseline{false};
  // How late each frame's FIRST packet is: the backlog a new frame starts behind. (The rest of a big
  // frame is also late by its own sending time, which isn't a queue.)
  std::atomic<int64_t> frameLateSum{0};  // 90 kHz ticks
  std::atomic<uint32_t> frameLateCount{0};
  uint32_t lastFrameTs = 0;
  std::vector<int32_t> transitWindow;  // per-500 ms minima (UI thread)
  uint32_t queueDelayUs = 0;
  // Skipping ahead: when the picture is far behind, drop late packets until they are live again.
  std::atomic<bool> catchingUp{false};
  std::atomic<uint64_t> catchUpStart{0};
  std::atomic<uint32_t> skippedMs{0};
  // Freeze-instead-of-smear: after a lost packet, decoded frames are damaged until the next
  // keyframe; keep showing the last good frame meanwhile.
  std::atomic<bool> waitingForKeyframe{false};
  std::atomic<uint64_t> waitingSince{0};

  // Stats bookkeeping (UI thread).
  uint64_t prevTime = 0, prevBytes = 0, prevPackets = 0, prevLost = 0, prevFrames = 0;
  int tick = 0;
  Stats lastStats{};

  // Input forwarding (UI thread: hooks run on it).
  HHOOK kbHook = nullptr, msHook = nullptr;
  bool forwardInput = true;
  std::bitset<256> physDown;
  std::set<uint16_t> keysDown;
  uint8_t buttonsDown = 0;
  uint16_t lastMx = 0xFFFF, lastMy = 0xFFFF;

  // Low-level hooks stall every mouse movement on this PC until they return, so they never touch
  // the network: they queue events and InputThread sends them.
  struct QueuedInput {
    uint8_t type, len;
    uint8_t data[8];
  };
  std::mutex inputMu;
  std::vector<QueuedInput> inputQueue;
  HANDLE inputEvent = nullptr;

  // The sender's cursor shape; drawn locally while input is forwarded.
  std::atomic<uint8_t> cursorShape{0};
  std::atomic<bool> cursorVisible{true};

  // Muted: audio keeps streaming and decoding, only the sound card output is silenced, so
  // unmuting is instant. Kept across sessions.
  std::atomic<bool> muted{false};

  // Mirror/extend, as last reported by the sender.
  std::atomic<bool> senderExtended{false}, senderCanExtend{false};
  // This PC's microphone, streamed to the sender for its apps. Off until the user turns it on.
  GstElement* mic = nullptr;  // window thread only
  std::atomic<bool> micOn{false}, senderMicAvailable{false};
  // Window thread: the user wants it shared (on, still opening, or waiting to be reopened), and an
  // open is running on its own thread (OpenCameraAsync / OpenMicAsync).
  bool cameraWanted = false, cameraOpening = false, micWanted = false, micOpening = false;
  // This PC's webcam, streamed to the sender as "Triton Camera". Off until the user turns it on.
  GstElement* camera = nullptr;  // window thread only
  std::atomic<bool> cameraOn{false}, senderCameraAvailable{false};
  std::string micDeviceOpen;      // the mic g.mic was opened with ("" = Windows' default)
  bool micExclusive = false;      // ...in exclusive mode
  std::string micNeedsExclusive;  // a mic that refused shared mode: opened exclusively from then on
  // A webcam that stops (USB glitch, driver reset) is reopened a few times before giving up.
  int cameraRetries = 0;
  uint64_t cameraRetryAt = 0, cameraOpenedAt = 0;  // NowUs()
  std::mutex deviceMu;               // the panel picks these; the window thread opens them
  std::string micDevice, cameraDevice;
  // A switch restarts the sender's video with a new RTP stream (new SSRC, sequence numbers and
  // timestamps), so per-stream state starts over when the SSRC changes.
  uint32_t ssrc = 0;
  bool haveSsrc = false;
  std::atomic<bool> resetDelayWindow{false};

  bool fullscreen = false;
};

State g;

std::shared_ptr<Connection> CurrentConn() {
  std::lock_guard<std::mutex> lock(g.connMu);
  return g.conn;
}

template <class T>
void SendToSender(uint8_t type, const T& v) {
  if (auto c = CurrentConn()) c->Send(type, v);
}

// Called from the hooks. Consecutive moves collapse into the newest one, so a stalled network never
// builds a backlog of stale positions.
template <class T>
void QueueInput(uint8_t type, const T& v) {
  static_assert(sizeof(T) <= sizeof(State::QueuedInput::data));
  {
    std::lock_guard<std::mutex> lock(g.inputMu);
    if (type != MSG_MOUSE_MOVE || g.inputQueue.empty() || g.inputQueue.back().type != MSG_MOUSE_MOVE) {
      g.inputQueue.push_back({type, static_cast<uint8_t>(sizeof(T)), {}});
    }
    std::memcpy(g.inputQueue.back().data, &v, sizeof(T));
  }
  SetEvent(g.inputEvent);
}

void InputThread() {
  std::vector<State::QueuedInput> batch;
  while (!g.quitting) {
    WaitForSingleObject(g.inputEvent, 250);
    {
      std::lock_guard<std::mutex> lock(g.inputMu);
      batch.swap(g.inputQueue);
    }
    if (auto c = CurrentConn()) {
      for (const auto& e : batch) c->Send(e.type, e.data, e.len);
    }
    batch.clear();
  }
}

HCURSOR RemoteCursor() {
  return g.cursorVisible ? LoadCursorW(nullptr, kCursorShapes[g.cursorShape]) : nullptr;
}

void RequestKeyframe() {
  uint64_t now = NowUs();
  uint64_t prev = g.lastKeyReq.load();
  if (now - prev < 250'000) return;
  if (!g.lastKeyReq.compare_exchange_strong(prev, now)) return;
  if (auto c = CurrentConn()) c->Send(MSG_KEYFRAME, nullptr, 0);
}

// ---------------------------------------------------------------------------
// Pad probes

template <class Fn>
void ForEachBuffer(GstPadProbeInfo* info, Fn fn) {
  if (info->type & GST_PAD_PROBE_TYPE_BUFFER) {
    fn(GST_PAD_PROBE_INFO_BUFFER(info));
  } else if (info->type & GST_PAD_PROBE_TYPE_BUFFER_LIST) {
    GstBufferList* list = GST_PAD_PROBE_INFO_BUFFER_LIST(info);
    for (guint i = 0; i < gst_buffer_list_length(list); ++i) fn(gst_buffer_list_get(list, i));
  }
}

bool ReadRtpHeader(GstBuffer* buf, uint16_t& seq, uint32_t& ts, gsize& size, uint32_t* ssrc = nullptr) {
  uint8_t hdr[12];
  size = gst_buffer_get_size(buf);
  if (size < 12 || gst_buffer_extract(buf, 0, hdr, 12) != 12) return false;
  if ((hdr[0] >> 6) != 2) return false;  // not RTP v2: an FEC parity packet (fec.h)
  seq = static_cast<uint16_t>((hdr[2] << 8) | hdr[3]);
  ts = (uint32_t(hdr[4]) << 24) | (uint32_t(hdr[5]) << 16) | (uint32_t(hdr[6]) << 8) | hdr[7];
  if (ssrc) *ssrc = (uint32_t(hdr[8]) << 24) | (uint32_t(hdr[9]) << 16) | (uint32_t(hdr[10]) << 8) | hdr[11];
  return true;
}

constexpr int32_t kCatchUpEnterMs = 400;  // this far behind live: skip ahead
constexpr int32_t kCatchUpExitMs = 100;   // packets this fresh again: resume
constexpr uint64_t kCatchUpMaxUs = 3'000'000;  // then show late video rather than nothing...
constexpr uint64_t kCatchUpCooldownUs = 5'000'000;  // ...and let the sender's bitrate cut drain it

// Decides, per packet, whether the picture is so far behind that it should skip ahead. Returns true
// for packets to throw away.
bool CatchUpDrop(int32_t transit, uint64_t now) {
  if (!g.haveBaseline) return false;
  const int32_t lateMs = static_cast<int32_t>(transit - g.baselineTransit.load()) / 90;
  if (!g.catchingUp) {
    if (lateMs < kCatchUpEnterMs || now - g.catchUpStart < kCatchUpMaxUs + kCatchUpCooldownUs) return false;
    g.catchingUp = true;
    g.catchUpStart = now;
    g.skippedMs = static_cast<uint32_t>(lateMs);
    return true;
  }
  // Still stale: keep skipping, for at most kCatchUpMaxUs. (The late packets still count toward the
  // delay reports, so the sender keeps cutting its bitrate meanwhile.)
  if (lateMs > kCatchUpExitMs && now - g.catchUpStart < kCatchUpMaxUs) return true;
  g.catchingUp = false;
  return false;
}

// Raw network view: throughput, loss, RFC 3550 inter-arrival jitter and queueing delay.
GstPadProbeReturn UdpProbe(GstPad*, GstPadProbeInfo* info, gpointer) {
  bool drop = false;
  ForEachBuffer(info, [&drop](GstBuffer* buf) {
    uint16_t seq;
    uint32_t ts, ssrc;
    gsize size;
    if (!ReadRtpHeader(buf, seq, ts, size, &ssrc)) return;
    if (g.haveSsrc && ssrc != g.ssrc) {  // the sender restarted its video (mirror <-> extend)
      g.udpSeq = -1;
      g.haveTransit = false;
      g.haveBaseline = false;
      g.catchingUp = false;
      g.intervalMinTransit = State::kNoTransit;
      g.frameLateSum = 0;
      g.frameLateCount = 0;
      g.resetDelayWindow = true;
    }
    g.ssrc = ssrc;
    g.haveSsrc = true;
    g.bytes += size;
    g.packets++;
    if (g.udpSeq >= 0) {
      uint16_t gap = static_cast<uint16_t>(seq - static_cast<uint16_t>(g.udpSeq + 1));
      if (gap < 1000) {  // larger "gaps" are late or duplicate packets
        g.lost += gap;
        g.udpSeq = seq;
      }
    } else {
      g.udpSeq = seq;
    }
    const uint64_t now = NowUs();
    const uint32_t arrival = static_cast<uint32_t>(now * 9 / 100);  // 90 kHz clock
    const int32_t transit = static_cast<int32_t>(arrival - ts);
    if (g.haveTransit) {
      const double d = std::abs(static_cast<double>(transit - g.lastTransit));
      g.jitter += (d - g.jitter) / 16.0;
      g.jitterUs = static_cast<uint32_t>(g.jitter * 1e6 / 90000.0);
    }
    g.lastTransit = transit;
    g.haveTransit = true;
    int64_t cur = g.intervalMinTransit.load();
    while (transit < cur && !g.intervalMinTransit.compare_exchange_weak(cur, transit)) {
    }
    if (ts != g.lastFrameTs && g.haveBaseline) {
      g.frameLateSum += std::max(0, static_cast<int32_t>(transit - g.baselineTransit.load()));
      g.frameLateCount++;
    }
    g.lastFrameTs = ts;
    if (CatchUpDrop(transit, now)) drop = true;
  });
  return drop ? GST_PAD_PROBE_DROP : GST_PAD_PROBE_OK;
}

void StartWaitingForKeyframe() {
  if (!g.waitingForKeyframe.exchange(true)) g.waitingSince = NowUs();
  RequestKeyframe();
}

// Any gap in what reaches the depayloader means a damaged picture: freeze and ask for a keyframe.
GstPadProbeReturn DepayProbe(GstPad*, GstPadProbeInfo* info, gpointer) {
  ForEachBuffer(info, [](GstBuffer* buf) {
    uint16_t seq;
    uint32_t ts;
    gsize size;
    if (!ReadRtpHeader(buf, seq, ts, size)) return;
    if (g.depaySeq >= 0) {
      uint16_t gap = static_cast<uint16_t>(seq - static_cast<uint16_t>(g.depaySeq + 1));
      if (gap != 0 && gap < 1000) StartWaitingForKeyframe();
    }
    g.depaySeq = seq;
  });
  return GST_PAD_PROBE_OK;
}

// A complete keyframe (no DELTA_UNIT flag) leaving the parser repairs the picture.
GstPadProbeReturn KeyframeProbe(GstPad*, GstPadProbeInfo* info, gpointer) {
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (g.waitingForKeyframe && buf && !GST_BUFFER_FLAG_IS_SET(buf, GST_BUFFER_FLAG_DELTA_UNIT)) {
    g.waitingForKeyframe = false;
  }
  return GST_PAD_PROBE_OK;
}

// Decoded frames: shown, unless they are damaged ones waiting for that keyframe.
// (Counted either way: fps is decoder throughput, which the sender's rate control relies on.)
GstPadProbeReturn FrameProbe(GstPad*, GstPadProbeInfo*, gpointer) {
  g.frames++;
  return g.waitingForKeyframe ? GST_PAD_PROBE_DROP : GST_PAD_PROBE_OK;
}

// Counts into the std::atomic<uint64_t> passed as user data (audio diagnostics).
GstPadProbeReturn CountProbe(GstPad*, GstPadProbeInfo*, gpointer counter) {
  ++*static_cast<std::atomic<uint64_t>*>(counter);
  return GST_PAD_PROBE_OK;
}

// Counts audio packets the jitter buffer threw away (its drop-msg reports, rate-limited, carry the
// count since the previous report).
GstBusSyncReply AudioBusHandler(GstBus*, GstMessage* msg, gpointer) {
  const GstStructure* s = GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ELEMENT ? gst_message_get_structure(msg) : nullptr;
  if (!s || !gst_structure_has_name(s, "drop-msg")) return GST_BUS_PASS;
  guint late = 0, overflow = 0;
  gst_structure_get_uint(s, "num-too-late", &late);
  gst_structure_get_uint(s, "num-drop-on-latency", &overflow);
  g.audioDrops += late + overflow;
  gst_message_unref(msg);
  return GST_BUS_DROP;
}

void AddProbe(GstElement* pipeline, const char* element, const char* pad, GstPadProbeType type,
              GstPadProbeCallback cb) {
  GstElement* e = gst_bin_get_by_name(GST_BIN(pipeline), element);
  if (!e) return;
  GstPad* p = gst_element_get_static_pad(e, pad);
  if (p) {
    gst_pad_add_probe(p, type, cb, nullptr, nullptr);
    gst_object_unref(p);
  }
  gst_object_unref(e);
}

// ---------------------------------------------------------------------------
// Window helpers

// Maps a client-area point to 0..65535 coordinates inside the letterboxed video.
bool MapToVideo(POINT pt, uint16_t& nx, uint16_t& ny) {
  RECT rc;
  GetClientRect(g.hwnd, &rc);
  const double cw = rc.right, ch = rc.bottom;
  const double vw = g.hello.width, vh = g.hello.height;
  if (cw <= 0 || ch <= 0 || vw <= 0 || vh <= 0) return false;
  const double scale = std::min(cw / vw, ch / vh);
  const double dw = vw * scale, dh = vh * scale;
  double fx = (pt.x - (cw - dw) / 2) / dw;
  double fy = (pt.y - (ch - dh) / 2) / dh;
  const bool inside = fx >= 0 && fx <= 1 && fy >= 0 && fy <= 1;
  fx = std::clamp(fx, 0.0, 1.0);
  fy = std::clamp(fy, 0.0, 1.0);
  nx = static_cast<uint16_t>(std::lround(fx * 65535));
  ny = static_cast<uint16_t>(std::lround(fy * 65535));
  return inside;
}

// The video window has no title bar or borders at all: either it covers the whole monitor, or it is
// a centered picture at up to 85% of the screen. The launcher's bubble hides/shows and resizes it.
void ApplyWindowSize() {
  MONITORINFO mi{sizeof(mi)};
  GetMonitorInfoW(MonitorFromWindow(g.hwnd, MONITOR_DEFAULTTOPRIMARY), &mi);
  if (g.fullscreen) {
    const RECT& m = mi.rcMonitor;
    SetWindowPos(g.hwnd, HWND_TOPMOST, m.left, m.top, m.right - m.left, m.bottom - m.top,
                 SWP_NOOWNERZORDER | SWP_NOACTIVATE);  // resizing never takes the focus from the panel
    return;
  }
  const RECT& work = mi.rcWork;
  const double maxW = (work.right - work.left) * 0.85, maxH = (work.bottom - work.top) * 0.85;
  const double scale = std::min({1.0, maxW / g.hello.width, maxH / g.hello.height});
  const int w = static_cast<int>(g.hello.width * scale), h = static_cast<int>(g.hello.height * scale);
  SetWindowPos(g.hwnd, HWND_TOPMOST, work.left + ((work.right - work.left) - w) / 2,
               work.top + ((work.bottom - work.top) - h) / 2, w, h, SWP_NOOWNERZORDER | SWP_NOACTIVATE);
}

void ToggleFullscreen() {
  g.fullscreen = !g.fullscreen;
  ApplyWindowSize();
  if (IsWindowVisible(g.hwnd)) SetForegroundWindow(g.hwnd);  // the panel that sent this just closed
}

void ReleaseAllInput();

// Hidden while a session runs = "minimized to the bubble"; the stream keeps playing underneath.
void ToggleVisible() {
  if (IsWindowVisible(g.hwnd)) {
    ReleaseAllInput();
    ShowWindow(g.hwnd, SW_HIDE);
  } else {
    // Back on top of whatever was opened meanwhile (e.g. Chrome), without taking the focus: the
    // panel that asked stays open on top. Clicking the picture gives it the keyboard and mouse.
    SetWindowPos(g.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE);
  }
}

// ---------------------------------------------------------------------------
// Input forwarding via low-level hooks (they see keys like Alt+Tab and Win too).

void ReleaseAllInput() {
  for (uint16_t id : g.keysDown) {
    Key k{static_cast<uint16_t>(id & 0xFF), static_cast<uint8_t>((id & 0x100) ? 2 : 0)};
    QueueInput(MSG_KEY, k);
  }
  g.keysDown.clear();
  for (uint8_t b = 0; b < 5; ++b) {
    if (g.buttonsDown & (1 << b)) QueueInput(MSG_MOUSE_BUTTON, MouseButton{b, 0});
  }
  g.buttonsDown = 0;
}

bool OurWindowFocused() { return GetForegroundWindow() == g.hwnd; }

LRESULT CALLBACK KeyboardHook(int code, WPARAM wp, LPARAM lp) {
  if (code == HC_ACTION) {
    const auto* k = reinterpret_cast<const KBDLLHOOKSTRUCT*>(lp);
    if (!(k->flags & LLKHF_INJECTED)) {
      const bool down = wp == WM_KEYDOWN || wp == WM_SYSKEYDOWN;
      if (k->vkCode < 256) g.physDown[k->vkCode] = down;
      if (g.inSession && OurWindowFocused()) {
        const bool ctrl = g.physDown[VK_LCONTROL] || g.physDown[VK_RCONTROL];
        const bool alt = g.physDown[VK_LMENU] || g.physDown[VK_RMENU];
        const bool shift = g.physDown[VK_LSHIFT] || g.physDown[VK_RSHIFT];
        if (down && ctrl && alt && shift && (k->vkCode == 'I' || k->vkCode == 'F' || k->vkCode == 'Q' || k->vkCode == 'M')) {
          if (k->vkCode == 'M') ReceiverCommand('M');
          else PostMessageW(g.hwnd, WM_APP_HOTKEY, k->vkCode, 0);
          return 1;
        }
        if (!g.forwardInput && down && k->vkCode == VK_F11) {
          PostMessageW(g.hwnd, WM_APP_HOTKEY, 'F', 0);
          return 1;
        }
        if (g.forwardInput) {
          const bool ext = (k->flags & LLKHF_EXTENDED) != 0;
          const uint16_t id = static_cast<uint16_t>((k->scanCode & 0xFF) | (ext ? 0x100 : 0));
          if (down) {
            g.keysDown.insert(id);
          } else if (!g.keysDown.erase(id)) {
            return CallNextHookEx(nullptr, code, wp, lp);  // its key-down went to this PC
          }
          Key key{static_cast<uint16_t>(k->scanCode), static_cast<uint8_t>((down ? 1 : 0) | (ext ? 2 : 0))};
          QueueInput(MSG_KEY, key);
          return 1;  // swallow so the receiver PC doesn't act on it
        }
      }
    }
  }
  return CallNextHookEx(nullptr, code, wp, lp);
}

LRESULT CALLBACK MouseHook(int code, WPARAM wp, LPARAM lp) {
  if (code == HC_ACTION && g.inSession && g.forwardInput) {
    const auto* m = reinterpret_cast<const MSLLHOOKSTRUCT*>(lp);
    if (!(m->flags & LLMHF_INJECTED) && OurWindowFocused()) {
      POINT pt = m->pt;
      ScreenToClient(g.hwnd, &pt);
      uint16_t nx, ny;
      const bool inside = MapToVideo(pt, nx, ny) && WindowFromPoint(m->pt) == g.hwnd;
      auto sendMove = [&] {
        if (nx != g.lastMx || ny != g.lastMy) {
          QueueInput(MSG_MOUSE_MOVE, MouseMove{nx, ny});
          g.lastMx = nx;
          g.lastMy = ny;
        }
      };
      int button = -1;
      bool down = false;
      switch (wp) {
        case WM_MOUSEMOVE:
          if (inside || g.buttonsDown) sendMove();
          break;
        case WM_LBUTTONDOWN: button = 0; down = true; break;
        case WM_LBUTTONUP: button = 0; break;
        case WM_RBUTTONDOWN: button = 1; down = true; break;
        case WM_RBUTTONUP: button = 1; break;
        case WM_MBUTTONDOWN: button = 2; down = true; break;
        case WM_MBUTTONUP: button = 2; break;
        case WM_XBUTTONDOWN:
        case WM_XBUTTONUP:
          button = HIWORD(m->mouseData) == XBUTTON1 ? 3 : 4;
          down = wp == WM_XBUTTONDOWN;
          break;
        case WM_MOUSEWHEEL:
        case WM_MOUSEHWHEEL:
          if (inside) {
            QueueInput(MSG_MOUSE_WHEEL, MouseWheel{static_cast<int16_t>(HIWORD(m->mouseData)),
                                                     static_cast<uint8_t>(wp == WM_MOUSEHWHEEL)});
            return 1;
          }
          break;
      }
      if (button >= 0) {
        const uint8_t bit = static_cast<uint8_t>(1 << button);
        if (down && inside) {
          sendMove();
          QueueInput(MSG_MOUSE_BUTTON, MouseButton{static_cast<uint8_t>(button), 1});
          g.buttonsDown |= bit;
        } else if (!down && (g.buttonsDown & bit)) {
          sendMove();
          QueueInput(MSG_MOUSE_BUTTON, MouseButton{static_cast<uint8_t>(button), 0});
          g.buttonsDown &= static_cast<uint8_t>(~bit);
        }
      }
    }
  }
  return CallNextHookEx(nullptr, code, wp, lp);
}

// ---------------------------------------------------------------------------
// Session lifecycle (UI thread)

void StopPipeline(GstElement*& p) {
  if (!p) return;
  gst_element_set_state(p, GST_STATE_NULL);
  gst_object_unref(p);
  p = nullptr;
}

GstElement* Launch(const std::string& desc, const char* label) {
  GError* err = nullptr;
  GstElement* p = gst_parse_launch(desc.c_str(), &err);
  if (err) {
    std::fprintf(stderr, "[%s] could not build pipeline: %s\n", label, err->message);
    g_clear_error(&err);
    if (p) gst_object_unref(p);
    return nullptr;
  }
  return p;
}

// Just the sender's name: the numbers are in the console / launcher activity box, not on the picture.
void UpdateTitle() {
  wchar_t title[128];
  swprintf_s(title, L"%S - Triton Screen Share", g.hello.name);
  SetWindowTextW(g.hwnd, title);
}

// Window thread only (it owns the pipelines). Mutes in our own volume element rather than the
// sound card's mute: Windows can recreate the audio device while it sits idle (e.g. muted and hidden
// for a while), and a device-level mute flag doesn't reliably survive that.
void ApplyMute() {
  if (!g.audio) return;
  if (GstElement* vol = gst_bin_get_by_name(GST_BIN(g.audio), "avol")) {
    g_object_set(vol, "mute", g.muted ? TRUE : FALSE, nullptr);
    gst_object_unref(vol);
  }
}

void StopAudio() { StopPipeline(g.audio); }

// ---------------------------------------------------------------------------
// This PC's webcam and mic, for the sharing PC's apps. Opening a webcam or mic (and an encoder) takes
// a second or two, so it runs on a thread of its own: the window thread only gets the finished
// pipeline (WM_APP_CAMERA_READY / WM_APP_MIC_READY) and the picture never freezes meanwhile.

struct MediaKey {
  uint8_t bytes[kSrtpKeyBytes];
};

// One long-lived thread runs the opens, with COM initialised for good: a Media Foundation webcam
// opened on a short-lived thread without it started, then stopped with a stream error.
void RunOnMediaThread(std::function<void()> job) {
  static std::mutex mu;
  static std::condition_variable cv;
  static std::deque<std::function<void()>> jobs;
  static std::once_flag started;
  std::call_once(started, [] {
    std::thread([] {
      CoInitializeEx(nullptr, COINIT_MULTITHREADED);
      for (;;) {
        std::function<void()> next;
        {
          std::unique_lock<std::mutex> lock(mu);
          cv.wait(lock, [] { return !jobs.empty(); });
          next = std::move(jobs.front());
          jobs.pop_front();
        }
        next();
      }
    }).detach();
  });
  {
    std::lock_guard<std::mutex> lock(mu);
    jobs.push_back(std::move(job));
  }
  cv.notify_one();
}

// Webcam -> 1280x720 H.264 to the sender's kCameraPort, shown there as "Triton Camera". 6 Mbps at a
// balanced encoder preset (the other PC's apps compress it again, so it must arrive clean), and a
// keyframe every second so the other PC can join at any moment.
GstElement* BuildCamera(const std::string& device, const std::string& peer, const MediaKey& key) {
  const std::string enc = ChooseEncoder(Codec::H264, "");
  if (enc.empty()) return nullptr;
  // The webcam's 720p at 30 fps first: many also offer raw 720p at only 10 fps, or small modes the
  // pipeline would otherwise be free to pick and scale up.
  GstElement* p = Launch("mfvideosrc name=camsrc ! capsfilter caps=\"video/x-raw,width=1280,height=720,framerate=30/1;"
                         "video/x-raw,framerate=30/1;video/x-raw\" ! videoconvert ! videoscale ! "
                         "video/x-raw,format=NV12,width=1280,height=720 ! " +
                             enc + " name=cenc ! h264parse config-interval=-1 ! "
                             "rtph264pay pt=99 mtu=1200 config-interval=-1 aggregate-mode=zero-latency ! "
                             "udpsink name=cout host=" + peer + " port=" + std::to_string(kCameraPort) +
                             " sync=false async=false qos-dscp=34",
                         "camera");
  if (!p) return nullptr;
  if (!device.empty()) {
    GstElement* src = gst_bin_get_by_name(GST_BIN(p), "camsrc");
    g_object_set(src, "device-path", device.c_str(), nullptr);
    gst_object_unref(src);
  }
  GstElement* e = gst_bin_get_by_name(GST_BIN(p), "cenc");
  ConfigureEncoder(e, enc, 6000, 30);
  TrySet(e, "target-usage", "4");  // QuickSync: balanced, not the fastest preset the screen uses
  TrySet(e, "gop-size", "30");
  TrySet(e, "key-int-max", "30");
  gst_object_unref(e);
  EncryptRtp(p, "cout", "sink", key.bytes);
  if (gst_element_set_state(p, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
    PollBus(p, "camera");
    StopPipeline(p);
  }
  return p;
}

void OpenCameraAsync() {
  if (g.cameraOpening) return;
  g.cameraOpening = true;
  std::string device;
  {
    std::lock_guard<std::mutex> lock(g.deviceMu);
    device = g.cameraDevice;
  }
  MediaKey key;
  std::memcpy(key.bytes, g.keys.camera, kSrtpKeyBytes);
  const std::string peer = g.peer;
  const HWND hwnd = g.hwnd;
  RunOnMediaThread([device, peer, key, hwnd] {
    GstElement* p = BuildCamera(device, peer, key);
    if (!PostMessageW(hwnd, WM_APP_CAMERA_READY, 0, reinterpret_cast<LPARAM>(p)) && p) StopPipeline(p);
  });
}

// A webcam that stopped or didn't open again: up to 3 more tries, 2 s apart.
void CameraRetryOrGiveUp(const char* what) {
  if (g.cameraRetries < 3) {
    ++g.cameraRetries;
    g.cameraRetryAt = NowUs() + 2'000'000;
    std::printf("%s; trying again in 2 s (%d of 3).\n", what, g.cameraRetries);
  } else {
    g.cameraOn = false;
    g.cameraWanted = false;
    SendToSender(MSG_CAMERA, uint8_t{0});
    std::printf("Camera didn't come back: check it, then turn camera sharing on again.\n");
  }
}

void OnCameraReady(GstElement* p) {
  g.cameraOpening = false;
  if (!g.cameraWanted || !g.inSession) {  // turned off, or the session ended, meanwhile
    if (p) StopPipeline(p);
    return;
  }
  if (p) {
    g.camera = p;
    g.cameraOpenedAt = NowUs();
    if (g.cameraOn) {
      std::printf("Camera is back.\n");
    } else {
      g.cameraOn = true;
      SendToSender(MSG_CAMERA, uint8_t{1});
      std::printf("Camera sharing ON: the other PC's apps can use this camera.\n");
    }
  } else if (!g.cameraOn) {  // the first open
    g.cameraWanted = false;
    std::fprintf(stderr, "Couldn't open this PC's camera.\n");
  } else {
    CameraRetryOrGiveUp("Camera still unavailable");
  }
}

void ToggleCamera() {
  if (g.cameraWanted) {  // on, still opening, or waiting to be reopened
    StopPipeline(g.camera);
    if (g.cameraOn) SendToSender(MSG_CAMERA, uint8_t{0});
    g.cameraOn = false;
    g.cameraWanted = false;
    g.cameraRetryAt = 0;
    std::printf("Camera sharing off.\n");
    return;
  }
  if (!g.inSession || !g.senderCameraAvailable) return;
  g.cameraRetries = 0;
  g.cameraWanted = true;
  std::printf("Opening the camera...\n");
  OpenCameraAsync();
}

// Mic -> Opus voice to the sender's kMicPort (played there into its virtual cable, so its apps see it
// as a microphone). Shared mode, locked to the mic's own shared format (left open, the pipeline could
// pick another format wasapi2src advertises but can't open). Some USB webcam mics (a Rapoo) refuse
// shared mode whatever the format (AUDCLNT_E_UNSUPPORTED_FORMAT) but work exclusively: `exclusive`
// opens it that way, so only this app can use it meanwhile. (WASAPI ids need no escaping.)
GstElement* BuildMic(const std::string& device, bool exclusive, const std::string& peer, const MediaKey& key) {
  const std::string deviceArg = device.empty() ? std::string() : " device=\"" + device + "\"";
  const char* name = device.empty() ? "(Windows default)" : device.c_str();
  std::string source;
  if (exclusive) {
    source = "wasapisrc name=micsrc exclusive=true" + deviceArg;
    std::printf("Opening the microphone %s exclusively\n", name);
  } else {
    std::string format = MicFormat(device);
    std::printf("Opening the microphone %s (%s)\n", name, format.empty() ? "format unknown" : format.c_str());
    if (format.empty()) format = "audio/x-raw,rate=48000";
    source = "wasapi2src name=micsrc low-latency=true" + deviceArg + " ! " + format;
  }
  // The queue lets capture run on its own thread: without it a busy moment in converting/encoding made
  // the mic's ring buffer overflow ("Can't record audio fast enough", many times a second on the TV).
  GstElement* p = Launch(source + " ! queue max-size-time=100000000 max-size-buffers=0 max-size-bytes=0 leaky=downstream ! "
                             "audioconvert ! audioresample ! "
                             "audio/x-raw,rate=48000,channels=1 ! opusenc frame-size=10 bitrate=64000 ! rtpopuspay pt=98 ! "
                             "udpsink name=mout host=" + peer + " port=" + std::to_string(kMicPort) +
                             " sync=false async=false qos-dscp=46",
                         "mic");
  if (!p) return nullptr;
  EncryptRtp(p, "mout", "sink", key.bytes);
  if (gst_element_set_state(p, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
    PollBus(p, "mic");
    StopPipeline(p);
  }
  return p;
}

struct MicResult {
  GstElement* pipeline;
  std::string device;
  bool exclusive;
};

void OpenMicAsync(const std::string& device, bool exclusive) {
  if (g.micOpening) return;
  g.micOpening = true;
  MediaKey key;
  std::memcpy(key.bytes, g.keys.mic, kSrtpKeyBytes);
  const std::string peer = g.peer;
  const HWND hwnd = g.hwnd;
  RunOnMediaThread([device, exclusive, peer, key, hwnd] {
    auto* r = new MicResult{BuildMic(device, exclusive, peer, key), device, exclusive};
    if (!PostMessageW(hwnd, WM_APP_MIC_READY, 0, reinterpret_cast<LPARAM>(r))) {
      if (r->pipeline) StopPipeline(r->pipeline);
      delete r;
    }
  });
}

void MicGiveUp(const char* why) {
  g.micWanted = false;
  if (g.micOn) SendToSender(MSG_MIC, uint8_t{0});
  g.micOn = false;
  std::printf("%s\n", why);
}

void OnMicReady(std::unique_ptr<MicResult> r) {
  g.micOpening = false;
  if (!g.micWanted || !g.inSession) {  // turned off, or the session ended, meanwhile
    if (r->pipeline) StopPipeline(r->pipeline);
    return;
  }
  if (r->pipeline) {
    g.mic = r->pipeline;
    g.micDeviceOpen = r->device;
    g.micExclusive = r->exclusive;
    if (!g.micOn) {
      g.micOn = true;
      SendToSender(MSG_MIC, uint8_t{1});
      std::printf("Mic sharing ON: the other PC's apps can use this microphone.\n");
    }
  } else if (!r->exclusive && !r->device.empty()) {
    OpenMicAsync(r->device, true);  // didn't even start in shared mode: try exclusively
  } else {
    MicGiveUp("Couldn't open this PC's microphone.");
  }
}

void ToggleMic() {
  if (g.micWanted) {  // on, or still opening
    StopPipeline(g.mic);
    if (g.micOn) SendToSender(MSG_MIC, uint8_t{0});
    g.micOn = false;
    g.micWanted = false;
    std::printf("Mic sharing off.\n");
    return;
  }
  if (!g.inSession || !g.senderMicAvailable) return;
  std::string device;
  {
    std::lock_guard<std::mutex> lock(g.deviceMu);
    device = g.micDevice;
  }
  g.micWanted = true;
  // A mic that needed exclusive mode before goes straight to it.
  OpenMicAsync(device, !device.empty() && device == g.micNeedsExclusive);
}

// Builds and starts the audio pipeline. Used at session start and by the watchdog to recover.
bool StartAudio() {
  // sync=false: play each packet the moment it is decoded. Clock-synced playback (the default)
  // was measured to resync the sink ~30x/s and overflow the jitter buffer, i.e. no audible sound.
  // The sound card's small ring buffer absorbs arrival jitter; drop-on-latency bounds the delay if
  // the two devices' audio clocks drift apart.
  // do-lost: a packet that never comes is concealed by opusdec instead of leaving a gap.
  g.audioBufferMs = kAudioBufferMinMs;
  g.audio = Launch("udpsrc name=asrc port=" + std::to_string(kAudioPort) +
                       " caps=\"application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=97\" ! "
                       "rtpjitterbuffer name=ajb latency=" + std::to_string(g.audioBufferMs) +
                       " drop-on-latency=true do-lost=true post-drop-messages=true ! rtpopusdepay ! "
                       "opusdec plc=true ! audioconvert ! audioresample ! volume name=avol ! "
                       "wasapi2sink name=asink low-latency=true sync=false",
                   "audio");
  if (!g.audio) return false;
  DecryptRtp(g.audio, "asrc", "src", g.keys.audio);
  ApplyMute();
  GstBus* bus = gst_element_get_bus(g.audio);
  gst_bus_set_sync_handler(bus, AudioBusHandler, nullptr, nullptr);
  gst_object_unref(bus);
  g.lastAudioDrop = g.lastAudioChange = NowUs();
  for (auto [element, pad, counter] : {std::tuple{"asrc", "src", &g.audioPackets}, std::tuple{"asink", "sink", &g.audioPlayed}}) {
    GstElement* e = gst_bin_get_by_name(GST_BIN(g.audio), element);
    if (!e) continue;
    GstPad* p = gst_element_get_static_pad(e, pad);
    gst_pad_add_probe(p, GST_PAD_PROBE_TYPE_BUFFER, CountProbe, counter, nullptr);
    gst_object_unref(p);
    gst_object_unref(e);
  }
  if (gst_element_set_state(g.audio, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
    PollBus(g.audio, "audio");
    StopPipeline(g.audio);
    return false;
  }
  g.audioStallSince = 0;
  g.audioWatchPackets = g.audioPackets;
  g.audioWatchPlayed = g.audioPlayed;
  return true;
}

// Runs every 500 ms. Sound must recover by itself: the pipeline used to be stopped for good on its
// first error. Two failure signs: a bus error, or packets arriving while nothing reaches the sound
// card for 1.5 s (a stuck device). Either way, rebuild it; retries back off up to 10 s.
// Test hook: LFRD_TEST_AUDIO_FAULT=error|stall breaks the audio once, 5 s into the session, so the
// watchdog's recovery can be checked without unplugging a sound card.
GstPadProbeReturn BlockProbe(GstPad*, GstPadProbeInfo*, gpointer) { return GST_PAD_PROBE_OK; }

void InjectAudioFaultForTest(uint64_t now) {
  static bool done = false;
  const char* fault = std::getenv("LFRD_TEST_AUDIO_FAULT");
  if (done || !fault || !g.audio || now - g.sessionStart < 5'000'000) return;
  done = true;
  std::printf("[test] injecting audio %s\n", fault);
  if (std::string(fault) == "error") {
    GError* err = g_error_new_literal(GST_STREAM_ERROR, GST_STREAM_ERROR_FAILED, "injected test error");
    gst_element_post_message(g.audio, gst_message_new_error(GST_OBJECT(g.audio), err, "test"));
    g_error_free(err);
  } else if (GstElement* sink = gst_bin_get_by_name(GST_BIN(g.audio), "asink")) {  // stuck device
    GstPad* pad = gst_element_get_static_pad(sink, "sink");
    gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BLOCK_DOWNSTREAM, BlockProbe, nullptr, nullptr);
    gst_object_unref(pad);
    gst_object_unref(sink);
  }
}

void AudioWatchdog(uint64_t now) {
  if (!g.opt.audio || !g.hello.audio) return;
  InjectAudioFaultForTest(now);
  if (g.audio) {
    bool restart = !PollBus(g.audio, "audio");
    const uint64_t packets = g.audioPackets, played = g.audioPlayed;
    if (packets != g.audioWatchPackets && played == g.audioWatchPlayed) {
      if (!g.audioStallSince) g.audioStallSince = now;
      if (now - g.audioStallSince > 1'500'000) restart = true;
    } else {
      g.audioStallSince = 0;
      if (played != g.audioWatchPlayed) g.audioRestartDelayMs = 1000;  // healthy again
    }
    g.audioWatchPackets = packets;
    g.audioWatchPlayed = played;
    if (restart) {
      std::printf("Sound stopped playing; restarting audio.\n");
      StopAudio();
      g.audioRestartAt = now;  // first retry right away
    }
    return;
  }
  if (g.audioRestartAt && now >= g.audioRestartAt) {
    if (StartAudio()) {
      g.audioRestartAt = 0;
      std::printf("Audio restarted.\n");
    } else {
      g.audioRestartAt = now + static_cast<uint64_t>(g.audioRestartDelayMs) * 1000;
      g.audioRestartDelayMs = std::min(g.audioRestartDelayMs * 2, 10'000);
    }
  }
}

bool StartSession() {
  RegisterFecElements();
  const Codec codec = static_cast<Codec>(g.hello.codec);
  g.decoder = g.opt.decoder.empty() ? ChooseDecoder(codec) : g.opt.decoder;
  if (g.decoder.empty()) {
    std::fprintf(stderr, "No decoder available for this stream.\n");
    return false;
  }
  const bool h264 = codec == Codec::H264;
  // No buffering anywhere: packets go straight to the depayloader and the sink renders the moment a
  // frame is decoded. A jitter buffer (measured: it holds packets ~its full latency) is opt-in.
  std::string desc =
      "udpsrc name=vsrc port=" + std::to_string(kVideoPort) +
      // 1 MB holds a burst or a keyframe; a 4 MB buffer could hold seconds of backlog.
      " buffer-size=1048576 caps=\"application/x-rtp,media=video,clock-rate=90000,encoding-name=" +
      (h264 ? "H264" : "H265") + ",payload=96\" ! tritonfecdec name=fecdec ! " +  // rebuilds losses, decrypts (fec.h)
      (g.opt.jitterMs > 0 ? "rtpjitterbuffer latency=" + std::to_string(g.opt.jitterMs) + " drop-on-latency=true ! "
                          : std::string()) +
      (h264 ? "rtph264depay name=depay ! h264parse name=parse ! " : "rtph265depay name=depay ! h265parse name=parse ! ") +
      // The sink waits for the monitor's refresh before taking the next frame, so without this queue
      // decoding (and reading the network) could never run faster than the display: every hiccup
      // left a backlog that never drained, and it crept up to seconds. A 1-frame leaky queue lets
      // decoding run at network speed and the screen always gets the newest frame.
      g.decoder + " name=dec ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
      "d3d11videosink name=vsink sync=false force-aspect-ratio=true";
  g.video = Launch(desc, "video");
  if (!g.video) return false;
  GstElement* fec = gst_bin_get_by_name(GST_BIN(g.video), "fecdec");
  ConfigureFec(fec, g.keys.video);
  if (fec) gst_object_unref(fec);

  GstElement* sink = gst_bin_get_by_name(GST_BIN(g.video), "vsink");
  gst_video_overlay_set_window_handle(GST_VIDEO_OVERLAY(sink), reinterpret_cast<guintptr>(g.hwnd));
  gst_object_unref(sink);
  GstElement* dec = gst_bin_get_by_name(GST_BIN(g.video), "dec");
  TrySet(dec, "max-errors", "-1");  // never give up on a damaged frame; a keyframe will fix it
  TrySet(dec, "thread-type", "slice");  // libav: frame threading would add a frame of delay per thread
  gst_object_unref(dec);

  const auto bufferTypes = static_cast<GstPadProbeType>(GST_PAD_PROBE_TYPE_BUFFER | GST_PAD_PROBE_TYPE_BUFFER_LIST);
  AddProbe(g.video, "vsrc", "src", bufferTypes, UdpProbe);
  AddProbe(g.video, "depay", "sink", bufferTypes, DepayProbe);
  AddProbe(g.video, "parse", "src", GST_PAD_PROBE_TYPE_BUFFER, KeyframeProbe);
  AddProbe(g.video, "dec", "src", GST_PAD_PROBE_TYPE_BUFFER, FrameProbe);

  g.bytes = g.packets = g.lost = g.frames = 0;
  g.jitterUs = 0;
  g.udpSeq = g.depaySeq = -1;
  g.jitter = 0;
  g.haveTransit = false;
  g.intervalMinTransit = State::kNoTransit;
  g.haveBaseline = false;
  g.transitWindow.clear();
  g.queueDelayUs = 0;
  g.catchingUp = false;
  g.catchUpStart = 0;
  g.haveSsrc = false;
  g.senderExtended = false;
  g.senderCanExtend = false;
  g.senderMicAvailable = false;
  g.senderCameraAvailable = false;
  g.waitingForKeyframe = true;  // joining mid-stream: show nothing until a clean keyframe
  g.waitingSince = NowUs();
  g.prevBytes = g.prevPackets = g.prevLost = g.prevFrames = 0;
  g.prevTime = g.sessionStart = NowUs();
  g.lastStats = {};
  g.forwardInput = g.opt.input;
  g.lastMx = g.lastMy = 0xFFFF;

  g.fullscreen = g.opt.fullscreen;
  ApplyWindowSize();
  ShowWindow(g.hwnd, SW_SHOW);
  SetForegroundWindow(g.hwnd);

  if (gst_element_set_state(g.video, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
    PollBus(g.video, "video");
    StopPipeline(g.video);
    ShowWindow(g.hwnd, SW_HIDE);
    return false;
  }

  g.audioPackets = g.audioPlayed = g.audioDrops = 0;
  g.prevAudioPackets = g.prevAudioPlayed = g.prevAudioDrops = g.prevAudioDropsPrinted = 0;
  g.audioRestartAt = 0;
  g.audioRestartDelayMs = 1000;
  if (g.opt.audio && g.hello.audio && !StartAudio()) {
    std::fprintf(stderr, "Audio unavailable for now; will keep retrying.\n");
    g.audioRestartAt = NowUs() + 1'000'000;
  }

  if (g.opt.input) {
    g.kbHook = SetWindowsHookExW(WH_KEYBOARD_LL, KeyboardHook, GetModuleHandleW(nullptr), 0);
    g.msHook = SetWindowsHookExW(WH_MOUSE_LL, MouseHook, GetModuleHandleW(nullptr), 0);
  }
  SetTimer(g.hwnd, kStatsTimer, 500, nullptr);
  g.inSession = true;
  UpdateTitle();
  return true;
}

void StopSession() {
  if (!g.inSession) return;
  ReleaseAllInput();
  g.inSession = false;
  KillTimer(g.hwnd, kStatsTimer);
  if (g.kbHook) UnhookWindowsHookEx(g.kbHook);
  if (g.msHook) UnhookWindowsHookEx(g.msHook);
  g.kbHook = g.msHook = nullptr;
  StopAudio();
  g.audioRestartAt = 0;
  StopPipeline(g.mic);
  g.micOn = false;
  g.micWanted = false;
  StopPipeline(g.camera);
  g.cameraOn = false;
  g.cameraWanted = false;
  g.cameraRetryAt = 0;
  StopPipeline(g.video);
  ShowWindow(g.hwnd, SW_HIDE);
  SetWindowTextW(g.hwnd, L"Triton Screen Share");
  if (!g.quitting) std::printf("Session ended. Waiting for a sender...\n");
}

void EndSessionFromUi() {
  if (auto c = CurrentConn()) c->Shutdown();  // network thread notices and posts WM_APP_STOP
}

// Grows the audio buffer at once when packets are being dropped; shrinks it slowly once Wi-Fi has
// been calm for a while. Shrinking can itself drop one packet, so drops right after a shrink don't
// count.
void AdaptAudioBuffer(uint64_t now) {
  const uint64_t drops = g.audioDrops;
  const bool dropped = drops != g.prevAudioDrops;
  g.prevAudioDrops = drops;
  if (!g.audio) return;
  int next = g.audioBufferMs;
  if (dropped && now - g.sessionStart > 2'000'000 && now - g.lastAudioChange > 1'000'000) {
    g.lastAudioDrop = now;
    next = std::min(kAudioBufferMaxMs, g.audioBufferMs + kAudioBufferStepUpMs);
  } else if (!dropped && now - g.lastAudioDrop > 30'000'000 && now - g.lastAudioChange > 10'000'000) {
    next = std::max(kAudioBufferMinMs, g.audioBufferMs - kAudioBufferStepDownMs);
  }
  if (next == g.audioBufferMs) return;
  if (GstElement* jb = gst_bin_get_by_name(GST_BIN(g.audio), "ajb")) {
    g_object_set(jb, "latency", static_cast<guint>(next), nullptr);
    gst_object_unref(jb);
  }
  if (next > g.audioBufferMs) std::printf("Wi-Fi stalls dropped audio: audio buffer raised to %d ms\n", next);
  g.audioBufferMs = next;
  g.lastAudioChange = now;
}

// Folds this interval's least-delayed packet into the 30 s no-queue baseline, and publishes the
// average lateness of this interval's frames against it.
void UpdateQueueDelay() {
  if (g.resetDelayWindow.exchange(false)) g.transitWindow.clear();
  const int64_t lateSum = g.frameLateSum.exchange(0);
  const uint32_t lateCount = g.frameLateCount.exchange(0);
  const int64_t m = g.intervalMinTransit.exchange(State::kNoTransit);
  if (m == State::kNoTransit) return;  // no video this interval
  const int32_t transit = static_cast<int32_t>(m);
  g.transitWindow.push_back(transit);
  if (g.transitWindow.size() > 60) g.transitWindow.erase(g.transitWindow.begin());
  int32_t base = g.transitWindow.front();
  for (int32_t t : g.transitWindow) {
    if (static_cast<int32_t>(t - base) < 0) base = t;
  }
  g.baselineTransit = base;
  g.haveBaseline = true;
  g.queueDelayUs = lateCount ? static_cast<uint32_t>(lateSum / lateCount * 1000 / 90) : 0;
}

void OnStatsTimer() {
  const uint64_t now = NowUs();
  const double dt = (now - g.prevTime) / 1e6;
  if (dt <= 0) return;
  const uint64_t bytes = g.bytes, packets = g.packets, lost = g.lost, frames = g.frames;
  const uint64_t dB = bytes - g.prevBytes, dP = packets - g.prevPackets;
  const uint64_t dL = lost - g.prevLost, dF = frames - g.prevFrames;

  Stats st{};
  st.recvKbps = static_cast<uint32_t>(dB * 8 / 1000.0 / dt);
  st.lossPermille = static_cast<uint16_t>(dP + dL ? dL * 1000 / (dP + dL) : 0);
  st.jitterUs = g.jitterUs;
  st.fps = static_cast<uint16_t>(dF / dt + 0.5);
  st.lostPackets = static_cast<uint32_t>(lost);
  UpdateQueueDelay();
  st.queueDelayUs = g.queueDelayUs;
  SendToSender(MSG_STATS, st);
  g.lastStats = st;

  // Frozen waiting for a keyframe (the request or the keyframe itself may have been lost): ask
  // again; after 3 s show whatever the decoder has rather than stay frozen.
  if (g.waitingForKeyframe) {
    RequestKeyframe();
    if (now - g.waitingSince > 3'000'000) g.waitingForKeyframe = false;
  }
  if (const uint32_t skipped = g.skippedMs.exchange(0)) {
    std::printf("Picture was %u ms behind (network or this PC too slow): skipped ahead to live.\n", skipped);
  }

  g.prevTime = now;
  g.prevBytes = bytes;
  g.prevPackets = packets;
  g.prevLost = lost;
  g.prevFrames = frames;

  AdaptAudioBuffer(now);
  if (++g.tick % 4 == 0) {
    // Audio arrives as 100 packets/s (10 ms each); "played" should match it while sound is on.
    const uint64_t ap = g.audioPackets, apl = g.audioPlayed, ad = g.audioDrops;
    char audio[96] = "";
    if (g.audio) {
      std::snprintf(audio, sizeof(audio), " | audio in %3.0f/s played %3.0f/s dropped %llu, buffer %d ms",
                    (ap - g.prevAudioPackets) / 2.0, (apl - g.prevAudioPlayed) / 2.0,
                    static_cast<unsigned long long>(ad - g.prevAudioDropsPrinted), g.audioBufferMs);
    }
    g.prevAudioPackets = ap;
    g.prevAudioPlayed = apl;
    g.prevAudioDropsPrinted = ad;
    // "loss" counts packets the network lost; "rebuilt" how many of them FEC made whole again.
    std::printf("%5.1f Mbps | %3u fps | loss %4.1f%% (%u total, %llu rebuilt) | delay %4.0f ms%s\n",
                st.recvKbps / 1000.0, st.fps, st.lossPermille / 10.0, st.lostPackets,
                static_cast<unsigned long long>(FecRecovered()), st.queueDelayUs / 1000.0, audio);
  }

  if (g.video && !PollBus(g.video, "video")) EndSessionFromUi();
  AudioWatchdog(now);
  // The webcam stopped (unplugged, USB glitch, driver reset): reopen it up to 3 times, 2 s apart; a
  // camera that then ran for a minute gets 3 tries again. The other PC keeps its "Triton Camera".
  if (g.camera && !PollBus(g.camera, "camera")) {
    StopPipeline(g.camera);
    if (now - g.cameraOpenedAt > 60'000'000) g.cameraRetries = 0;
    CameraRetryOrGiveUp("Camera stopped");
  }
  if (g.cameraWanted && !g.camera && !g.cameraOpening && g.cameraRetryAt && now >= g.cameraRetryAt) {
    g.cameraRetryAt = 0;
    OpenCameraAsync();  // OnCameraReady says whether it's back
  }
  if (g.mic && !PollBus(g.mic, "mic")) {  // it refused to open, or was unplugged
    StopPipeline(g.mic);
    // Refused in shared mode: try exclusively (see BuildMic), and remember that for this mic.
    if (!g.micExclusive && !g.micDeviceOpen.empty()) {
      g.micNeedsExclusive = g.micDeviceOpen;
      std::printf("This microphone refused shared mode; opening it exclusively (only this app can use it).\n");
      OpenMicAsync(g.micDeviceOpen, true);
    } else {
      MicGiveUp("Mic sharing stopped: the microphone couldn't be opened.");
    }
  }
}

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
  switch (msg) {
    case WM_APP_START:
      g.startOk = StartSession();
      SetEvent(g.started);
      return 0;
    case WM_APP_STOP:
      StopSession();
      SetEvent(g.stopped);
      return 0;
    case WM_APP_HOTKEY:
      if (wp == 'I' && g.opt.input) {
        g.forwardInput = !g.forwardInput;
        if (!g.forwardInput) {
          ReleaseAllInput();
          SetCursor(LoadCursorW(nullptr, IDC_ARROW));
        }
        // While forwarding, this PC draws the cursor; otherwise the sender draws it into the video.
        SendToSender(MSG_CURSOR_MODE, CursorMode{static_cast<uint8_t>(g.forwardInput)});
        std::printf("Input forwarding %s\n", g.forwardInput ? "ON" : "OFF");
      } else if (wp == 'F') {
        ToggleFullscreen();
      } else if (wp == 'P') {
        ToggleMic();
      } else if (wp == 'V') {
        ToggleCamera();
      } else if (wp == 'p') {  // another microphone was picked: reopen if sharing
        if (g.mic) {
          ToggleMic();
          ToggleMic();
        }
      } else if (wp == 'v') {  // another webcam was picked
        if (g.camera) {
          ToggleCamera();
          ToggleCamera();
        }
      } else if (wp == 'H') {
        ToggleVisible();
      } else if (wp == 'M') {
        ApplyMute();
        std::printf("Sound %s\n", g.muted ? "muted" : "on");
      } else if (wp == 'Q') {
        EndSessionFromUi();
      }
      return 0;
    case WM_TIMER:
      if (wp == kStatsTimer) {
        OnStatsTimer();
      } else if (wp == kHousekeepingTimer) {
        if (g.opt.seconds && NowUs() - g.startedAt > static_cast<uint64_t>(g.opt.seconds) * 1'000'000) g_stop = true;
        if (g_stop) PostMessageW(hwnd, WM_CLOSE, 0, 0);
      }
      return 0;
    case WM_SETCURSOR:
      // While forwarding, the cursor is drawn here (in the sender's current shape) rather than into
      // the video, so it moves with no network or video delay.
      if (g.inSession && g.forwardInput && LOWORD(lp) == HTCLIENT) {
        SetCursor(RemoteCursor());
        return TRUE;
      }
      break;
    case WM_APP_CAMERA_READY:
      OnCameraReady(reinterpret_cast<GstElement*>(lp));
      return 0;
    case WM_APP_MIC_READY:
      OnMicReady(std::unique_ptr<MicResult>(reinterpret_cast<MicResult*>(lp)));
      return 0;
    case WM_APP_SIZE:
      // Extend mode: the sender's virtual screen is ready. Mouse mapping and window size follow it.
      g.hello.width = static_cast<uint16_t>(wp);
      g.hello.height = static_cast<uint16_t>(lp);
      if (g.inSession) ApplyWindowSize();
      std::printf("Showing an extended screen, %ux%u.\n", g.hello.width, g.hello.height);
      return 0;
    case WM_APP_CURSOR:
      // The shape changed without the mouse moving (e.g. a link appeared under it): apply it now.
      if (g.inSession && g.forwardInput) {
        POINT pt;
        GetCursorPos(&pt);
        if (WindowFromPoint(pt) == hwnd &&
            SendMessageW(hwnd, WM_NCHITTEST, 0, MAKELPARAM(pt.x, pt.y)) == HTCLIENT) {
          SetCursor(RemoteCursor());
        }
      }
      return 0;
    case WM_CLOSE:
      if (g.inSession && !g_stop) {  // closing the window ends the session, not the app
        EndSessionFromUi();
        return 0;
      }
      g.quitting = true;
      EndSessionFromUi();
      StopSession();
      if (g.listenSock != INVALID_SOCKET) {
        closesocket(g.listenSock);
        g.listenSock = INVALID_SOCKET;
      }
      DestroyWindow(hwnd);
      return 0;
    case WM_DESTROY:
      PostQuitMessage(0);
      return 0;
  }
  return DefWindowProcW(hwnd, msg, wp, lp);
}

// ---------------------------------------------------------------------------
// Network thread: accept one sender at a time and relay its control messages.

void NetworkThread() {
  const std::string name = ComputerName();
  uint64_t lastBeacon = 0;
  while (!g.quitting) {
    SOCKET ls = g.listenSock;
    if (ls == INVALID_SOCKET) break;
    if (NowUs() - lastBeacon > 1'000'000) {
      SendBeacon(g.opt.port, name);
      lastBeacon = NowUs();
    }
    fd_set rd;
    FD_ZERO(&rd);
    FD_SET(ls, &rd);
    timeval tv{0, 500'000};
    int r = select(0, &rd, nullptr, nullptr, &tv);
    if (g.quitting || r == SOCKET_ERROR) break;
    if (r == 0) continue;

    SOCKET s = accept(ls, nullptr, nullptr);
    if (s == INVALID_SOCKET) continue;
    SetNoDelay(s);
    auto conn = std::make_shared<Connection>(s);
    const std::string peer = conn->PeerIp();
    std::string peerName;
    if (!SecureHandshake(*conn, false, g.keys, peerName)) {  // it printed why
      std::printf("Refused the connection from %s.\n", peer.c_str());
      continue;
    }

    uint8_t type = 0;
    std::vector<uint8_t> p;
    if (!conn->WaitReadable(5000) || !conn->Recv(type, p) || type != MSG_HELLO || p.size() < sizeof(Hello)) {
      std::printf("Ignored a connection from %s (bad handshake).\n", peer.c_str());
      continue;
    }
    Hello hello;
    std::memcpy(&hello, p.data(), sizeof(hello));
    hello.name[sizeof(hello.name) - 1] = '\0';
    if (hello.version != kProtocolVersion) {
      std::printf("Sender %s uses protocol v%u; this app speaks v%u. Update both sides.\n", peer.c_str(),
                  hello.version, kProtocolVersion);
      continue;
    }
    std::printf("\n'%s' (%s) connected: %ux%u @ %u fps, %s%s\n", hello.name, peer.c_str(), hello.width,
                hello.height, hello.fps, hello.codec == 0 ? "H.264" : "H.265", hello.audio ? " + audio" : "");

    g.hello = hello;
    g.peer = peer;
    {
      std::lock_guard<std::mutex> lock(g.connMu);
      g.conn = conn;  // set first so the very first stats/keyframe requests can be sent
    }
    ResetEvent(g.started);
    PostMessageW(g.hwnd, WM_APP_START, 0, 0);
    if (WaitForSingleObject(g.started, 15000) != WAIT_OBJECT_0 || !g.startOk) {
      std::printf("Could not start playback.\n");
      std::lock_guard<std::mutex> lock(g.connMu);
      g.conn.reset();
      continue;
    }
    std::printf("Playing via %s. Hotkeys: Ctrl+Alt+Shift+I input on/off, +F fullscreen, +Q disconnect.\n",
                g.decoder.c_str());
    // This PC's screen (the one the picture is shown on), for extend mode's virtual screen size.
    const bool wantAudio = g.audio || g.audioRestartAt;  // a failed start is being retried
    Ready ready{kVideoPort, static_cast<uint16_t>(wantAudio ? kAudioPort : 0), 0, 0, 0};
    MONITORINFOEXW mi{};
    mi.cbSize = sizeof(mi);
    DEVMODEW dm{};
    dm.dmSize = sizeof(dm);
    if (GetMonitorInfoW(MonitorFromWindow(g.hwnd, MONITOR_DEFAULTTOPRIMARY), &mi) &&
        EnumDisplaySettingsW(mi.szDevice, ENUM_CURRENT_SETTINGS, &dm)) {
      ready.screenWidth = static_cast<uint16_t>(dm.dmPelsWidth);
      ready.screenHeight = static_cast<uint16_t>(dm.dmPelsHeight);
      ready.screenHz = static_cast<uint16_t>(dm.dmDisplayFrequency);
    }
    conn->Send(MSG_READY, ready);
    g.cursorShape = 0;
    g.cursorVisible = true;
    conn->Send(MSG_CURSOR_MODE, CursorMode{static_cast<uint8_t>(g.forwardInput)});

    // The sender mostly transmits, so its Wi-Fi adapter dozes and the access point holds packets
    // for it until the next beacon: RTT to it was measured jumping 10-313 ms. Steady tiny traffic
    // toward it keeps the radio awake, so keyframe requests and input arrive without that delay.
    std::atomic<bool> sessionOver{false};
    std::thread keepAlive([conn, &sessionOver] {
      while (!sessionOver && conn->Send(MSG_KEEPALIVE, nullptr, 0)) Sleep(20);
    });

    while (!g.quitting) {
      if (!conn->Recv(type, p)) break;
      if (type == MSG_PING && p.size() >= sizeof(Ping)) {
        conn->Send(MSG_PONG, p.data(), sizeof(Ping));
      } else if (type == MSG_SIZE && p.size() >= sizeof(VideoSize)) {
        VideoSize vs;
        std::memcpy(&vs, p.data(), sizeof(vs));
        PostMessageW(g.hwnd, WM_APP_SIZE, vs.width, vs.height);
      } else if (type == MSG_CAMERA_STATUS && p.size() >= sizeof(MicStatus)) {
        g.senderCameraAvailable = p[0] != 0;
      } else if (type == MSG_MIC_STATUS && p.size() >= sizeof(MicStatus)) {
        g.senderMicAvailable = p[0] != 0;
      } else if (type == MSG_MODE && p.size() >= sizeof(ModeReport)) {
        g.senderExtended = p[0] != 0;
        g.senderCanExtend = p[1] != 0;
      } else if (type == MSG_CURSOR && p.size() >= sizeof(Cursor)) {
        g.cursorShape = p[0] < kCursorShapeCount ? p[0] : 0;
        g.cursorVisible = p[1] != 0;
        PostMessageW(g.hwnd, WM_APP_CURSOR, 0, 0);
      } else if (type == MSG_BYE) {
        break;
      }
    }

    sessionOver = true;
    keepAlive.join();
    {
      std::lock_guard<std::mutex> lock(g.connMu);
      g.conn.reset();
    }
    conn->Shutdown();
    if (g.quitting) break;
    ResetEvent(g.stopped);
    PostMessageW(g.hwnd, WM_APP_STOP, 0, 0);
    WaitForSingleObject(g.stopped, 5000);
  }
}

}  // namespace

void ReceiverCommand(char command) {
  if (command == 'M') {
    // The state flips right away (so the panel can label its button); the sink follows on the
    // window thread, and a session started later starts muted too.
    g.muted = !g.muted;
    if (g.hwnd) PostMessageW(g.hwnd, WM_APP_HOTKEY, 'M', 0);
    return;
  }
  // Handled on the window's own thread, exactly like the Ctrl+Alt+Shift hotkeys.
  if (g.inSession && g.hwnd) PostMessageW(g.hwnd, WM_APP_HOTKEY, static_cast<WPARAM>(command), 0);
}

bool ReceiverMuted() { return g.muted; }

bool ReceiverInSession() { return g.inSession; }

bool ReceiverHidden() { return g.hwnd && !IsWindowVisible(g.hwnd); }

bool ReceiverMicOn(bool* available) {
  if (available) *available = g.inSession && g.senderMicAvailable;
  return g.micOn;
}

void ReceiverToggleMic() {
  if (g.hwnd) PostMessageW(g.hwnd, WM_APP_HOTKEY, 'P', 0);
}

bool ReceiverCameraOn(bool* available) {
  if (available) *available = g.inSession && g.senderCameraAvailable;
  return g.cameraOn;
}

void ReceiverToggleCamera() {
  if (g.hwnd) PostMessageW(g.hwnd, WM_APP_HOTKEY, 'V', 0);
}

void ReceiverSetMicDevice(const std::string& id) {
  {
    std::lock_guard<std::mutex> lock(g.deviceMu);
    if (g.micDevice == id) return;
    g.micDevice = id;
  }
  if (g.hwnd) PostMessageW(g.hwnd, WM_APP_HOTKEY, 'p', 0);
}

void ReceiverSetCameraDevice(const std::string& id) {
  {
    std::lock_guard<std::mutex> lock(g.deviceMu);
    if (g.cameraDevice == id) return;
    g.cameraDevice = id;
  }
  if (g.hwnd) PostMessageW(g.hwnd, WM_APP_HOTKEY, 'v', 0);
}

bool ReceiverExtended(bool* canExtend) {
  if (canExtend) *canExtend = g.inSession && g.senderCanExtend;
  return g.senderExtended;
}

void ReceiverSwitchMode() {
  if (g.inSession && g.senderCanExtend) SendToSender(MSG_SET_MODE, SetMode{static_cast<uint8_t>(!g.senderExtended)});
}

int RunReceiver(const ReceiverOptions& opt) {
  g.opt = opt;
  g.quitting = false;  // the launcher can start this mode again after stopping it
  g.startedAt = NowUs();
  g.listenSock = ListenTcp(opt.port);
  if (g.listenSock == INVALID_SOCKET) {
    std::fprintf(stderr, "Cannot listen on TCP port %u (already in use?).\n", opt.port);
    return 1;
  }

  WNDCLASSEXW wc{sizeof(wc)};
  wc.lpfnWndProc = WndProc;
  wc.hInstance = GetModuleHandleW(nullptr);
  wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
  wc.hbrBackground = static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
  wc.lpszClassName = kWindowClass;
  RegisterClassExW(&wc);
  // WS_POPUP: no title bar, borders or buttons. Tool window: no taskbar button (the bubble shows and
  // hides it). Topmost: other apps opened meanwhile never cover it.
  g.hwnd = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_TOPMOST, kWindowClass, L"Triton Screen Share", WS_POPUP, CW_USEDEFAULT,
                           CW_USEDEFAULT, 1280, 720, nullptr, nullptr, wc.hInstance, nullptr);
  if (!g.hwnd) {
    std::fprintf(stderr, "Could not create the window.\n");
    return 1;
  }
  g_notifyHwnd = g.hwnd;
  g.started = CreateEventW(nullptr, FALSE, FALSE, nullptr);
  g.stopped = CreateEventW(nullptr, FALSE, FALSE, nullptr);
  SetTimer(g.hwnd, kHousekeepingTimer, 250, nullptr);

  std::printf("Show a screen here: waiting for a sender on TCP %u.\n", opt.port);
  std::printf("On the other device choose 'Share this screen' and pick '%s', or type one of:\n",
              ComputerName().c_str());
  for (const auto& a : LocalAddresses()) std::printf("    %-15s  (%s)\n", a.ip.c_str(), a.adapter.c_str());
  std::printf("Stop with the Stop button (or Ctrl+C in a console).\n\n");

  g.inputEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
  std::thread net(NetworkThread);
  std::thread input(InputThread);
  MSG msg;
  while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }
  g.quitting = true;
  SetEvent(g.inputEvent);
  input.join();
  CloseHandle(g.inputEvent);
  g_notifyHwnd = nullptr;
  if (g.listenSock != INVALID_SOCKET) closesocket(g.listenSock);
  g.listenSock = INVALID_SOCKET;
  SetEvent(g.started);
  SetEvent(g.stopped);
  net.join();
  CloseHandle(g.started);
  CloseHandle(g.stopped);
  return 0;
}

}  // namespace lfrd
