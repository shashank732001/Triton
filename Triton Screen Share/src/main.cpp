// Triton Screen Share: one app, two modes.
//   TritonScreenShare.exe              window for choosing a mode (no console)
//   TritonScreenShare.exe --background just the bubble, panel closed (how Nova Launcher starts it)
//   TritonScreenShare.exe menu         interactive console menu
//   TritonScreenShare.exe share ...    send this screen
//   TritonScreenShare.exe show ...     show a screen here
//   TritonScreenShare.exe check        what this device supports
#include <gst/gst.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>

#include "common.h"
#include "gst_util.h"
#include "vdd.h"

using namespace lfrd;

namespace {

BOOL WINAPI OnConsoleCtrl(DWORD) {
  g_stop = true;
  if (HWND h = g_notifyHwnd) PostMessageW(h, WM_CLOSE, 0, 0);
  return TRUE;
}

void PrintUsage() {
  std::printf(
      "Usage:\n"
      "  TritonScreenShare                     open the window\n"
      "  TritonScreenShare menu                interactive console menu\n"
      "  TritonScreenShare share [options]     share this screen\n"
      "      --to <ip>            receiver address (omit to search the network)\n"
      "      --monitor <n>        monitor index from 'check' (default: primary)\n"
      "      --fps <n>            frames per second (default 60)\n"
      "      --bitrate <kbps>     starting bitrate (default 12000)\n"
      "      --max-bitrate <kbps> ceiling for adaptation (default 40000)\n"
      "      --min-bitrate <kbps> floor for adaptation (default 3000)\n"
      "      --fixed-bitrate      disable adaptive bitrate\n"
      "      --codec h264|h265    (default h264)\n"
      "      --encoder <name>     force a GStreamer encoder, e.g. x264enc\n"
      "      --no-audio           video only\n"
      "      --extend             extend the desktop onto the receiver (needs the Virtual Display Driver)\n"
      "  TritonScreenShare show [options]      show a screen here\n"
      "      --jitter-ms <n>      reorder buffer for unstable Wi-Fi (default 0 = off)\n"
      "      --fullscreen         start fullscreen\n"
      "      --decoder <name>     force a GStreamer decoder, e.g. d3d11h264dec\n"
      "      --no-input           don't forward mouse/keyboard\n"
      "      --no-audio           don't play audio\n"
      "  TritonScreenShare check               list monitors, encoders, decoders, addresses\n"
      "Both: --port <n> (control port, default 47990), --seconds <n> (auto-stop, for testing)\n");
}

int Check() {
  gchar* version = gst_version_string();
  std::printf("%s\n\nMonitors:\n", version);
  g_free(version);
  auto monitors = ListMonitors();
  for (size_t i = 0; i < monitors.size(); ++i) {
    const RECT& r = monitors[i].rect;
    std::printf("  [%zu] %s  %ldx%ld at (%ld,%ld)%s\n", i, monitors[i].device.c_str(), r.right - r.left,
                r.bottom - r.top, r.left, r.top, monitors[i].primary ? "  primary" : "");
  }
  for (Codec c : {Codec::H264, Codec::H265}) {
    const char* label = c == Codec::H264 ? "H.264" : "H.265";
    const std::string enc = ChooseEncoder(c, ""), dec = ChooseDecoder(c);
    std::printf("\n%s encoders:", label);
    for (const char* n : EncoderCandidates(c)) {
      if (HasElement(n)) std::printf(" %s%s", n, enc == n ? "*" : "");
    }
    std::printf("\n%s decoders:", label);
    for (const char* n : DecoderCandidates(c)) {
      if (HasElement(n)) std::printf(" %s%s", n, dec == n ? "*" : "");
    }
  }
  std::printf("\n(* = will be used)\n\nScreen capture: %s\nAudio capture:  %s\n",
              HasElement("d3d11screencapturesrc") ? "d3d11screencapturesrc" : "MISSING",
              HasElement("wasapi2src") ? "wasapi2src" : "MISSING");
  std::printf("Extend:         %s\n", VddStatus().c_str());
  std::printf("\nThis device's addresses:\n");
  for (const auto& a : LocalAddresses()) std::printf("  %-15s  (%s)\n", a.ip.c_str(), a.adapter.c_str());
  return 0;
}

std::string Prompt(const char* text) {
  std::printf("%s", text);
  std::string line;
  std::getline(std::cin, line);
  return line;
}

bool PromptSender(SenderOptions& o) {
  auto monitors = ListMonitors();
  if (monitors.size() > 1) {
    std::printf("\nMonitors:\n");
    for (size_t i = 0; i < monitors.size(); ++i) {
      const RECT& r = monitors[i].rect;
      std::printf("  %zu) %s  %ldx%ld%s\n", i, monitors[i].device.c_str(), r.right - r.left,
                  r.bottom - r.top, monitors[i].primary ? "  (primary)" : "");
    }
    std::string m = Prompt("Monitor to share [Enter = primary]: ");
    if (!m.empty()) o.monitor = std::atoi(m.c_str());
  }

  std::printf("\nLooking for devices running 'Show a screen here'...\n");
  auto found = Discover(2500);
  found.erase(std::remove_if(found.begin(), found.end(), [](const FoundReceiver& f) { return f.sender; }), found.end());
  for (size_t i = 0; i < found.size(); ++i) {
    std::printf("  %zu) %s  (%s)\n", i + 1, found[i].name.c_str(), found[i].ip.c_str());
  }
  if (found.empty()) std::printf("  none found (a VPN or firewall can hide them; you can type the IP).\n");

  std::string pick = Prompt(found.empty() ? "Receiver IP address: " : "Pick a number, or type an IP address: ");
  if (pick.empty()) return false;
  int n = std::atoi(pick.c_str());
  if (pick.find('.') == std::string::npos && n >= 1 && n <= static_cast<int>(found.size())) {
    o.to = found[n - 1].ip;
    o.port = found[n - 1].port;
  } else {
    o.to = pick;
  }
  return true;
}

int Interactive() {
  std::printf(
      "Triton Screen Share\n"
      "  1) Share this screen\n"
      "  2) Show a screen here\n"
      "  3) Check this device\n");
  std::string c = Prompt("> ");
  if (c == "1") {
    SenderOptions o;
    if (!PromptSender(o)) return 1;
    return RunSender(o);
  }
  if (c == "2") return RunReceiver(ReceiverOptions{});
  if (c == "3") return Check();
  return 0;
}

bool HasGstDll(const std::wstring& bin) {
  return GetFileAttributesW((bin + L"\\gstreamer-1.0-0.dll").c_str()) != INVALID_FILE_ATTRIBUTES;
}

// Points this process at the GStreamer copy bundled next to the exe (else an installed one), the
// same way _env.bat does. The GStreamer DLLs are delay-loaded, so this runs before any of them.
bool SetupGStreamer() {
  wchar_t exe[MAX_PATH];
  GetModuleFileNameW(nullptr, exe, MAX_PATH);
  std::wstring root = exe;
  root = root.substr(0, root.find_last_of(L'\\')) + L"\\gstreamer";
  std::wstring bin = root + L"\\bin";
  if (HasGstDll(bin)) {
    SetEnvironmentVariableW(L"GST_PLUGIN_SYSTEM_PATH_1_0", (root + L"\\lib\\gstreamer-1.0").c_str());
    SetEnvironmentVariableW(L"GST_PLUGIN_PATH_1_0", nullptr);
    SetEnvironmentVariableW(L"GST_PLUGIN_SCANNER_1_0", (root + L"\\libexec\\gstreamer-1.0\\gst-plugin-scanner.exe").c_str());
    // Own plugin cache, so an installed GStreamer of another version never gets mixed in.
    wchar_t local[MAX_PATH];
    if (GetEnvironmentVariableW(L"LOCALAPPDATA", local, MAX_PATH)) {
      const std::wstring dir = std::wstring(local) + L"\\TritonScreenShare";
      CreateDirectoryW(dir.c_str(), nullptr);
      SetEnvironmentVariableW(L"GST_REGISTRY_1_0", (dir + L"\\gst-registry.bin").c_str());
    }
  } else {
    wchar_t installed[MAX_PATH];
    bin.clear();
    if (GetEnvironmentVariableW(L"GSTREAMER_1_0_ROOT_MSVC_X86_64", installed, MAX_PATH)) {
      bin = installed;
      if (!bin.empty() && bin.back() != L'\\') bin += L'\\';
      bin += L"bin";
    }
    if (bin.empty() || !HasGstDll(bin)) {
      wchar_t pf[MAX_PATH] = L"C:\\Program Files";
      GetEnvironmentVariableW(L"ProgramFiles", pf, MAX_PATH);
      bin = std::wstring(pf) + L"\\gstreamer\\1.0\\msvc_x86_64\\bin";
    }
    if (!HasGstDll(bin)) return false;
  }
  SetDllDirectoryW(bin.c_str());
  // Plugins load their own dependencies by PATH search.
  std::wstring path = bin;
  wchar_t old[32767];
  if (GetEnvironmentVariableW(L"PATH", old, 32767)) path += L";" + std::wstring(old);
  SetEnvironmentVariableW(L"PATH", path.c_str());
  return true;
}

// The exe is a GUI program (no console flashes up for the window); command-line modes borrow the
// console they were started from, or open one.
void UseConsole() {
  // Output already going to a file or pipe (e.g. "TritonScreenShare check > out.txt") stays there.
  const DWORD outType = GetFileType(GetStdHandle(STD_OUTPUT_HANDLE));
  const bool redirected = outType == FILE_TYPE_DISK || outType == FILE_TYPE_PIPE;
  if (!AttachConsole(ATTACH_PARENT_PROCESS) && !redirected) AllocConsole();
  FILE* f;
  if (!redirected) {
    freopen_s(&f, "CONOUT$", "w", stdout);
    freopen_s(&f, "CONOUT$", "w", stderr);
  }
  freopen_s(&f, "CONIN$", "r", stdin);
  std::cin.clear();
  SetConsoleOutputCP(CP_UTF8);
  SetConsoleCtrlHandler(OnConsoleCtrl, TRUE);
}

}  // namespace

