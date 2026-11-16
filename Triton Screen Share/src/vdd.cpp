#include "vdd.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cwctype>
#include <fstream>
#include <sstream>
#include <vector>

namespace lfrd {
namespace {

// From the driver source (MttVDD/Driver.cpp): a message-mode pipe open to everyone; commands are
// UTF-16 strings matched by prefix, e.g. L"PING" (answers PONG) or L"SETDISPLAYCOUNT 1".
constexpr wchar_t kPipe[] = L"\\\\.\\pipe\\MTTVirtualDisplayPipe";
constexpr wchar_t kSettings[] = L"C:\\VirtualDisplayDriver\\vdd_settings.xml";

std::wstring Lower(std::wstring s) {
  for (auto& c : s) c = static_cast<wchar_t>(std::towlower(c));
  return s;
}

// Sends one command; returns the raw reply bytes if `wantReply` (empty on failure or timeout).
std::string PipeCommand(const std::wstring& cmd, bool wantReply) {
  HANDLE h = CreateFileW(kPipe, GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
  if (h == INVALID_HANDLE_VALUE && GetLastError() == ERROR_PIPE_BUSY && WaitNamedPipeW(kPipe, 1000)) {
    h = CreateFileW(kPipe, GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
  }
  if (h == INVALID_HANDLE_VALUE) return {};
  DWORD mode = PIPE_READMODE_MESSAGE;
  SetNamedPipeHandleState(h, &mode, nullptr, nullptr);
  OVERLAPPED ov{};
  ov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  // Wait for an overlapped call with a timeout, so a stuck driver can't hang the app.
  auto finish = [&](BOOL started, DWORD& bytes) {
    if (!started && GetLastError() != ERROR_IO_PENDING) return false;
    if (WaitForSingleObject(ov.hEvent, 1500) != WAIT_OBJECT_0) {
      CancelIo(h);
      return false;
    }
    return GetOverlappedResult(h, &ov, &bytes, FALSE) != FALSE;
  };
  std::string reply;
  DWORD n = 0;
  const DWORD size = static_cast<DWORD>((cmd.size() + 1) * sizeof(wchar_t));  // with the terminator
  if (finish(WriteFile(h, cmd.c_str(), size, nullptr, &ov), n) && wantReply) {
    char buf[512];
    ResetEvent(ov.hEvent);
    if (finish(ReadFile(h, buf, sizeof(buf), nullptr, &ov), n)) reply.assign(buf, n);
  }
  CloseHandle(ov.hEvent);
  CloseHandle(h);
  return reply;
}

// The virtual adapter's display device (\\.\DISPLAYn), attached to the desktop or not.
bool FindVirtual(DISPLAY_DEVICEW& out) {
  for (DWORD i = 0;; ++i) {
    DISPLAY_DEVICEW dd{sizeof(dd)};
    if (!EnumDisplayDevicesW(nullptr, i, &dd, 0)) return false;
    const std::wstring name = Lower(dd.DeviceString);
    if (name.find(L"virtual display") != std::wstring::npos || name.find(L"mttvdd") != std::wstring::npos) {
      out = dd;
      return true;
    }
  }
}

struct Mode {
  int w, h, hz;
};

std::vector<Mode> Modes(const wchar_t* device) {
  std::vector<Mode> modes;
  DEVMODEW dm{};
  dm.dmSize = sizeof(dm);
  for (DWORD i = 0; EnumDisplaySettingsExW(device, i, &dm, 0); ++i) {
    modes.push_back({static_cast<int>(dm.dmPelsWidth), static_cast<int>(dm.dmPelsHeight),
                     static_cast<int>(dm.dmDisplayFrequency)});
  }
  return modes;
}

bool HasResolution(const std::vector<Mode>& modes, int w, int h) {
  return std::any_of(modes.begin(), modes.end(), [&](const Mode& m) { return m.w == w && m.h == h; });
}

// Adds width x height @ hz to the driver's resolution list. Only works where the settings file is
// writable by this user; otherwise the closest existing mode is used.
bool AddResolution(int w, int h, int hz) {
  std::wifstream in(kSettings);
  if (!in) return false;
  std::wstringstream ss;
  ss << in.rdbuf();
  in.close();
  std::wstring xml = ss.str();
  const size_t at = xml.find(L"</resolutions>");
  if (at == std::wstring::npos) return false;
  wchar_t entry[256];
  swprintf_s(entry, L"    <resolution>\n      <width>%d</width>\n      <height>%d</height>\n      "
                    L"<refresh_rate>%d</refresh_rate>\n    </resolution>\n  ", w, h, hz);
  xml.insert(at, entry);
  std::wofstream outFile(kSettings, std::ios::trunc);
  if (!outFile) return false;
  outFile << xml;
  return static_cast<bool>(outFile);
}

// Exact resolution with the refresh rate nearest `hz`; else the largest mode that fits inside
// w x h with the closest shape; else the first one.
Mode PickMode(const std::vector<Mode>& modes, int w, int h, int hz) {
  const Mode* best = nullptr;
  for (const Mode& m : modes) {
    if (m.w == w && m.h == h && (!best || std::abs(m.hz - hz) < std::abs(best->hz - hz))) best = &m;
  }
  if (best) return *best;
  const double aspect = static_cast<double>(w) / h;
  double bestScore = -1;
  for (const Mode& m : modes) {
    if (m.w > w || m.h > h) continue;
    const double score = static_cast<double>(m.w) * m.h / (1.0 + 4 * std::abs(static_cast<double>(m.w) / m.h - aspect)) -
                         std::abs(m.hz - hz);
    if (score > bestScore) {
      bestScore = score;
      best = &m;
    }
  }
  if (best) return *best;
  return modes.empty() ? Mode{1920, 1080, 60} : modes.front();
}

bool Apply(const wchar_t* device, DEVMODEW& dm) {
  const LONG r = ChangeDisplaySettingsExW(device, &dm, nullptr, CDS_UPDATEREGISTRY | CDS_NORESET, nullptr);
  if (r != DISP_CHANGE_SUCCESSFUL) {
    std::fprintf(stderr, "Windows refused the display change (code %ld).\n", r);
    return false;
  }
  ChangeDisplaySettingsExW(nullptr, nullptr, nullptr, 0, nullptr);
  return true;
}

}  // namespace

bool VddAvailable() {
  DISPLAY_DEVICEW dd;
  if (FindVirtual(dd)) return true;
  const std::string reply = PipeCommand(L"PING", true);
  return reply.find("PONG") != std::string::npos || reply.find(std::string("P\0O\0N\0G", 7)) != std::string::npos;
}

std::string VddStatus() {
  const std::string reply = PipeCommand(L"PING", true);
  const bool pong = reply.find("PONG") != std::string::npos ||
                    reply.find(std::string("P\0O\0N\0G", 7)) != std::string::npos;
  DISPLAY_DEVICEW dd;
  if (!FindVirtual(dd)) {
    return pong ? "driver answers, but has no virtual screen yet (Extend will ask it for one)"
                : "NOT installed (install the Virtual Display Driver to use Extend)";
  }
  std::string s = "ready - " + Narrow(dd.DeviceName) + " \"" + Narrow(dd.DeviceString) + "\", ";
  s += (dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP) ? "on the desktop now" : "off the desktop (until you extend)";
  s += pong ? ", control pipe OK" : ", control pipe not answering";
  return s;
}

bool VddAttach(int width, int height, int hz, MonitorInfo& out) {
  DISPLAY_DEVICEW dd;
  if (!FindVirtual(dd)) {
    // Installed but with no monitor (count was changed): ask for one and wait for it.
    PipeCommand(L"SETDISPLAYCOUNT 1", false);
    for (int i = 0; i < 50 && !FindVirtual(dd); ++i) Sleep(100);
    if (!FindVirtual(dd)) {
      std::fprintf(stderr,
                   "The Virtual Display Driver isn't installed (or isn't running) on this PC.\n"
                   "Install it once from github.com/VirtualDrivers/Virtual-Display-Driver/releases.\n");
      return false;
    }
  }
  if (width <= 0 || height <= 0) width = 1920, height = 1080;
  if (hz <= 0) hz = 60;

  std::vector<Mode> modes = Modes(dd.DeviceName);
  if (!HasResolution(modes, width, height) && AddResolution(width, height, hz)) {
    // The reload recreates the virtual screen under a NEW name (\.\DISPLAY5 became DISPLAY6 in a
    // test), so wait until a virtual screen offering the new size exists and use that one.
    PipeCommand(L"RELOAD_DRIVER", false);
    for (int i = 0; i < 100; ++i) {
      Sleep(100);
      if (FindVirtual(dd) && HasResolution(modes = Modes(dd.DeviceName), width, height)) break;
    }
    if (!FindVirtual(dd)) {
      std::fprintf(stderr, "The virtual screen didn't come back after reloading the driver.\n");
      return false;
    }
    modes = Modes(dd.DeviceName);
  }
  const Mode mode = PickMode(modes, width, height, hz);

  // To the right of everything else, top-aligned with the main screen.
  LONG right = 0;
  for (const MonitorInfo& m : ListMonitors()) {
    if (Narrow(dd.DeviceName) != m.device) right = std::max(right, m.rect.right);
  }
  DEVMODEW dm{};
  dm.dmSize = sizeof(dm);
  dm.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY;
  dm.dmPosition.x = right;
  dm.dmPosition.y = 0;
  dm.dmPelsWidth = mode.w;
  dm.dmPelsHeight = mode.h;
  dm.dmDisplayFrequency = mode.hz;
  if (!Apply(dd.DeviceName, dm)) return false;

  for (int i = 0; i < 50; ++i) {
    const std::string device = FindVirtual(dd) ? Narrow(dd.DeviceName) : std::string();  // name may change
    for (const MonitorInfo& m : ListMonitors()) {
      if (device.empty()) break;
      if (m.device == device) {
        out = m;
        std::printf("Extended desktop: virtual screen %s, %dx%d @ %d Hz, to the right of your screen%s.\n",
                    device.c_str(), mode.w, mode.h, mode.hz,
                    mode.w == width && mode.h == height ? "" : " (closest size the driver offers)");
        return true;
      }
    }
    Sleep(100);
  }
  std::fprintf(stderr, "The virtual screen didn't appear.\n");
  return false;
}

void VddDetach() {
  DISPLAY_DEVICEW dd;
  if (!FindVirtual(dd) || !(dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP)) return;
  DEVMODEW dm{};
  dm.dmSize = sizeof(dm);
  dm.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT;  // all zero = off the desktop
  Apply(dd.DeviceName, dm);
}

}  // namespace lfrd
