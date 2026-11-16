// "Share this screen": capture a monitor on the GPU, hardware-encode it, send RTP over UDP,
// adapt the bitrate from receiver reports, and replay the receiver's mouse/keyboard input.
#include <gst/gst.h>
#include <gst/video/video.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "common.h"
#include "fec.h"
#include "gst_util.h"
#include "secure.h"
#include "sps_rewrite.h"
#include "vcam_host.h"
#include "vdd.h"

namespace lfrd {
namespace {

// This session's stream keys (from SecureHandshake): every pipeline encrypts or decrypts with them.
SessionKeys g_keys;

BOOL CALLBACK CollectMonitor(HMONITOR h, HDC, LPRECT, LPARAM user) {
  auto* list = reinterpret_cast<std::vector<MonitorInfo>*>(user);
  MONITORINFOEXW mi{};
  mi.cbSize = sizeof(mi);
  if (GetMonitorInfoW(h, &mi)) {
    list->push_back({h, mi.rcMonitor, (mi.dwFlags & MONITORINFOF_PRIMARY) != 0, Narrow(mi.szDevice)});
  }
  return TRUE;
}

std::string BuildVideoPipeline(const std::string& enc, Codec codec, int fps, const std::string& host,
                               uint16_t port) {
  const bool h264 = codec == Codec::H264;
  // A 1-frame leaky queue drops stale frames instead of letting latency build up.
  std::string s = "d3d11screencapturesrc name=cap show-cursor=true ! "
                  "video/x-raw(memory:D3D11Memory),framerate=" + std::to_string(fps) + "/1 ! "
                  "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! ";
  if (AcceptsD3D11Input(enc)) {
    s += "d3d11convert ! video/x-raw(memory:D3D11Memory),format=NV12 ! ";  // stays on the GPU
  } else {
    s += "d3d11download ! videoconvert ! video/x-raw,format=I420 ! ";  // software encoder
  }
  s += enc + " name=enc ! ";
  // "bs" is where the SPS gets rewritten (see sps_rewrite.h), so it must be Annex-B access units.
  s += h264 ? "h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! "
              "capsfilter name=bs ! rtph264pay"
            : "h265parse config-interval=-1 ! rtph265pay";
  // tritonfecenc encrypts every packet and adds Reed-Solomon parity per frame (fec.h).
  s += " name=pay pt=96 mtu=1200 config-interval=-1 aggregate-mode=zero-latency ! tritonfecenc name=fec ! "
       "udpsink name=out host=" + host + " port=" + std::to_string(port) +
       // Small send buffer: when Wi-Fi is slower than the encoder, a 4 MB buffer was measured to
       // queue ~4-6 s of video. 256 KB makes the pipeline wait instead and the leaky queue drops
       // capture frames, so delay can't build here.
       " sync=false async=false qos-dscp=34 buffer-size=262144";
  return s;
}

std::string BuildAudioPipeline(const std::string& host, uint16_t port) {
  return "wasapi2src loopback=true low-latency=true ! "
         "queue max-size-time=40000000 max-size-buffers=0 max-size-bytes=0 leaky=downstream ! "
         "audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 ! "
         // WASAPI loopback timestamps wobble; without perfect timestamps every wobble reaches the
         // receiver as a discontinuity and its audio sink resyncs (~30/s measured = no sound).
         "opusenc frame-size=10 bitrate=128000 perfect-timestamp=true ! rtpopuspay pt=97 ! "
         "udpsink name=aout host=" + host + " port=" + std::to_string(port) +  // encrypted on its sink
         " sync=false async=false qos-dscp=46";
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

bool Play(GstElement* p, const char* label) {
  if (gst_element_set_state(p, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
    PollBus(p, label);
    return false;
  }
  return true;
}

void Stop(GstElement*& p) {
  if (!p) return;
  gst_element_set_state(p, GST_STATE_NULL);
  gst_object_unref(p);
  p = nullptr;
}

// ---------------------------------------------------------------------------
// Input injection. Coordinates arrive normalized to the shared monitor.

void InjectMouseMove(const RECT& mon, uint16_t nx, uint16_t ny) {
  const int px = mon.left + static_cast<int>(nx / 65535.0 * (mon.right - mon.left - 1));
  const int py = mon.top + static_cast<int>(ny / 65535.0 * (mon.bottom - mon.top - 1));
  const int vx = GetSystemMetrics(SM_XVIRTUALSCREEN), vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
  const int vw = GetSystemMetrics(SM_CXVIRTUALSCREEN), vh = GetSystemMetrics(SM_CYVIRTUALSCREEN);
  INPUT in{};
  in.type = INPUT_MOUSE;
  in.mi.dx = static_cast<LONG>((px - vx) * 65535.0 / std::max(1, vw - 1));
  in.mi.dy = static_cast<LONG>((py - vy) * 65535.0 / std::max(1, vh - 1));
  in.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK;
  SendInput(1, &in, sizeof(in));
}

void InjectMouseButton(uint8_t button, bool down) {
  INPUT in{};
  in.type = INPUT_MOUSE;
  switch (button) {
    case 0: in.mi.dwFlags = down ? MOUSEEVENTF_LEFTDOWN : MOUSEEVENTF_LEFTUP; break;
    case 1: in.mi.dwFlags = down ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_RIGHTUP; break;
    case 2: in.mi.dwFlags = down ? MOUSEEVENTF_MIDDLEDOWN : MOUSEEVENTF_MIDDLEUP; break;
    case 3:
    case 4:
      in.mi.dwFlags = down ? MOUSEEVENTF_XDOWN : MOUSEEVENTF_XUP;
      in.mi.mouseData = button == 3 ? XBUTTON1 : XBUTTON2;
      break;
    default: return;
  }
  SendInput(1, &in, sizeof(in));
}

void InjectWheel(int16_t delta, bool horizontal) {
  INPUT in{};
  in.type = INPUT_MOUSE;
  in.mi.dwFlags = horizontal ? MOUSEEVENTF_HWHEEL : MOUSEEVENTF_WHEEL;
  in.mi.mouseData = static_cast<DWORD>(delta);
  SendInput(1, &in, sizeof(in));
}

void InjectKey(uint16_t scan, uint8_t flags) {
  INPUT in{};
  in.type = INPUT_KEYBOARD;
  in.ki.wScan = scan;
  in.ki.dwFlags = KEYEVENTF_SCANCODE;
  if (flags & 2) in.ki.dwFlags |= KEYEVENTF_EXTENDEDKEY;
  if (!(flags & 1)) in.ki.dwFlags |= KEYEVENTF_KEYUP;
  SendInput(1, &in, sizeof(in));
}

// The receiver draws the cursor itself (it moves instantly there instead of a video-latency later),
// so it only needs to know which shape to show. Standard cursors are shared system handles, so a
// handle comparison identifies them.
Cursor CurrentCursor() {
  static HCURSOR handles[kCursorShapeCount];
  static bool loaded = false;
  if (!loaded) {
    for (uint8_t i = 0; i < kCursorShapeCount; ++i) handles[i] = LoadCursorW(nullptr, kCursorShapes[i]);
    loaded = true;
  }
  Cursor c{0, 1};
  CURSORINFO ci{sizeof(ci)};
  if (!GetCursorInfo(&ci)) return c;
  // CURSOR_SUPPRESSED (touch input) still means a mouse cursor would be visible for our injected moves.
  c.visible = (ci.flags & (CURSOR_SHOWING | CURSOR_SUPPRESSED)) ? 1 : 0;
  for (uint8_t i = 0; i < kCursorShapeCount; ++i) {
    if (ci.hCursor == handles[i]) c.shape = i;
  }
  return c;
}

// ---------------------------------------------------------------------------
// Bitrate control: back off quickly when the network shows loss or queueing, creep back up slowly
// while it is clean. Queueing delay is the early signal: a link slower than the bitrate first makes
// packets late (lag on screen) and only much later, once buffers overflow, makes them lost.

struct RateController {
  int bitrate, minBitrate, maxBitrate;
  int hold = 0;

  int Update(const Stats& st) {
    const double lossPct = st.lossPermille / 10.0;
    const double jitterMs = st.jitterUs / 1000.0;
    const double delayMs = st.queueDelayUs / 1000.0;
    int next = bitrate;
    if (delayMs > 120.0 || lossPct >= 2.0) {
      // Far over what the link carries: drop to below what actually got through, so the queue drains.
      next = std::min(static_cast<int>(bitrate * 0.6), static_cast<int>(st.recvKbps * 0.85));
      hold = 6;
    } else if (delayMs > 45.0 || lossPct >= 0.5 || jitterMs > 12.0) {
      next = static_cast<int>(bitrate * 0.8);
      hold = 4;
    } else if (delayMs > 20.0) {  // (~10 ms is normal encode-time wobble)
      // A queue is starting, so the link is about full and what got through is its capacity: stay
      // just above that instead of probing higher (a busier screen would otherwise jump to the cap).
      next = std::min(bitrate, static_cast<int>(st.recvKbps * 1.15) + 500);
      hold = std::max(hold, 2);
    } else if (hold > 0) {
      --hold;
    } else {
      // Climb faster while there is no queue at all (12 -> 40 Mbps in ~7 s), carefully otherwise.
      next = static_cast<int>(bitrate * (delayMs < 8.0 ? 1.10 : 1.04)) + 100;
    }
    return std::clamp(next, minBitrate, maxBitrate);
  }
};

// Decoded webcam frames from the receiver (1280x720 NV12) go to "Triton Camera".
std::atomic<uint64_t> g_cameraFrames{0};
void OnCameraFrame(GstElement*, GstBuffer* buf, GstPad*, gpointer) {
  GstMapInfo m;
  if (!gst_buffer_map(buf, &m, GST_MAP_READ)) return;
  PushCameraFrame(m.data, m.size);
  gst_buffer_unmap(buf, &m);
  ++g_cameraFrames;
}

// Restamps each captured frame with the real time it was captured. The capture source counts frames
// at the nominal rate (60.000 fps) while the monitor really refreshes at e.g. 59.95 Hz, so its
// timestamps drifted ~0.8 ms/s from real time. The receiver measures queueing delay from these
// (as RTP timestamps), and that drift looked like an ever-growing lag.
// While set (NowUs() before it), captured frames are thrown away before encoding, so nothing is sent
// and a backlog already in the network drains at the link's full speed.
std::atomic<uint64_t> g_pauseUntil{0};

GstPadProbeReturn RealTimeStampProbe(GstPad* pad, GstPadProbeInfo* info, gpointer) {
  if (NowUs() < g_pauseUntil) return GST_PAD_PROBE_DROP;
  GstElement* src = GST_ELEMENT(gst_pad_get_parent(pad));
  if (!src) return GST_PAD_PROBE_OK;
  if (GstClock* clock = gst_element_get_clock(src)) {
    const GstClockTime now = gst_clock_get_time(clock) - gst_element_get_base_time(src);
    GstBuffer* buf = gst_buffer_make_writable(GST_PAD_PROBE_INFO_BUFFER(info));
    GST_BUFFER_PTS(buf) = now;
    GST_BUFFER_DTS(buf) = GST_CLOCK_TIME_NONE;
    GST_PAD_PROBE_INFO_DATA(info) = buf;
    gst_object_unref(clock);
  }
  gst_object_unref(src);
  return GST_PAD_PROBE_OK;
}

// The capture/encode/send pipeline for one screen. Switching between mirror and extend replaces it
// while everything else (connection, audio, input, bitrate) carries on.
struct VideoPipe {
  GstElement* pipeline = nullptr;
  GstElement* cap = nullptr;
  GstElement* enc = nullptr;
  GstPad* encSrc = nullptr;
};

void StopVideo(VideoPipe& v) {
  if (v.encSrc) gst_object_unref(v.encSrc);
  if (v.enc) gst_object_unref(v.enc);
  if (v.cap) gst_object_unref(v.cap);
  Stop(v.pipeline);
  v = {};
}

bool StartVideo(VideoPipe& v, const std::string& encName, const SenderOptions& opt, const std::string& host,
                uint16_t port, const MonitorInfo& mon, int bitrate, bool showCursor) {
  v.pipeline = Launch(BuildVideoPipeline(encName, opt.codec, opt.fps, host, port), "video");
  if (!v.pipeline) return false;
  GstElement* fec = gst_bin_get_by_name(GST_BIN(v.pipeline), "fec");
  ConfigureFec(fec, g_keys.video);
  if (fec) gst_object_unref(fec);
  v.cap = gst_bin_get_by_name(GST_BIN(v.pipeline), "cap");
  g_object_set(v.cap, "monitor-handle", static_cast<guint64>(reinterpret_cast<uintptr_t>(mon.handle)),
               "show-cursor", showCursor ? TRUE : FALSE, nullptr);
  if (GstPad* capSrc = gst_element_get_static_pad(v.cap, "src")) {
    gst_pad_add_probe(capSrc, GST_PAD_PROBE_TYPE_BUFFER, RealTimeStampProbe, nullptr, nullptr);
    gst_object_unref(capSrc);
  }
  v.enc = gst_bin_get_by_name(GST_BIN(v.pipeline), "enc");
  ConfigureEncoder(v.enc, encName, bitrate, opt.fps);
  v.encSrc = gst_element_get_static_pad(v.enc, "src");
  if (GstElement* bs = gst_bin_get_by_name(GST_BIN(v.pipeline), "bs")) {
    GstPad* bsSrc = gst_element_get_static_pad(bs, "src");
    InstallZeroReorderProbe(bsSrc);
    gst_object_unref(bsSrc);
    gst_object_unref(bs);
  }
  if (!Play(v.pipeline, "video")) {
    std::fprintf(stderr, "Video pipeline failed to start.\n");
    StopVideo(v);
    return false;
  }
  return true;
}

// Monitors are re-listed after a display change: their handles can change with the layout.
bool FindMonitor(const std::string& device, MonitorInfo& out) {
  for (const MonitorInfo& m : ListMonitors()) {
    if (m.device == device) {
      out = m;
      return true;
    }
  }
  return false;
}

}  // namespace

std::vector<MonitorInfo> ListMonitors() {
  std::vector<MonitorInfo> list;
  EnumDisplayMonitors(nullptr, nullptr, CollectMonitor, reinterpret_cast<LPARAM>(&list));
  return list;
}

int RunSender(const SenderOptions& opt) {
  auto monitors = ListMonitors();
  if (monitors.empty()) {
    std::fprintf(stderr, "No monitors found.\n");
    return 1;
  }
  int index = opt.monitor;
  if (index < 0) {
    index = 0;
    for (size_t i = 0; i < monitors.size(); ++i) {
      if (monitors[i].primary) index = static_cast<int>(i);
    }
  }
  if (index >= static_cast<int>(monitors.size())) {
    std::fprintf(stderr, "Monitor %d does not exist (found %zu).\n", index, monitors.size());
    return 1;
  }
  const MonitorInfo mirrorMon = monitors[index];  // the screen mirror mode shares
  MonitorInfo mon = mirrorMon;                     // the one being shared now
  int width = mon.rect.right - mon.rect.left;
  int height = mon.rect.bottom - mon.rect.top;

  RegisterFecElements();
  const std::string encName = ChooseEncoder(opt.codec, opt.encoder);
  if (encName.empty()) {
    std::fprintf(stderr, "No usable %s encoder found%s.\n", opt.codec == Codec::H264 ? "H.264" : "H.265",
                 opt.encoder.empty() ? "" : (" named " + opt.encoder).c_str());
    return 1;
  }

  std::printf("Connecting to %s:%u ...\n", opt.to.c_str(), opt.port);
  SOCKET s = ConnectTcp(opt.to, opt.port, 4000);
  if (s == INVALID_SOCKET) {
    std::fprintf(stderr,
                 "Could not reach %s:%u. Is 'Show a screen here' running there, and is the\n"
                 "firewall allowing TritonScreenShare.exe on Private networks?\n",
                 opt.to.c_str(), opt.port);
    return 1;
  }
  Connection conn(s);
  const std::string host = conn.PeerIp();
  std::string peerName;
  if (!SecureHandshake(conn, true, g_keys, peerName)) return 1;

  Hello hello{};
  hello.version = kProtocolVersion;
  hello.width = static_cast<uint16_t>(width);
  hello.height = static_cast<uint16_t>(height);
  hello.fps = static_cast<uint8_t>(opt.fps);
  hello.codec = static_cast<uint8_t>(opt.codec);
  hello.audio = opt.audio ? 1 : 0;
  strncpy_s(hello.name, ComputerName().c_str(), _TRUNCATE);
  conn.Send(MSG_HELLO, hello);

  Ready ready{};
  bool gotReady = false;
  const uint64_t readyDeadline = NowUs() + 15'000'000;
  while (!gotReady && !g_stop && NowUs() < readyDeadline) {
    if (!conn.WaitReadable(200)) continue;
    uint8_t type;
    std::vector<uint8_t> payload;
    if (!conn.Recv(type, payload)) break;
    if (type == MSG_READY && payload.size() >= sizeof(Ready)) {
      std::memcpy(&ready, payload.data(), sizeof(Ready));
      gotReady = true;
    }
  }
  if (!gotReady) {
    std::fprintf(stderr, "Receiver did not get ready.\n");
    return 1;
  }

  // Extend: add a virtual screen the size of the receiver's and share that. It comes off the
  // desktop again however this function returns.
  struct DetachOnExit {
    bool on = false;
    ~DetachOnExit() {
      if (on) VddDetach();
    }
  } detach;
  const bool canExtend = opt.extend || VddAvailable();
  bool extended = opt.extend;
  if (opt.extend) {
    detach.on = true;  // also after a failed attempt, which may have left it half set up
    if (!VddAttach(ready.screenWidth, ready.screenHeight, ready.screenHz, mon)) {
      conn.Send(MSG_BYE, nullptr, 0);
      return 1;
    }
    width = mon.rect.right - mon.rect.left;
    height = mon.rect.bottom - mon.rect.top;
    conn.Send(MSG_SIZE, VideoSize{static_cast<uint16_t>(width), static_cast<uint16_t>(height)});
  }

  uint16_t videoPort = ready.videoPort;
  // Test hook: send video through a local network simulator instead of straight to the receiver.
  if (const char* p = std::getenv("LFRD_TEST_VIDEO_PORT")) videoPort = static_cast<uint16_t>(std::atoi(p));
  bool showCursor = true;  // until the receiver says it draws the cursor itself
  VideoPipe vp;
  if (!StartVideo(vp, encName, opt, host, videoPort, mon, opt.bitrate, showCursor)) return 1;
  conn.Send(MSG_MODE, ModeReport{static_cast<uint8_t>(extended), static_cast<uint8_t>(canExtend)});

  // The receiver's microphone, for apps on this PC: played into a virtual cable (VB-Audio Virtual
  // Cable, which the installer names "Triton Mic Input" here and "Triton Mic" as the microphone), so Teams/Zoom/a browser can
  // pick it. Test hook: LFRD_TEST_MIC_DEVICE=default plays it on the normal speakers instead.
  std::string micCableName;
  std::string micDevice = FindAudioOutput({"Triton Mic Input", "CABLE Input"}, &micCableName);  // renamed by the installer
  const bool micToSpeakers = std::getenv("LFRD_TEST_MIC_DEVICE") != nullptr;
  const bool micAvailable = !micDevice.empty() || micToSpeakers;
  GstElement* mic = nullptr;
  conn.Send(MSG_MIC_STATUS, MicStatus{static_cast<uint8_t>(micAvailable), 0});

  // The receiver's webcam, for apps on this PC, as "Triton Camera" (Windows 11 + Install-Camera.bat).
  // Test hook: LFRD_TEST_CAMERA=1 decodes it without the virtual camera.
  std::string cameraWhy;
  const bool cameraTest = std::getenv("LFRD_TEST_CAMERA") != nullptr;
  const bool cameraAvailable = VirtualCameraAvailable(&cameraWhy) || cameraTest;
  if (!cameraAvailable) std::printf("The other PC's camera can't be offered here: %s.\n", cameraWhy.c_str());
  GstElement* camera = nullptr;
  uint64_t cameraFramesPrinted = 0;
  conn.Send(MSG_CAMERA_STATUS, MicStatus{static_cast<uint8_t>(cameraAvailable), 0});

  GstElement* audio = nullptr;
  if (opt.audio && ready.audioPort) {
    uint16_t audioPort = ready.audioPort;
    // Test hook: send audio through a local network simulator instead of straight to the receiver.
    if (const char* p = std::getenv("LFRD_TEST_AUDIO_PORT")) audioPort = static_cast<uint16_t>(std::atoi(p));
    audio = Launch(BuildAudioPipeline(host, audioPort), "audio");
    if (audio) EncryptRtp(audio, "aout", "sink", g_keys.audio);
    if (audio && !Play(audio, "audio")) {
      std::fprintf(stderr, "Audio failed to start; continuing with video only.\n");
      Stop(audio);
    }
  }

  std::printf("\nSharing %s (%dx%d @ %d fps) -> %s using %s%s\n", mon.device.c_str(), width, height,
              opt.fps, host.c_str(), encName.c_str(), audio ? " + audio" : "");
  std::printf("Stop with the Stop button (or Ctrl+C in a console).\n\n");

  RateController rc{opt.bitrate, opt.minBitrate, opt.maxBitrate};
  Stats last{};
  uint32_t rttUs = 0;
  uint64_t lastPing = 0, lastStatus = 0, lastPrint = 0, lastKeyframe = 0;
  Cursor sentCursor{0xFF, 0xFF};
  const uint64_t endAt = opt.seconds ? NowUs() + static_cast<uint64_t>(opt.seconds) * 1'000'000 : 0;
  bool connected = true;

  while (connected && !g_stop) {
    if (endAt && NowUs() > endAt) break;
    if (!PollBus(vp.pipeline, "video")) break;
    if (camera && !PollBus(camera, "camera")) {
      std::fprintf(stderr, "The receiver's camera stream stopped.\n");
      Stop(camera);
      conn.Send(MSG_CAMERA_STATUS, MicStatus{static_cast<uint8_t>(cameraAvailable), 0});
    }
    if (mic && !PollBus(mic, "mic")) {
      std::fprintf(stderr, "The receiver's mic stopped playing here.\n");
      Stop(mic);
      conn.Send(MSG_MIC_STATUS, MicStatus{static_cast<uint8_t>(micAvailable), 0});
    }
    if (audio && !PollBus(audio, "audio")) {
      std::fprintf(stderr, "Audio stopped; continuing with video only.\n");
      Stop(audio);
    }

    uint64_t now = NowUs();
    if (now - lastPing > 500'000) {
      Ping p{now};
      conn.Send(MSG_PING, p);
      lastPing = now;
    }

    // Drain everything pending, then go back to housekeeping.
    for (int budget = 256; budget > 0 && conn.WaitReadable(budget == 256 ? 10 : 0); --budget) {
      uint8_t type;
      std::vector<uint8_t> p;
      if (!conn.Recv(type, p)) {
        std::printf("Receiver disconnected.\n");
        connected = false;
        break;
      }
      now = NowUs();
      switch (type) {
        case MSG_PONG:
          if (p.size() >= sizeof(Ping)) {
            Ping ping;
            std::memcpy(&ping, p.data(), sizeof(ping));
            rttUs = static_cast<uint32_t>(now - ping.t);
          }
          break;
        case MSG_STATS:
          if (p.size() >= sizeof(Stats)) {
            std::memcpy(&last, p.data(), sizeof(Stats));
            // Only once video flows. Packets arriving counts: while the receiver skips stale video it
            // decodes nothing (0 fps), and that is exactly when its delay report matters most.
            if (opt.adaptive && (last.fps > 0 || last.recvKbps > 0)) {
              // Seconds of video stuck in the network: lowering the bitrate drains it only slowly
              // (link speed minus new bitrate), so pause sending for about as long as the backlog.
              const uint64_t delayUs = last.queueDelayUs;
              if (delayUs > 250'000 && now > g_pauseUntil + 1'000'000) {
                g_pauseUntil = now + std::min<uint64_t>(delayUs, 1'500'000);
                std::printf("Network backlog of %llu ms: pausing video to let it drain.\n",
                            static_cast<unsigned long long>(delayUs / 1000));
              }
              int next = rc.Update(last);
              if (std::abs(next - rc.bitrate) * 50 >= rc.bitrate) {  // ignore <2% changes
                rc.bitrate = next;
                SetEncoderBitrate(vp.enc, next);
              }
            }
          }
          break;
        case MSG_KEYFRAME:
          if (now - lastKeyframe > 200'000) {
            gst_pad_send_event(vp.encSrc, gst_video_event_new_upstream_force_key_unit(GST_CLOCK_TIME_NONE, TRUE, 0));
            lastKeyframe = now;
          }
          break;
        case MSG_MOUSE_MOVE:
          if (p.size() >= sizeof(MouseMove)) {
            MouseMove m;
            std::memcpy(&m, p.data(), sizeof(m));
            InjectMouseMove(mon.rect, m.x, m.y);
          }
          break;
        case MSG_MOUSE_BUTTON:
          if (p.size() >= sizeof(MouseButton)) InjectMouseButton(p[0], p[1] != 0);
          break;
        case MSG_MOUSE_WHEEL:
          if (p.size() >= sizeof(MouseWheel)) {
            MouseWheel w;
            std::memcpy(&w, p.data(), sizeof(w));
            InjectWheel(w.delta, w.horizontal != 0);
          }
          break;
        case MSG_KEY:
          if (p.size() >= sizeof(Key)) {
            Key k;
            std::memcpy(&k, p.data(), sizeof(k));
            InjectKey(k.scan, k.flags);
          }
          break;
        case MSG_CURSOR_MODE:
          if (!p.empty()) {
            showCursor = p[0] == 0;
            g_object_set(vp.cap, "show-cursor", showCursor ? TRUE : FALSE, nullptr);
          }
          break;
        case MSG_SET_MODE:
          // The receiver's Mirror/Extend button: move the stream to the other screen.
          if (!p.empty() && (p[0] != 0) != extended) {
            const bool toExtend = p[0] != 0;
            MonitorInfo next{};
            bool ok = true;
            if (toExtend) {
              detach.on = true;
              ok = canExtend && VddAttach(ready.screenWidth, ready.screenHeight, ready.screenHz, next);
              if (!ok) VddDetach();
            }
            if (ok) {
              StopVideo(vp);  // stop capturing the old screen before it can disappear
              if (!toExtend) VddDetach();
              if (!toExtend && !FindMonitor(mirrorMon.device, next)) next = ListMonitors().front();
              mon = next;
              width = mon.rect.right - mon.rect.left;
              height = mon.rect.bottom - mon.rect.top;
              // Size first: the receiver resets for the new stream before its packets arrive.
              conn.Send(MSG_SIZE, VideoSize{static_cast<uint16_t>(width), static_cast<uint16_t>(height)});
              extended = toExtend;
              if (!StartVideo(vp, encName, opt, host, videoPort, mon, rc.bitrate, showCursor)) {
                connected = false;
                break;
              }
              std::printf("Switched to %s: sharing %s (%dx%d).\n", extended ? "extend" : "mirror",
                          mon.device.c_str(), width, height);
            } else {
              std::fprintf(stderr, "Couldn't switch to extend (no Virtual Display Driver on this PC?).\n");
            }
            conn.Send(MSG_MODE, ModeReport{static_cast<uint8_t>(extended), static_cast<uint8_t>(canExtend)});
          }
          break;
        case MSG_MIC:
          if (!p.empty() && p[0] && !mic && micAvailable) {
            // Like the receiver's own audio: no clock sync, a small jitter buffer that grows nothing.
            mic = Launch("udpsrc port=" + std::to_string(kMicPort) +
                             " name=msrc caps=\"application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,"
                             "payload=98\" ! "
                             "rtpjitterbuffer latency=40 drop-on-latency=true do-lost=true ! rtpopusdepay ! "
                             "opusdec plc=true ! audioconvert ! audioresample ! wasapi2sink low-latency=true sync=false" +
                             (micDevice.empty() ? std::string() : " device=\"" + micDevice + "\""),
                         "mic");
            if (mic) DecryptRtp(mic, "msrc", "src", g_keys.mic);
            if (mic && !Play(mic, "mic")) Stop(mic);
            if (mic) {
              std::printf("Receiver's mic is live here as \"%s\" - pick that microphone in your apps.\n",
                          micDevice.empty() ? "speakers (test)" : "Triton Mic");
            }
          } else if (!p.empty() && !p[0] && mic) {
            Stop(mic);
            std::printf("Receiver's mic is off.\n");
          }
          conn.Send(MSG_MIC_STATUS, MicStatus{static_cast<uint8_t>(micAvailable), static_cast<uint8_t>(mic != nullptr)});
          break;
        case MSG_CAMERA:
          if (!p.empty() && p[0] && !camera && cameraAvailable) {
            if (!cameraTest) StartVirtualCamera();  // stays until the session ends, so apps keep it
            camera = Launch("udpsrc port=" + std::to_string(kCameraPort) +
                                " name=csrc buffer-size=1048576 caps=\"application/x-rtp,media=video,clock-rate=90000,"
                                "encoding-name=H264,payload=99\" ! rtph264depay ! h264parse ! avdec_h264 name=cdec ! "
                                "videoconvert ! videoscale ! video/x-raw,format=NV12,width=1280,height=720 ! "
                                "fakesink name=csink sync=false signal-handoffs=true",
                            "camera");
            if (camera) {
              DecryptRtp(camera, "csrc", "src", g_keys.camera);
              GstElement* sink = gst_bin_get_by_name(GST_BIN(camera), "csink");
              g_signal_connect(sink, "handoff", G_CALLBACK(OnCameraFrame), nullptr);
              gst_object_unref(sink);
              GstElement* dec = gst_bin_get_by_name(GST_BIN(camera), "cdec");
              TrySet(dec, "thread-type", "slice");
              gst_object_unref(dec);
              if (!Play(camera, "camera")) Stop(camera);
            }
            if (camera) std::printf("Receiver's camera is live here as \"Triton Camera\".\n");
          } else if (!p.empty() && !p[0] && camera) {
            Stop(camera);
            std::printf("Receiver's camera is off.\n");
          }
          conn.Send(MSG_CAMERA_STATUS,
                    MicStatus{static_cast<uint8_t>(cameraAvailable), static_cast<uint8_t>(camera != nullptr)});
          break;
        case MSG_BYE:
          std::printf("Receiver closed the session.\n");
          connected = false;
          break;
        default:
          break;
      }
      if (!connected) break;
    }

    const Cursor cur = CurrentCursor();
    if (cur.shape != sentCursor.shape || cur.visible != sentCursor.visible) {
      conn.Send(MSG_CURSOR, cur);
      sentCursor = cur;
    }

    now = NowUs();
    if (now - lastStatus > 1'000'000) {
      Status st{};
      st.bitrateKbps = static_cast<uint32_t>(rc.bitrate);
      st.rttUs = rttUs;
      strncpy_s(st.encoder, encName.c_str(), _TRUNCATE);
      conn.Send(MSG_STATUS, st);
      lastStatus = now;
    }
    if (now - lastPrint > 2'000'000) {
      std::printf("target %5.1f Mbps | received %5.1f Mbps | %3u fps | loss %4.1f%% | delay %4.0f ms | RTT %5.1f ms",
                  rc.bitrate / 1000.0, last.recvKbps / 1000.0, last.fps, last.lossPermille / 10.0,
                  last.queueDelayUs / 1000.0, rttUs / 1000.0);
      if (camera) {
        const uint64_t frames = g_cameraFrames;
        std::printf(" | camera in %.0f fps", (frames - cameraFramesPrinted) / ((now - lastPrint) / 1e6));
        cameraFramesPrinted = frames;
      }
      std::printf("\n");
      lastPrint = now;
    }
  }

  conn.Send(MSG_BYE, nullptr, 0);
  Stop(camera);
  StopVirtualCamera();
  Stop(mic);
  Stop(audio);
  StopVideo(vp);
  std::printf("Stopped sharing.\n");
  return 0;
}

}  // namespace lfrd