int main(int argc, char** argv) {
  const bool background = argc == 2 && std::string(argv[1]) == "--background";
  const bool window = argc < 2 || background;
  if (!window) UseConsole();
  // Physical pixels everywhere, so capture, window and mouse coordinates agree.
  SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
  SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);
  if (!SetupGStreamer()) {
    const char* text =
        "The \"gstreamer\" folder next to TritonScreenShare.exe is missing.\n"
        "Copy the complete TritonScreenShare folder again (including its gstreamer subfolder).";
    if (window) MessageBoxA(nullptr, text, "Triton Screen Share", MB_ICONERROR);
    else std::fprintf(stderr, "%s\n", text);
    return 1;
  }
  if (!InitWinsock()) {
    std::fprintf(stderr, "Winsock failed to start.\n");
    return 1;
  }
  gst_init(&argc, &argv);

  if (window) return RunLauncher(!background);
  if (std::string(argv[1]) == "menu") return Interactive();

  const std::string mode = argv[1];
  SenderOptions so;
  ReceiverOptions ro;
  for (int i = 2; i < argc; ++i) {
    const std::string a = argv[i];
    auto next = [&]() -> std::string {
      if (i + 1 >= argc) {
        std::fprintf(stderr, "%s needs a value\n", a.c_str());
        std::exit(2);
      }
      return argv[++i];
    };
    if (a == "--to") so.to = next();
    else if (a == "--port") so.port = ro.port = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--monitor") so.monitor = std::stoi(next());
    else if (a == "--fps") so.fps = std::stoi(next());
    else if (a == "--bitrate") so.bitrate = std::stoi(next());
    else if (a == "--max-bitrate") so.maxBitrate = std::stoi(next());
    else if (a == "--min-bitrate") so.minBitrate = std::stoi(next());
    else if (a == "--fixed-bitrate") so.adaptive = false;
    else if (a == "--codec") so.codec = next() == "h265" ? Codec::H265 : Codec::H264;
    else if (a == "--encoder") so.encoder = next();
    else if (a == "--decoder") ro.decoder = next();
    else if (a == "--no-audio") so.audio = ro.audio = false;
    else if (a == "--jitter-ms") ro.jitterMs = std::stoi(next());
    else if (a == "--fullscreen") ro.fullscreen = true;
    else if (a == "--extend") so.extend = true;
    else if (a == "--no-input") ro.input = false;
    else if (a == "--seconds") so.seconds = ro.seconds = std::stoi(next());
    else {
      std::fprintf(stderr, "Unknown option %s\n\n", a.c_str());
      PrintUsage();
      return 2;
    }
  }
  so.maxBitrate = std::max(so.maxBitrate, so.bitrate);

  if (mode == "share") {
    if (so.to.empty() && !PromptSender(so)) return 1;
    return RunSender(so);
  }
  if (mode == "show") return RunReceiver(ro);
  if (mode == "check") return Check();
  PrintUsage();
  return mode == "help" || mode == "--help" || mode == "-h" ? 0 : 2;
}
