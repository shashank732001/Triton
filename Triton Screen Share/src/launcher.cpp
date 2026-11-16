// What you get by double-clicking TritonScreenShare.exe: a small always-on-top bubble (like
// AssistiveTouch). Clicking it opens a borderless panel to share, show or stop; clicking anywhere
// else hides the panel again. The modes are the same RunSender/RunReceiver the command line uses;
// they run on a worker thread and their console output feeds the panel's status lines.
//
// The panel is drawn by hand with GDI+ (no child controls), in Nova Launcher's dark-glass style:
// on Windows 11 it sits on the blurred "acrylic" backdrop with DWM's rounded corners; elsewhere it
// falls back to a solid dark sheet.
#include "common.h"  // first: brings in windows.h

#include <dwmapi.h>
#include <sddl.h>
#include <fcntl.h>
#include <io.h>
#include <share.h>
#include <objbase.h>
#include <objidl.h>
#include <uxtheme.h>
#include <windowsx.h>

#include <algorithm>
namespace Gdiplus {  // gdiplus.h expects the min/max macros that NOMINMAX turns off
using std::max;
using std::min;
}  // namespace Gdiplus
#include <gdiplus.h>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <functional>
#include <future>
#include <memory>
#include <thread>

#include "firewall.h"
#include "gst_util.h"
#include "secure.h"
#include "vdd.h"

#pragma comment(linker, "\"/manifestdependency:type='win32' name='Microsoft.Windows.Common-Controls' " \
                        "version='6.0.0.0' processorArchitecture='*' publicKeyToken='6595b64144ccf1df' language='*'\"")

namespace lfrd {
namespace {

namespace gp = Gdiplus;

constexpr UINT WM_APP_LOG = WM_APP + 20;    // lParam: std::string* (output text)
constexpr UINT WM_APP_FOUND = WM_APP + 21;  // lParam: std::vector<FoundReceiver>*
constexpr UINT WM_APP_DONE = WM_APP + 22;   // a mode finished; wParam = its exit code
constexpr UINT WM_APP_OPEN = WM_APP + 23;   // sent to the bubble by a second copy of the app
constexpr UINT WM_APP_QUIT = WM_APP + 24;   // the quit event was set (e.g. by Nova Launcher closing)
constexpr UINT WM_APP_PULL = WM_APP + 25;   // lParam: PendingPull* (another PC asks for this screen)
constexpr UINT WM_APP_PULL_RESULT = WM_APP + 26;  // wParam: PullReply result or 3 = unreachable; lParam: std::wstring* name
constexpr UINT_PTR kPullTimeoutTimer = 4;           // panel: an unanswered request is declined
constexpr UINT_PTR kRelayoutTimer = 5;              // panel: refresh after the receiver acted
constexpr UINT WM_APP_DEVICES = WM_APP + 27;  // lParam: DeviceLists* (this PC's mics and webcams)
constexpr UINT WM_APP_RAISE = WM_APP + 28;    // panel: back on top after our picture window took the focus
constexpr UINT WM_APP_PAIR = WM_APP + 29;     // lParam: std::shared_ptr<PairAsk>* (show it), or 0 = withdraw it
constexpr UINT WM_APP_CONTROL = WM_APP + 30;  // lParam: ControlRequest* (a command on the control pipe)
constexpr UINT_PTR kFadeTimer = 1, kTopmostTimer = 2;  // bubble
constexpr UINT_PTR kCaretTimer = 3;                    // panel
constexpr UINT_PTR kVoiceTimer = 6;                    // panel: finishes a spoken "show my laptop here"
constexpr wchar_t kControlPipe[] = L"\\\\.\\pipe\\TritonScreenShare";
constexpr wchar_t kPanelClass[] = L"TritonScreenSharePanel";
constexpr wchar_t kBubbleClass[] = L"TritonScreenShareBubble";
constexpr wchar_t kQuitEvent[] = L"TritonScreenShare.Quit";
constexpr wchar_t kIdleStatus[] = L"Share this screen, or show another PC here.";
constexpr int kBubbleSize = 56;  // 96-DPI pixels
constexpr int kPanelWidth = 340;
constexpr size_t kMaxRows = 4;   // PCs listed in the panel

enum Id {
  ID_NONE, ID_FW_FIX, ID_IP, ID_MONITOR, ID_AUDIO, ID_SHARE, ID_EXTEND, ID_SHOW, ID_HIDE, ID_FULL, ID_MUTE,
  ID_MODE, ID_STOP, ID_QUIT, ID_ROLE, ID_ALLOW, ID_ALWAYS, ID_DENY, ID_MIC, ID_CAMERA, ID_MIC_PICK, ID_CAMERA_PICK,
  ID_AUTOSTART, ID_LOGS, ID_PAIR_OK, ID_PAIR_NO, ID_PAIR_NEW,
  ID_ROW = 100,         // + index into Launcher::found (PCs to share to)
  ID_SENDER_ROW = 200,  // + index into Launcher::senders (PCs to show here)
};
enum class Kind { Primary, Button, Danger, Link, Row, Field, Cycle, Toggle, Dropdown };
enum class Text { Title, Status, Detail, Section, Warning, Empty, Rule };

// Everything is laid out in 96-DPI units; painting scales to the real DPI.
struct Widget {
  int id;
  Kind kind;
  gp::RectF r;
  std::wstring text;
  bool enabled;
};
struct Label {
  Text style;
  gp::RectF r;
  std::wstring text;  // Status and Detail use the live Launcher::status / detail instead
};

enum class Mode { Idle, Sharing, Showing };

// "Start as": what the app does when it opens. Saved in HKCU\Software\TritonScreenShare.
enum class Role { Ask = 0, AlwaysShare = 1, AlwaysShow = 2 };

// A PC this one hasn't paired with connected (secure.cpp): both show `code`; the handshake thread
// waits for the user's answer here.
struct PairAsk {
  std::string name, code;
  std::promise<bool> answer;
};

// One line from the control pipe (the voice assistant), answered on the panel's thread.
struct ControlRequest {
  std::string command;
  std::promise<std::string> reply;
};

// Another PC asked (on kPullPort) to show this screen; waiting for the user's answer here.
struct PendingPull {
  std::shared_ptr<Connection> conn;
  std::string ip, name;
  uint16_t port = 0;
};

struct Launcher {
  HWND hwnd = nullptr;  // the panel
  HWND bubble = nullptr;
  HANDLE quitEvent = nullptr;
  int dpi = 96;
  bool glass = false;  // acrylic backdrop available (Windows 11)
  std::unique_ptr<gp::FontFamily> regular, semibold;
  std::vector<Widget> widgets;
  std::vector<Label> labels;
  int hover = ID_NONE, press = ID_NONE, focus = ID_NONE;
  bool keyboard = false;  // show the focus ring (after Tab / arrows)
  bool caretOn = true, tracking = false;
  std::wstring status = kIdleStatus, detail, ip;
  bool showDetail = false;  // keep the last output line visible after an error
  int sel = -1, monitor = 0;
  bool audio = true;
  std::vector<FoundReceiver> found;    // PCs showing screens (to share to)
  std::vector<FoundReceiver> senders;  // PCs that can be asked to share (to show here)
  std::vector<MonitorInfo> monitors;
  Role role = Role::Ask;
  bool autoShare = false;        // "share automatically": share as soon as a PC clicks "Show here"
  ULONGLONG autoShareAfter = 0;  // no automatic attempt before this (GetTickCount64)
  std::string lastShared;        // the PC shared with last; preferred when several show
  bool autostart = false;        // "Start with Windows" (HKCU Run value)
  std::vector<std::string> trusted;     // PCs allowed to show this screen without asking
  std::unique_ptr<PendingPull> pending;  // a request waiting for Allow / Deny
  std::shared_ptr<PairAsk> pairAsk;      // a pairing waiting for "codes match" / Cancel
  std::wstring statusBeforePair;
  std::atomic<bool> advertise{false};    // idle: announce "this PC can share"
  std::atomic<bool> answerShow{false};   // showing: tell PCs that ask that they can share to us
  std::mutex knownMu;
  std::vector<std::string> knownIps;     // PCs shared with before, newest first: asked directly
  std::thread pullListener;
  // This PC's microphones and webcams, listed whenever the panel opens while showing.
  std::vector<MediaDevice> mics, cameras;
  bool devicesKnown = false;
  std::atomic<bool> listingDevices{false};
  std::string micPick, cameraPick;  // MediaDevice::id chosen in the panel
  // A spoken "show / extend my laptop here" (control pipe): what to do once the other PC is on screen.
  int wantView = -1;              // -1 = nothing, 0 = mirror, 1 = extend
  ULONGLONG wantUntil = 0;        // give up after this (GetTickCount64)
  ULONGLONG pullAt = 0;           // ask a paired PC to share at this time, unless it already did by itself
  ULONGLONG sessionSince = 0;     // when the picture arrived (the sender reports "can extend" just after)
  std::thread worker, discovery;
  Mode mode = Mode::Idle;
  bool firewallNeedsFix = false;
  std::string pendingLine;  // output not yet ended by a newline
  std::atomic<bool> busy{false}, closing{false};
  // Bubble dragging / fading.
  POINT dragFrom{}, bubbleFrom{};
  bool pressed = false, dragging = false, hovered = false;
  BYTE alpha = 255;
};
Launcher L;

// Nova Launcher's palette.
const gp::Color kCyan(255, 64, 200, 255), kViolet(255, 128, 96, 255), kMint(255, 52, 211, 153),
    kAccent(255, 154, 216, 255), kDanger(255, 255, 84, 108), kAmber(255, 255, 204, 120);

int Px(int v) { return MulDiv(v, L.dpi, 96); }
gp::Color Alpha(const gp::Color& c, int a) {
  return gp::Color(static_cast<BYTE>(std::clamp(a, 0, 255)), c.GetR(), c.GetG(), c.GetB());
}
gp::Color White(int a) { return Alpha(gp::Color(255, 255, 255, 255), a); }

std::wstring Wide(const std::string& s) {
  std::wstring w(s.size(), L'\0');
  w.resize(MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), static_cast<int>(w.size())));
  return w;
}

void Invalidate() {
  if (L.hwnd) InvalidateRect(L.hwnd, nullptr, FALSE);
}

// ---------------------------------------------------------------------------
// Logs: every line the modes print, with the time, shown by "Show logs" in a window of its own.

constexpr wchar_t kLogClass[] = L"TritonScreenShareLog";
constexpr size_t kMaxLogLines = 5000;  // ~80 minutes of the once-a-second stats lines
std::deque<std::wstring> g_log;
HWND g_logWnd = nullptr, g_logEdit = nullptr;
HFONT g_logFont = nullptr;
HBRUSH g_logBrush = nullptr;

std::wstring LogText() {
  std::wstring all;
  for (const std::wstring& line : g_log) all += line + L"\r\n";
  return all;
}

void ScrollLogToEnd() {
  const int len = GetWindowTextLengthW(g_logEdit);
  SendMessageW(g_logEdit, EM_SETSEL, len, len);
  SendMessageW(g_logEdit, EM_SCROLLCARET, 0, 0);
}

// The same lines also go to %LOCALAPPDATA%\TritonScreenShare\log.txt (the previous run's is kept as
// log-previous.txt), so a problem can be looked at afterwards or sent on. Stops at 20 MB.
void WriteLogFile(const SYSTEMTIME& t, const std::string& line) {
  static FILE* file = [] {
    FILE* f = nullptr;
    wchar_t local[MAX_PATH];
    if (!GetEnvironmentVariableW(L"LOCALAPPDATA", local, MAX_PATH)) return f;
    const std::wstring dir = std::wstring(local) + L"\\TritonScreenShare";
    CreateDirectoryW(dir.c_str(), nullptr);
    MoveFileExW((dir + L"\\log.txt").c_str(), (dir + L"\\log-previous.txt").c_str(), MOVEFILE_REPLACE_EXISTING);
    f = _wfsopen((dir + L"\\log.txt").c_str(), L"w", _SH_DENYWR);  // others may read it meanwhile
    return f;
  }();
  if (!file || std::ftell(file) > 20 * 1024 * 1024) return;
  std::fprintf(file, "%04u-%02u-%02u %02u:%02u:%02u  %s\n", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond,
               line.c_str());
  std::fflush(file);
}

// Panel thread. While the caret is at the end the window follows new lines; click into the text to
// stop following (to read or select), Ctrl+End to follow again.
void AppendLog(const std::string& line) {
  SYSTEMTIME t;
  GetLocalTime(&t);
  WriteLogFile(t, line);
  wchar_t stamp[16];
  swprintf_s(stamp, L"%02u:%02u:%02u  ", t.wHour, t.wMinute, t.wSecond);
  g_log.push_back(stamp + Wide(line));
  while (g_log.size() > kMaxLogLines) g_log.pop_front();
  if (!g_logEdit) return;

  const int len = GetWindowTextLengthW(g_logEdit);
  DWORD selStart = 0, selEnd = 0;
  SendMessageW(g_logEdit, EM_GETSEL, reinterpret_cast<WPARAM>(&selStart), reinterpret_cast<LPARAM>(&selEnd));
  const bool follow = selStart == selEnd && selEnd == static_cast<DWORD>(len);
  if (SendMessageW(g_logEdit, EM_GETLINECOUNT, 0, 0) > static_cast<LRESULT>(kMaxLogLines + 500)) {
    SetWindowTextW(g_logEdit, LogText().c_str());  // drop what the list dropped
    if (follow) ScrollLogToEnd();
    return;
  }
  const int first = static_cast<int>(SendMessageW(g_logEdit, EM_GETFIRSTVISIBLELINE, 0, 0));
  SendMessageW(g_logEdit, WM_SETREDRAW, FALSE, 0);
  SendMessageW(g_logEdit, EM_SETSEL, len, len);
  SendMessageW(g_logEdit, EM_REPLACESEL, FALSE, reinterpret_cast<LPARAM>((g_log.back() + L"\r\n").c_str()));
  if (!follow) {  // put the reader's selection and scroll position back
    SendMessageW(g_logEdit, EM_SETSEL, selStart, selEnd);
    const int now = static_cast<int>(SendMessageW(g_logEdit, EM_GETFIRSTVISIBLELINE, 0, 0));
    SendMessageW(g_logEdit, EM_LINESCROLL, 0, first - now);
  }
  SendMessageW(g_logEdit, WM_SETREDRAW, TRUE, 0);
  InvalidateRect(g_logEdit, nullptr, TRUE);
}

LRESULT CALLBACK LogProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
  switch (msg) {
    case WM_CREATE: {
      g_logEdit = CreateWindowExW(0, L"EDIT", nullptr,
                                  WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_HSCROLL | ES_MULTILINE | ES_READONLY |
                                      ES_AUTOVSCROLL | ES_AUTOHSCROLL | ES_NOHIDESEL,
                                  0, 0, 0, 0, hwnd, nullptr, GetModuleHandleW(nullptr), nullptr);
      SendMessageW(g_logEdit, EM_SETLIMITTEXT, 0, 0);  // no 32 KB limit
      SetWindowTheme(g_logEdit, L"DarkMode_Explorer", nullptr);  // dark scrollbars
      if (!g_logFont) {
        g_logFont = CreateFontW(-MulDiv(10, GetDpiForWindow(hwnd), 72), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                                FIXED_PITCH, L"Consolas");
      }
      if (!g_logBrush) g_logBrush = CreateSolidBrush(RGB(22, 24, 30));
      SendMessageW(g_logEdit, WM_SETFONT, reinterpret_cast<WPARAM>(g_logFont), FALSE);
      SetWindowTextW(g_logEdit, LogText().c_str());
      ScrollLogToEnd();
      return 0;
    }
    case WM_SIZE:
      MoveWindow(g_logEdit, 0, 0, LOWORD(lp), HIWORD(lp), TRUE);
      return 0;
    case WM_SETFOCUS:
      SetFocus(g_logEdit);
      return 0;
    case WM_CTLCOLORSTATIC:  // a read-only edit asks for "static" colours
      SetTextColor(reinterpret_cast<HDC>(wp), RGB(214, 222, 235));
      SetBkColor(reinterpret_cast<HDC>(wp), RGB(22, 24, 30));
      return reinterpret_cast<LRESULT>(g_logBrush);
    case WM_DESTROY:
      g_logWnd = g_logEdit = nullptr;
      return 0;
  }
  return DefWindowProcW(hwnd, msg, wp, lp);
}

// Opens the log window (or brings it back). It stays on top of the picture and, like the panel,
// never appears in the shared screen.
void ShowLogs() {
  if (g_logWnd) {
    ShowWindow(g_logWnd, SW_RESTORE);
    SetForegroundWindow(g_logWnd);
    return;
  }
  static bool registered = false;
  if (!registered) {
    WNDCLASSEXW wc{sizeof(wc)};
    wc.lpfnWndProc = LogProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hIcon = LoadIconW(nullptr, IDI_APPLICATION);
    wc.hbrBackground = CreateSolidBrush(RGB(22, 24, 30));
    wc.lpszClassName = kLogClass;
    RegisterClassExW(&wc);
    registered = true;
  }
  RECT bubble;
  GetWindowRect(L.bubble, &bubble);
  MONITORINFO mi{sizeof(mi)};
  GetMonitorInfoW(MonitorFromRect(&bubble, MONITOR_DEFAULTTONEAREST), &mi);
  const RECT& wa = mi.rcWork;
  const int w = std::min(Px(960), static_cast<int>(wa.right - wa.left) - Px(40));
  const int h = std::min(Px(560), static_cast<int>(wa.bottom - wa.top) - Px(40));
  g_logWnd = CreateWindowExW(WS_EX_TOPMOST, kLogClass, L"Triton Screen Share - logs", WS_OVERLAPPEDWINDOW,
                             wa.left + (wa.right - wa.left - w) / 2, wa.top + (wa.bottom - wa.top - h) / 2, w, h,
                             nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
  if (!g_logWnd) return;
  const BOOL dark = TRUE;  // DWMWA_USE_IMMERSIVE_DARK_MODE
  DwmSetWindowAttribute(g_logWnd, 20, &dark, sizeof(dark));
  if (!std::getenv("LFRD_TEST_CAPTURABLE")) SetWindowDisplayAffinity(g_logWnd, WDA_EXCLUDEFROMCAPTURE);
  ShowWindow(g_logWnd, SW_SHOW);
  ScrollLogToEnd();  // now that it has its size: open at the newest line
  SetForegroundWindow(g_logWnd);
}

// Routes printf/fprintf from the modes into a pipe that a reader thread forwards to the panel.
void CaptureOutput() {
  HANDLE rd, wr;
  if (!CreatePipe(&rd, &wr, nullptr, 0)) return;
  const int fd = _open_osfhandle(reinterpret_cast<intptr_t>(wr), _O_WRONLY | _O_TEXT);
  FILE* f;
  freopen_s(&f, "NUL", "w", stdout);
  freopen_s(&f, "NUL", "w", stderr);
  _dup2(fd, _fileno(stdout));
  _dup2(fd, _fileno(stderr));
  setvbuf(stdout, nullptr, _IONBF, 0);
  setvbuf(stderr, nullptr, _IONBF, 0);
  std::thread([rd] {
    char buf[4096];
    DWORD n;
    while (ReadFile(rd, buf, sizeof(buf), &n, nullptr) && n) {
      auto* s = new std::string(buf, n);
      if (!PostMessageW(L.hwnd, WM_APP_LOG, 0, reinterpret_cast<LPARAM>(s))) delete s;
    }
  }).detach();
}

void SetStatus(const std::wstring& text) {
  L.status = text;
  Invalidate();
}

void Relayout();

// The panel has no log box: the latest output line is shown under the status, and a few lines
// change the status itself.
void OnOutputLine(std::string line) {
  while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
  if (line.empty()) return;
  // Console instructions (hotkeys, Ctrl+C, the receiver's address list) don't belong in the panel.
  if (line.find("Ctrl+C") != std::string::npos || line.find("Hotkeys:") != std::string::npos ||
      line.rfind("    ", 0) == 0 || line.rfind("On the other device", 0) == 0 ||
      line.rfind("Show a screen here: waiting", 0) == 0) {
    return;
  }
  L.detail = Wide(line);
  if (L.mode == Mode::Showing) {
    const size_t q1 = line.find('\''), q2 = line.find("' (");
    if (q1 != std::string::npos && q2 != std::string::npos && line.find("connected:") != std::string::npos) {
      L.status = L"Showing " + Wide(line.substr(q1 + 1, q2 - q1 - 1)) + L"'s screen";
      Relayout();
    } else if (line.rfind("Playing via", 0) == 0) {
      // The sharing PC reports what it can offer (mic, camera, extend) right after this: refresh
      // for a few seconds so those buttons come alive without reopening the panel.
      SetTimer(L.hwnd, kRelayoutTimer, 500, nullptr);
    } else if (line.rfind("Session ended", 0) == 0) {
      L.status = L"Showing: pick a PC below, or share from it.";
      Relayout();
    }
  }
  Invalidate();
}

void OnOutput(const std::string& chunk) {
  L.pendingLine += chunk;
  size_t nl;
  while ((nl = L.pendingLine.find('\n')) != std::string::npos) {
    std::string line = L.pendingLine.substr(0, nl);
    while (!line.empty() && line.back() == '\r') line.pop_back();
    if (!line.empty()) AppendLog(line);
    OnOutputLine(L.pendingLine.substr(0, nl));
    L.pendingLine.erase(0, nl + 1);
  }
}

// Looks for other PCs continuously while not sharing: receivers to share to, and (for "Show here")
// PCs that can be asked to share. While idle, also announces this PC as one that can be asked.
void DiscoveryLoop() {
  const std::string name = ComputerName();
  while (!L.closing) {
    if (L.busy) {
      Sleep(300);
      continue;
    }
    if (L.advertise) SendBeacon(kPullPort, name, true);
    // PCs that can't hear broadcasts ask instead; they get the same beacon as an answer.
    std::string answer;
    if (L.answerShow) answer = BeaconText(kControlPort, name, false);
    else if (L.advertise) answer = BeaconText(kPullPort, name, true);
    std::vector<std::string> ask;
    {
      std::lock_guard<std::mutex> lock(L.knownMu);
      ask = L.knownIps;
    }
    auto* found = new std::vector<FoundReceiver>(Discover(1500, answer, ask));
    if (L.closing || !PostMessageW(L.hwnd, WM_APP_FOUND, 0, reinterpret_cast<LPARAM>(found))) delete found;
  }
}

// ---------------------------------------------------------------------------
// Drawing helpers

void RoundRect(gp::GraphicsPath& p, const gp::RectF& r, float radius) {
  radius = std::min(radius, std::min(r.Width, r.Height) / 2);
  const float d = radius * 2;
  p.AddArc(r.X, r.Y, d, d, 180, 90);
  p.AddArc(r.GetRight() - d, r.Y, d, d, 270, 90);
  p.AddArc(r.GetRight() - d, r.GetBottom() - d, d, d, 0, 90);
  p.AddArc(r.X, r.GetBottom() - d, d, d, 90, 90);
  p.CloseFigure();
}

void FillRound(gp::Graphics& g, const gp::RectF& r, float radius, const gp::Brush& fill) {
  gp::GraphicsPath p;
  RoundRect(p, r, radius);
  g.FillPath(&fill, &p);
}

// Fills exactly inside a StrokeRound outline (the same half-pixel inset). Filling the full rect
// left the fill's soft edge peeking out past the 1 px outline as a faint grey halo.
void FillInside(gp::Graphics& g, const gp::RectF& r, float radius, const gp::Brush& fill) {
  FillRound(g, gp::RectF(r.X + 0.5f, r.Y + 0.5f, r.Width - 1, r.Height - 1), radius, fill);
}

void StrokeRound(gp::Graphics& g, const gp::RectF& r, float radius, const gp::Color& c, float width = 1.0f) {
  gp::GraphicsPath p;
  RoundRect(p, gp::RectF(r.X + 0.5f, r.Y + 0.5f, r.Width - 1, r.Height - 1), radius);
  gp::Pen pen(c, width);
  g.DrawPath(&pen, &p);
}

// A horizontal cyan -> violet gradient (Nova's aurora colours) across r.
std::unique_ptr<gp::LinearGradientBrush> Aurora(const gp::RectF& r, int alpha) {
  return std::make_unique<gp::LinearGradientBrush>(gp::PointF(r.X - 1, 0), gp::PointF(r.GetRight() + 1, 0),
                                                   Alpha(kCyan, alpha), Alpha(kViolet, alpha));
}

void Write(gp::Graphics& g, const std::wstring& s, const gp::RectF& r, bool bold, float size, const gp::Color& c,
           gp::StringAlignment align = gp::StringAlignmentNear, bool wrap = false) {
  gp::Font font(bold ? L.semibold.get() : L.regular.get(), size, gp::FontStyleRegular, gp::UnitPixel);
  gp::StringFormat sf;
  sf.SetAlignment(align);
  sf.SetLineAlignment(gp::StringAlignmentCenter);
  sf.SetTrimming(wrap ? gp::StringTrimmingWord : gp::StringTrimmingEllipsisCharacter);
  if (!wrap) sf.SetFormatFlags(gp::StringFormatFlagsNoWrap);
  gp::SolidBrush brush(c);
  g.DrawString(s.c_str(), -1, &font, r, &sf, &brush);
}

float TextWidth(gp::Graphics& g, const std::wstring& s, float size) {
  if (s.empty()) return 0;
  gp::Font font(L.regular.get(), size, gp::FontStyleRegular, gp::UnitPixel);
  gp::RectF box;
  g.MeasureString(s.c_str(), -1, &font, gp::PointF(0, 0), gp::StringFormat::GenericTypographic(), &box);
  return box.Width;
}

std::unique_ptr<gp::FontFamily> Family(std::initializer_list<const wchar_t*> names) {
  for (const wchar_t* n : names) {
    auto f = std::make_unique<gp::FontFamily>(n);
    if (f->IsAvailable()) return f;
  }
  return std::unique_ptr<gp::FontFamily>(gp::FontFamily::GenericSansSerif()->Clone());
}

// Draws a 32-bit premultiplied image for UpdateLayeredWindow / BitBlt.
template <class Draw>
HBITMAP RenderDib(HDC dc, int w, int h, Draw draw) {
  BITMAPINFO bi{};
  bi.bmiHeader = {sizeof(BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB};
  void* bits = nullptr;
  HBITMAP dib = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
  if (!dib) return nullptr;
  gp::Bitmap bmp(w, h, w * 4, PixelFormat32bppPARGB, static_cast<BYTE*>(bits));
  gp::Graphics g(&bmp);
  g.SetSmoothingMode(gp::SmoothingModeAntiAlias);
  g.SetPixelOffsetMode(gp::PixelOffsetModeHighQuality);
  g.SetTextRenderingHint(gp::TextRenderingHintAntiAliasGridFit);  // grayscale: ClearType needs an opaque background
  g.Clear(gp::Color(0, 0, 0, 0));
  draw(g);
  return dib;
}

// ---------------------------------------------------------------------------
// Bubble: a layered window drawn with per-pixel alpha so its edge is smooth.

int BubblePx() { return MulDiv(kBubbleSize, static_cast<int>(GetDpiForWindow(L.bubble)), 96); }

// The "cast" glyph: a screen outline with radio waves in its lower-left corner.
void DrawCastIcon(gp::Graphics& g, float cx, float cy, float s) {
  const float w = s * 0.40f, h = s * 0.30f, x0 = cx - w / 2 + s * 0.02f, y0 = cy - h / 2, x1 = x0 + w, y1 = y0 + h;
  gp::Pen pen(White(245), s * 0.05f);
  pen.SetStartCap(gp::LineCapRound);
  pen.SetEndCap(gp::LineCapRound);
  pen.SetLineJoin(gp::LineJoinRound);
  const gp::PointF screen[] = {{x0, y0 + h * 0.38f}, {x0, y0}, {x1, y0}, {x1, y1}, {x0 + w * 0.56f, y1}};
  g.DrawLines(&pen, screen, 5);
  for (float r : {w * 0.28f, w * 0.50f}) g.DrawArc(&pen, x0 - r, y1 - r, r * 2, r * 2, 270, 90);
  gp::SolidBrush dot(White(245));
  const float d = w * 0.12f;
  g.FillPie(&dot, x0 - d, y1 - d, d * 2, d * 2, 270, 90);
}

void RenderBubble() {
  const int size = BubblePx();
  HDC screen = GetDC(nullptr);
  HBITMAP dib = RenderDib(screen, size, size, [size](gp::Graphics& g) {
    const float s = static_cast<float>(size);
    const gp::RectF disc(s * 0.07f, s * 0.05f, s * 0.86f, s * 0.86f);
    gp::SolidBrush shadow(gp::Color(70, 0, 0, 0));  // so it stands out on a white screen too
    g.FillEllipse(&shadow, disc.X - s * 0.02f, disc.Y + s * 0.03f, disc.Width + s * 0.04f, disc.Height + s * 0.03f);
    gp::SolidBrush body(gp::Color(232, 16, 18, 30));
    g.FillEllipse(&body, disc);
    // Glass sheen on the top half, like the launcher's pills.
    gp::LinearGradientBrush sheen(gp::PointF(0, disc.Y - 1), gp::PointF(0, disc.GetBottom() + 1), White(0), White(0));
    const gp::Color stops[] = {White(70), White(10), White(0)};
    const gp::REAL at[] = {0.0f, 0.55f, 1.0f};
    sheen.SetInterpolationColors(stops, at, 3);
    g.FillEllipse(&sheen, disc);
    // Ring: Nova's cyan -> violet when idle, mint while sharing / showing.
    const float inset = s * 0.075f;
    const gp::RectF rr(disc.X + inset, disc.Y + inset, disc.Width - 2 * inset, disc.Height - 2 * inset);
    std::unique_ptr<gp::Brush> ringBrush;
    if (L.mode == Mode::Idle) {
      ringBrush = std::make_unique<gp::LinearGradientBrush>(gp::PointF(rr.X - 1, rr.Y - 1),
                                                            gp::PointF(rr.GetRight() + 1, rr.GetBottom() + 1), kCyan, kViolet);
    } else {
      ringBrush = std::make_unique<gp::SolidBrush>(kMint);
    }
    gp::Pen ringPen(ringBrush.get(), s * 0.05f);
    g.DrawEllipse(&ringPen, rr);
    gp::Pen edge(White(64), 1.0f);
    g.DrawEllipse(&edge, disc);
    DrawCastIcon(g, disc.X + disc.Width / 2, disc.Y + disc.Height / 2, s);
  });
  if (dib) {
    HDC mem = CreateCompatibleDC(screen);
    HGDIOBJ old = SelectObject(mem, dib);
    RECT r;
    GetWindowRect(L.bubble, &r);
    POINT pos{r.left, r.top}, zero{0, 0};
    SIZE sz{size, size};
    BLENDFUNCTION blend{AC_SRC_OVER, 0, L.alpha, AC_SRC_ALPHA};
    UpdateLayeredWindow(L.bubble, screen, &pos, &sz, mem, &zero, 0, &blend, ULW_ALPHA);
    SelectObject(mem, old);
    DeleteDC(mem);
    DeleteObject(dib);
  }
  ReleaseDC(nullptr, screen);
}

RECT WorkAreaAt(POINT pt) {
  MONITORINFO mi{sizeof(mi)};
  GetMonitorInfoW(MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST), &mi);
  return mi.rcWork;
}

// Like AssistiveTouch: after a drag the bubble settles against the nearer side edge.
void SnapBubble() {
  RECT r;
  GetWindowRect(L.bubble, &r);
  const int size = r.right - r.left;
  const POINT center{(r.left + r.right) / 2, (r.top + r.bottom) / 2};
  const RECT work = WorkAreaAt(center);
  const int margin = MulDiv(6, static_cast<int>(GetDpiForWindow(L.bubble)), 96);
  const int x = center.x < (work.left + work.right) / 2 ? work.left + margin : work.right - size - margin;
  const int y = std::clamp(static_cast<int>(r.top), static_cast<int>(work.top) + margin,
                           static_cast<int>(work.bottom) - size - margin);
  SetWindowPos(L.bubble, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE);
}

void SetBubbleAlpha(BYTE alpha) {
  if (L.alpha == alpha) return;
  L.alpha = alpha;
  RenderBubble();
}

// ---------------------------------------------------------------------------
// Panel

std::wstring MonitorText() {
  if (L.monitors.empty()) return L"Main screen";
  const MonitorInfo& m = L.monitors[L.monitor];
  wchar_t row[128];
  swprintf_s(row, L"Screen %d  ·  %ld × %ld%s", L.monitor + 1, m.rect.right - m.rect.left,
             m.rect.bottom - m.rect.top, m.primary ? L"  ·  main" : L"");
  return row;
}

// Builds the widgets for the current mode and returns the panel height (96-DPI units).
int Layout() {
  const bool idle = L.mode == Mode::Idle, showing = L.mode == Mode::Showing;
  const float pad = 18, w = kPanelWidth - 2 * pad, half = (w - 10) / 2;
  float y = 18;
  L.widgets.clear();
  L.labels.clear();
  auto label = [&](Text style, float h, std::wstring text = L"") {
    L.labels.push_back({style, gp::RectF(pad, y, w, h), std::move(text)});
    y += h;
  };
  auto add = [&](int id, Kind kind, float x, float width, float h, std::wstring text, bool enabled = true) {
    L.widgets.push_back({id, kind, gp::RectF(x, y, width, h), std::move(text), enabled});
  };

  label(Text::Title, 24, L"Triton Screen Share");
  y += 4;
  if (L.pairAsk) {  // pairing with a new PC: the code and the answer buttons
    label(Text::Status, 20);
    y += 10;
    label(Text::Title, 34, L"Code   " + Wide(L.pairAsk->code));
    y += 6;
    label(Text::Warning, 54, L"Check that " + Wide(L.pairAsk->name) +
                                 L" shows the same code, then pair. Only pair your own PCs: paired PCs can share "
                                 L"with each other without asking.");
    y += 14;
    add(ID_PAIR_OK, Kind::Primary, pad, w, 42, L"Codes match - pair");
    y += 42 + 10;
    add(ID_PAIR_NO, Kind::Danger, pad, w, 42, L"Cancel");
    y += 42 + 18;
    return static_cast<int>(std::ceil(y));
  }
  if (L.pending) {  // another PC asked to show this screen: only the answer buttons
    label(Text::Status, 20);
    y += 10;
    label(Text::Warning, 36, L"Only allow PCs you know. Your screen (and sound) will be shown on " + Wide(L.pending->name) + L".");
    y += 14;
    add(ID_ALLOW, Kind::Primary, pad, half, 42, L"Allow");
    add(ID_ALWAYS, Kind::Button, pad + half + 10, half, 42, L"Always allow");
    y += 42 + 10;
    add(ID_DENY, Kind::Danger, pad, w, 42, L"Don't allow");
    y += 42 + 18;
    return static_cast<int>(std::ceil(y));
  }
  label(Text::Status, 20);
  if (!idle || L.showDetail) {
    y += 2;
    label(Text::Detail, 18);
  }
  y += 16;

  if (idle && L.firewallNeedsFix) {
    label(Text::Warning, 36, L"Windows Firewall would block the other PC (Public or Private Wi-Fi).");
    y += 6;
    add(ID_FW_FIX, Kind::Button, pad, w, 36, L"Allow through firewall (needs admin)");
    y += 36 + 18;
  }

  if (idle) {
    label(Text::Section, 16, L.found.empty() ? L"LOOKING FOR SCREENS" : L"SHARE TO");
    y += 8;
    if (L.found.empty()) {
      label(Text::Empty, 40, L"Waiting for a PC that clicked “Show here”");
      y += 8;
    }
    for (size_t i = 0; i < std::min(L.found.size(), kMaxRows); ++i) {
      add(ID_ROW + static_cast<int>(i), Kind::Row, pad, w, 40, Wide(L.found[i].name));
      y += 46;
    }
    add(ID_IP, Kind::Field, pad, w, 38, L"or type an IP address");
    y += 38 + 10;
    if (L.monitors.size() > 1) {
      add(ID_MONITOR, Kind::Cycle, pad, w, 38, MonitorText());
      y += 38 + 10;
    }
    add(ID_AUDIO, Kind::Toggle, pad, w, 30, L"Send sound too");
    y += 30 + 14;
    add(ID_SHARE, Kind::Primary, pad, half, 42, L"Share screen");
    add(ID_EXTEND, Kind::Button, pad + half + 10, half, 42, L"Extend screen");
    y += 42 + 18;
    label(Text::Rule, 1);
    y += 18;
    label(Text::Section, 16, L"USE THIS PC AS THE SCREEN");
    y += 8;
    add(ID_SHOW, Kind::Primary, pad, w, 42, L"Show here");
    y += 42 + 14;
    static const wchar_t* kRoles[] = {L"Start as: ask each time", L"Start as: share automatically",
                                      L"Start as: always show here"};
    add(ID_ROLE, Kind::Cycle, pad, w, 38, kRoles[static_cast<int>(L.role)]);
    y += 38 + 10;
    add(ID_AUTOSTART, Kind::Toggle, pad, w, 30, L"Start with Windows");
    y += 30;
  }

  if (showing && !ReceiverInSession()) {  // waiting: pick a PC to show, or let one share to us
    label(Text::Section, 16, L.senders.empty() ? L"PCS THAT CAN SHARE" : L"SHOW A SCREEN FROM");
    y += 8;
    if (L.senders.empty()) {
      label(Text::Empty, 40, L"Open Triton Screen Share on the other PC");
      y += 8;
    }
    for (size_t i = 0; i < std::min(L.senders.size(), kMaxRows); ++i) {
      add(ID_SENDER_ROW + static_cast<int>(i), Kind::Row, pad, w, 40, Wide(L.senders[i].name));
      y += 46;
    }
    y += 6;
  }

  if (showing && !ReceiverInSession()) {  // nothing on screen yet: hide/mute/extend have nothing to act on
    add(ID_STOP, Kind::Danger, pad, w, 42, L"Stop showing");
    y += 42;
  } else if (showing) {
    // The picture is always full screen; the bubble hides and shows it.
    add(ID_HIDE, Kind::Button, pad, half, 42, L"Hide / show screen");
    add(ID_MUTE, Kind::Button, pad + half + 10, half, 42, ReceiverMuted() ? L"Unmute sound" : L"Mute sound");
    y += 42 + 10;
    bool canExtend = false;
    const bool extended = ReceiverExtended(&canExtend);
    add(ID_MODE, Kind::Button, pad, w, 42,
        !canExtend ? L"Extend: the sharing PC has no driver" : extended ? L"Switch to mirror" : L"Switch to extend",
        canExtend);
    y += 42 + 18;
    label(Text::Section, 16, L"SHARE WITH THE OTHER PC");
    y += 8;
    // A dropdown shows which mic / camera is shared and opens the list; the button is off when there is none.
    auto pickName = [](const std::vector<MediaDevice>& list, const std::string& pick) {
      for (const MediaDevice& d : list) {
        if (d.id == pick) return Wide(d.name);
      }
      return list.empty() ? std::wstring() : Wide(list.front().name);
    };
    bool micAvailable = false;
    const bool micOn = ReceiverMicOn(&micAvailable);
    const bool noMic = L.devicesKnown && L.mics.empty();
    if (!L.mics.empty()) {
      add(ID_MIC_PICK, Kind::Dropdown, pad, w, 38, L"Mic:  " + pickName(L.mics, L.micPick));
      y += 38 + 8;
    }
    add(ID_MIC, micOn ? Kind::Primary : Kind::Button, pad, w, 42,
        micOn          ? L"Stop sharing my mic"
        : noMic        ? L"No microphone found on this PC"
        : !micAvailable ? L"Mic: the sharing PC needs VB-Cable"
                        : L"Share my mic",
        micOn || (micAvailable && !noMic));
    y += 42 + 12;
    bool camAvailable = false;
    const bool camOn = ReceiverCameraOn(&camAvailable);
    const bool noCamera = L.devicesKnown && L.cameras.empty();
    if (!L.cameras.empty()) {
      add(ID_CAMERA_PICK, Kind::Dropdown, pad, w, 38, L"Camera:  " + pickName(L.cameras, L.cameraPick));
      y += 38 + 8;
    }
    add(ID_CAMERA, camOn ? Kind::Primary : Kind::Button, pad, w, 42,
        camOn          ? L"Stop sharing my camera"
        : noCamera     ? L"No camera found on this PC"
        : !camAvailable ? L"Camera: run Install-Everything there"
                        : L"Share my camera",
        camOn || (camAvailable && !noCamera));
    y += 42 + 18;
    add(ID_STOP, Kind::Danger, pad, w, 42, L"Stop showing");
    y += 42;
  } else if (!idle) {
    add(ID_STOP, Kind::Danger, pad, w, 42, L"Stop sharing");
    y += 42;
  }

  y += 10;
  add(ID_LOGS, Kind::Link, pad, 90, 26, L"Show logs");
  add(ID_PAIR_NEW, Kind::Link, pad + w / 2 - 60, 120, 26, PairingOpen() ? L"Pairing open" : L"Pair a new PC");
  add(ID_QUIT, Kind::Link, pad + w - 90, 90, 26, L"Quit app");
  y += 26 + 10;

  auto exists = [](int id) {
    return std::any_of(L.widgets.begin(), L.widgets.end(), [id](const Widget& wd) { return wd.id == id; });
  };
  if (!exists(L.focus)) L.focus = ID_NONE;
  if (!exists(L.hover)) L.hover = ID_NONE;
  return static_cast<int>(std::ceil(y));
}

void PaintLabel(gp::Graphics& g, const Label& lb) {
  const gp::RectF& r = lb.r;
  switch (lb.style) {
    case Text::Title:
      Write(g, lb.text, r, true, 17, White(250));
      break;
    case Text::Status: {
      gp::SolidBrush dot(L.mode == Mode::Idle ? White(110) : kMint);
      g.FillEllipse(&dot, r.X, r.Y + r.Height / 2 - 3.5f, 7.0f, 7.0f);
      Write(g, L.status, gp::RectF(r.X + 14, r.Y, r.Width - 14, r.Height), true, 13.5f, White(235));
      break;
    }
    case Text::Detail:
      Write(g, L.detail, r, false, 12, White(140));
      break;
    case Text::Section:
      Write(g, lb.text, r, true, 11, White(125));
      break;
    case Text::Warning:
      Write(g, lb.text, r, false, 12.5f, kAmber, gp::StringAlignmentNear, true);
      break;
    case Text::Empty: {
      gp::GraphicsPath p;
      RoundRect(p, gp::RectF(r.X + 0.5f, r.Y + 0.5f, r.Width - 1, r.Height - 1), 12);
      gp::Pen dash(White(55), 1.0f);
      dash.SetDashStyle(gp::DashStyleDash);
      g.DrawPath(&dash, &p);
      Write(g, lb.text, r, false, 12.5f, White(135), gp::StringAlignmentCenter);
      break;
    }
    case Text::Rule: {
      gp::Pen line(White(28), 1.0f);
      g.DrawLine(&line, r.X, r.Y + 0.5f, r.GetRight(), r.Y + 0.5f);
      break;
    }
  }
}

void PaintWidget(gp::Graphics& g, const Widget& wd) {
  const gp::RectF& r = wd.r;
  const bool hot = wd.enabled && L.hover == wd.id, down = hot && L.press == wd.id;
  const float pill = r.Height / 2;
  // Button text stops short of the rounded ends (a long label ends in "…" instead of touching them).
  const gp::RectF label(r.X + pill * 0.6f, r.Y, r.Width - pill * 1.2f, r.Height);
  switch (wd.kind) {
    case Kind::Primary: {
      gp::GraphicsPath shape;
      RoundRect(shape, r, pill);
      g.FillPath(Aurora(r, wd.enabled ? 255 : 90).get(), &shape);
      if (hot || down) {
        gp::SolidBrush overlay(down ? gp::Color(50, 0, 0, 0) : White(40));
        g.FillPath(&overlay, &shape);
      }
      // The sheen over the top half is clipped to the pill. (Drawn as its own half-height rounded
      // rect, its corners were tighter than the pill's and stuck out as grey squares.)
      gp::Region saved;
      g.GetClip(&saved);
      g.SetClip(&shape, gp::CombineModeIntersect);
      gp::LinearGradientBrush sheen(gp::PointF(0, r.Y - 1), gp::PointF(0, r.Y + r.Height / 2 + 1), White(60), White(0));
      g.FillRectangle(&sheen, r.X, r.Y, r.Width, r.Height / 2);
      g.SetClip(&saved);
      Write(g, wd.text, label, true, 13.5f, White(wd.enabled ? 255 : 140), gp::StringAlignmentCenter);
      break;
    }
    case Kind::Button:
      FillInside(g, r, pill, gp::SolidBrush(White(!wd.enabled ? 12 : down ? 16 : hot ? 46 : 26)));
      StrokeRound(g, r, pill, White(wd.enabled ? 48 : 24));
      Write(g, wd.text, label, true, 13, White(wd.enabled ? 240 : 110), gp::StringAlignmentCenter);
      break;
    case Kind::Danger:
      FillRound(g, r, pill, gp::SolidBrush(Alpha(kDanger, down ? 180 : hot ? 255 : 215)));
      Write(g, wd.text, label, true, 13.5f, White(255), gp::StringAlignmentCenter);
      break;
    case Kind::Link:
      Write(g, wd.text, r, false, 12.5f, White(hot ? 235 : 145),
            wd.id == ID_LOGS ? gp::StringAlignmentNear
                             : wd.id == ID_PAIR_NEW ? gp::StringAlignmentCenter : gp::StringAlignmentFar);
      break;
    case Kind::Row: {
      const bool senderRow = wd.id >= ID_SENDER_ROW;
      const auto& list = senderRow ? L.senders : L.found;
      const size_t i = static_cast<size_t>(wd.id - (senderRow ? ID_SENDER_ROW : ID_ROW));
      const bool selected = !senderRow && static_cast<int>(i) == L.sel;
      FillInside(g, r, 12, gp::SolidBrush(White(selected ? 40 : hot ? 22 : 10)));
      StrokeRound(g, r, 12, selected ? Alpha(kAccent, 190) : White(26));
      // A small screen glyph, then the PC's name and address.
      const float ix = r.X + 14, iy = r.Y + r.Height / 2 - 8;
      gp::Pen glyph(selected ? kAccent : White(190), 1.6f);
      gp::GraphicsPath screen;
      RoundRect(screen, gp::RectF(ix, iy, 18, 12), 2.5f);
      g.DrawPath(&glyph, &screen);
      g.DrawLine(&glyph, ix + 5, iy + 15.5f, ix + 13, iy + 15.5f);
      Write(g, wd.text, gp::RectF(r.X + 44, r.Y, r.Width - 44 - 110, r.Height), true, 13.5f, White(245));
      if (i < list.size()) {
        Write(g, Wide(list[i].ip), gp::RectF(r.GetRight() - 120, r.Y, 106, r.Height), false, 12, White(130),
              gp::StringAlignmentFar);
      }
      break;
    }
    case Kind::Field: {
      const bool focused = L.focus == ID_IP;
      FillInside(g, r, 12, gp::SolidBrush(gp::Color(80, 0, 0, 0)));
      StrokeRound(g, r, 12, focused ? Alpha(kAccent, 210) : White(hot ? 64 : 36), focused ? 1.5f : 1.0f);
      const gp::RectF inner(r.X + 14, r.Y, r.Width - 28, r.Height);
      if (L.ip.empty()) Write(g, wd.text, inner, false, 13, White(115));
      else Write(g, L.ip, inner, false, 13.5f, White(245));
      if (focused && L.caretOn) {
        const float cx = inner.X + TextWidth(g, L.ip, 13.5f) + 1;
        gp::Pen caret(White(235), 1.5f);
        g.DrawLine(&caret, cx, r.Y + 10, cx, r.GetBottom() - 10);
      }
      break;
    }
    case Kind::Cycle: {
      FillInside(g, r, 12, gp::SolidBrush(gp::Color(80, 0, 0, 0)));
      StrokeRound(g, r, 12, White(hot ? 64 : 36));
      gp::Pen chevron(White(hot ? 230 : 150), 1.6f);
      chevron.SetStartCap(gp::LineCapRound);
      chevron.SetEndCap(gp::LineCapRound);
      const float cy = r.Y + r.Height / 2;
      const gp::PointF left[] = {{r.X + 20, cy - 5}, {r.X + 15, cy}, {r.X + 20, cy + 5}};
      const gp::PointF right[] = {{r.GetRight() - 20, cy - 5}, {r.GetRight() - 15, cy}, {r.GetRight() - 20, cy + 5}};
      g.DrawLines(&chevron, left, 3);
      g.DrawLines(&chevron, right, 3);
      // Long device names are cut short with "…" before they reach the chevrons.
      Write(g, wd.text, gp::RectF(r.X + 30, r.Y, r.Width - 60, r.Height), false, 13, White(230),
            gp::StringAlignmentCenter);
      break;
    }
    case Kind::Dropdown: {  // the chosen item, and a chevron that opens the list (ShowDevicePicker)
      FillInside(g, r, 12, gp::SolidBrush(gp::Color(80, 0, 0, 0)));
      StrokeRound(g, r, 12, White(hot ? 64 : 36));
      gp::Pen chevron(White(hot ? 230 : 150), 1.6f);
      chevron.SetStartCap(gp::LineCapRound);
      chevron.SetEndCap(gp::LineCapRound);
      const float cy = r.Y + r.Height / 2;
      const gp::PointF down[] = {{r.GetRight() - 24, cy - 3}, {r.GetRight() - 19, cy + 2}, {r.GetRight() - 14, cy - 3}};
      g.DrawLines(&chevron, down, 3);
      Write(g, wd.text, gp::RectF(r.X + 14, r.Y, r.Width - 46, r.Height), false, 13, White(230),
            gp::StringAlignmentNear);
      break;
    }
    case Kind::Toggle: {
      const bool on = wd.id == ID_AUTOSTART ? L.autostart : L.audio;
      Write(g, wd.text, gp::RectF(r.X + 2, r.Y, r.Width - 60, r.Height), false, 13.5f, White(225));
      const gp::RectF track(r.GetRight() - 42, r.Y + r.Height / 2 - 11, 42, 22);
      if (on) FillRound(g, track, 11, *Aurora(track, 255));
      else FillRound(g, track, 11, gp::SolidBrush(White(44)));
      if (hot) FillRound(g, track, 11, gp::SolidBrush(White(30)));
      gp::SolidBrush knob(White(255));
      g.FillEllipse(&knob, on ? track.GetRight() - 19 : track.X + 3, track.Y + 3, 16.0f, 16.0f);
      break;
    }
  }
  if (L.keyboard && L.focus == wd.id && wd.kind != Kind::Field) {
    const float radius = wd.kind == Kind::Row || wd.kind == Kind::Cycle || wd.kind == Kind::Dropdown ? 15 : pill + 3;
    StrokeRound(g, gp::RectF(r.X - 3, r.Y - 3, r.Width + 6, r.Height + 6), radius, White(210), 2.0f);
  }
}

void PaintPanel(HDC dc) {
  RECT rc;
  GetClientRect(L.hwnd, &rc);
  const int w = rc.right, h = rc.bottom;
  if (w <= 0 || h <= 0) return;
  HBITMAP dib = RenderDib(dc, w, h, [h](gp::Graphics& g) {
    const float scale = L.dpi / 96.0f, W = static_cast<float>(kPanelWidth), H = h / scale;
    if (!L.glass) g.Clear(gp::Color(255, 20, 22, 34));
    g.ScaleTransform(scale, scale);
    // Nova's glass: a blue-black tint over the blurred backdrop, a faint aurora glow in the corner
    // and a sheen that fades down from the top edge.
    gp::SolidBrush tint(gp::Color(L.glass ? 135 : 0, 12, 14, 26));
    g.FillRectangle(&tint, 0.0f, 0.0f, W, H);
    gp::GraphicsPath glow;
    glow.AddEllipse(-110.0f, -150.0f, 360.0f, 280.0f);
    gp::PathGradientBrush glowBrush(&glow);
    glowBrush.SetCenterColor(gp::Color(40, 96, 150, 255));
    gp::Color edge(0, 96, 150, 255);
    int n = 1;
    glowBrush.SetSurroundColors(&edge, &n);
    g.FillPath(&glowBrush, &glow);
    gp::LinearGradientBrush sheen(gp::PointF(0, -1), gp::PointF(0, H + 1), White(0), White(0));
    const gp::Color stops[] = {White(26), White(0), White(0)};
    const gp::REAL at[] = {0.0f, 0.3f, 1.0f};
    sheen.SetInterpolationColors(stops, at, 3);
    g.FillRectangle(&sheen, 0.0f, 0.0f, W, H);
    gp::Pen highlight(White(56), 1.0f);
    g.DrawLine(&highlight, 10.0f, 0.5f, W - 10, 0.5f);

    for (const Label& lb : L.labels) PaintLabel(g, lb);
    for (const Widget& wd : L.widgets) PaintWidget(g, wd);
  });
  if (!dib) return;
  HDC mem = CreateCompatibleDC(dc);
  HGDIOBJ old = SelectObject(mem, dib);
  BitBlt(dc, 0, 0, w, h, mem, 0, 0, SRCCOPY);  // keeps the alpha channel, so the backdrop shows through
  SelectObject(mem, old);
  DeleteDC(mem);
  DeleteObject(dib);
}

const Widget* WidgetAt(LPARAM lp) {
  const float x = GET_X_LPARAM(lp) * 96.0f / L.dpi, y = GET_Y_LPARAM(lp) * 96.0f / L.dpi;
  for (const Widget& wd : L.widgets) {
    if (wd.r.Contains(gp::PointF(x, y))) return &wd;
  }
  return nullptr;
}

const Widget* FindWidget(int id) {
  for (const Widget& wd : L.widgets) {
    if (wd.id == id) return &wd;
  }
  return nullptr;
}

// The mic / camera dropdown: lists this PC's devices under it, the one in use ticked. Picking one
// switches over at once if it is being shared.
void ShowDevicePicker(bool mic) {
  const auto& list = mic ? L.mics : L.cameras;
  std::string& pick = mic ? L.micPick : L.cameraPick;
  const Widget* wd = FindWidget(mic ? ID_MIC_PICK : ID_CAMERA_PICK);
  if (list.empty() || !wd) return;
  const std::string current = pick.empty() ? list.front().id : pick;
  HMENU menu = CreatePopupMenu();
  for (size_t i = 0; i < list.size(); ++i) {
    AppendMenuW(menu, MF_STRING | (list[i].id == current ? MF_CHECKED : MF_UNCHECKED), i + 1, Wide(list[i].name).c_str());
  }
  const float scale = L.dpi / 96.0f;
  POINT at{static_cast<LONG>(wd->r.X * scale), static_cast<LONG>((wd->r.Y + wd->r.Height + 4) * scale)};
  ClientToScreen(L.hwnd, &at);
  SetForegroundWindow(L.hwnd);
  const int chosen = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_LEFTALIGN | TPM_TOPALIGN | TPM_NONOTIFY, at.x, at.y, 0,
                                    L.hwnd, nullptr);
  DestroyMenu(menu);
  if (chosen <= 0) return;
  pick = list[chosen - 1].id;
  if (mic) ReceiverSetMicDevice(pick);
  else ReceiverSetCameraDevice(pick);
  Relayout();
}

void SetFocusId(int id) {
  L.focus = id;
  if (id == ID_IP) {
    L.caretOn = true;
    SetTimer(L.hwnd, kCaretTimer, 530, nullptr);
  } else {
    KillTimer(L.hwnd, kCaretTimer);
  }
  Invalidate();
}

// Sizes the panel for its mode and puts it beside the bubble, on the side with room.
void PlacePanel() {
  L.dpi = static_cast<int>(GetDpiForWindow(L.bubble));
  const int w = Px(kPanelWidth), h = Px(Layout());
  RECT b;
  GetWindowRect(L.bubble, &b);
  const RECT work = WorkAreaAt({(b.left + b.right) / 2, (b.top + b.bottom) / 2});
  const int gap = Px(10);
  int x = (b.left + b.right) / 2 > (work.left + work.right) / 2 ? b.left - w - gap : b.right + gap;
  int y = b.top;
  x = std::clamp(x, static_cast<int>(work.left), static_cast<int>(work.right) - w);
  y = std::clamp(y, static_cast<int>(work.top), static_cast<int>(work.bottom) - h);
  SetWindowPos(L.hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE);
  Invalidate();
}

// Content changed: resize if the panel is open (the list or mode decides its height).
void Relayout() {
  if (IsWindowVisible(L.hwnd)) PlacePanel();
  else Layout();
}

// Lists this PC's microphones and webcams in the background (it takes a moment) and re-lays out.
struct DeviceLists {
  std::vector<MediaDevice> mics, cameras;
};
void RefreshDevices() {
  if (L.listingDevices.exchange(true)) return;
  std::thread([] {
    auto* lists = new DeviceLists{ListCaptureDevices(false), ListCaptureDevices(true)};
    L.listingDevices = false;
    if (!PostMessageW(L.hwnd, WM_APP_DEVICES, 0, reinterpret_cast<LPARAM>(lists))) delete lists;
  }).detach();
}

void ShowPanel() {
  if (L.mode == Mode::Showing) RefreshDevices();
  PlacePanel();
  ShowWindow(L.hwnd, SW_SHOW);
  SetForegroundWindow(L.hwnd);
  SetBubbleAlpha(255);
}

void HidePanel() {
  ShowWindow(L.hwnd, SW_HIDE);
  L.press = ID_NONE;
  SetFocusId(ID_NONE);
  if (!L.hovered) SetTimer(L.bubble, kFadeTimer, 2500, nullptr);
}

void ShowFound(std::vector<FoundReceiver> found) {
  auto same = [](const FoundReceiver& a, const FoundReceiver& b) {
    return a.ip == b.ip && a.name == b.name && a.port == b.port;
  };
  if (std::equal(found.begin(), found.end(), L.found.begin(), L.found.end(), same)) return;
  const std::string selIp = L.sel >= 0 && L.sel < static_cast<int>(L.found.size()) ? L.found[L.sel].ip : "";
  L.found = std::move(found);
  L.sel = -1;
  for (size_t i = 0; i < std::min(L.found.size(), kMaxRows); ++i) {
    if (L.found[i].ip == selIp || (L.found.size() == 1 && L.ip.empty())) L.sel = static_cast<int>(i);
  }
  Relayout();
}

void SetMode(Mode mode) {
  L.mode = mode;
  L.busy = mode == Mode::Sharing;  // showing keeps discovering: it lists PCs to pick
  L.advertise = mode == Mode::Idle;
  L.answerShow = mode == Mode::Showing;
  RenderBubble();
  Relayout();
}

void StartMode(Mode mode, std::function<int()> run) {
  if (L.mode != Mode::Idle) return;
  if (L.worker.joinable()) L.worker.join();
  g_stop = false;
  L.detail.clear();
  L.showDetail = false;
  SetMode(mode);
  HidePanel();
  L.worker = std::thread([run] {
    const int rc = run();
    PostMessageW(L.hwnd, WM_APP_DONE, static_cast<WPARAM>(rc), 0);
  });
}

void StopMode() {
  g_stop = true;
  if (HWND h = g_notifyHwnd) PostMessageW(h, WM_CLOSE, 0, 0);
  SetStatus(L"Stopping...");
}

void SaveSettings();
void RememberIp(const std::string& ip);

// Share = mirror the chosen screen. Extend = a new virtual screen beside it, the receiver's size.
void OnShare(bool extend) {
  if (extend && !VddAvailable()) {
    MessageBoxW(L.hwnd,
                L"Extend needs the free \"Virtual Display Driver\" installed once on THIS PC (the one sharing).\n\n"
                L"1. Download the installer from\n"
                L"    github.com/VirtualDrivers/Virtual-Display-Driver/releases\n"
                L"2. Run it and choose Install (it asks for admin rights).\n"
                L"3. Come back and click \"Extend screen\" again.\n\n"
                L"Nothing is needed on the other PC.",
                L"Triton Screen Share", MB_ICONINFORMATION);
    return;
  }
  SenderOptions o;
  o.extend = extend;
  if (!L.ip.empty()) {
    o.to = Narrow(L.ip.c_str());
  } else if (L.sel >= 0 && L.sel < static_cast<int>(L.found.size())) {
    o.to = L.found[L.sel].ip;
    o.port = L.found[L.sel].port;
  } else {
    MessageBoxW(L.hwnd,
                L"Pick a PC from the list, or type its IP address.\n\nOn the other PC, open Triton Screen Share and "
                L"click \"Show here\" first.",
                L"Triton Screen Share", MB_ICONINFORMATION);
    return;
  }
  o.monitor = L.monitors.empty() ? -1 : L.monitor;
  o.audio = L.audio;
  RememberIp(o.to);
  const std::wstring who = !L.ip.empty() ? L.ip : Wide(L.found[L.sel].name);
  if (L.ip.empty() && L.found[L.sel].name != L.lastShared) {
    L.lastShared = L.found[L.sel].name;
    SaveSettings();
  }
  SetStatus((extend ? L"Extending this desktop onto " : L"Sharing this screen with ") + who);
  StartMode(Mode::Sharing, [o] { return RunSender(o); });
}

void OnShow() {
  SetStatus(L"Showing: pick a PC below, or share from it.");
  ReceiverOptions o;
  o.fullscreen = true;
  StartMode(Mode::Showing, [o] { return RunReceiver(o); });
}

// ---------------------------------------------------------------------------
// Settings (HKCU\Software\TritonScreenShare): "Start as" and the PCs allowed to show this screen.

constexpr wchar_t kSettingsKey[] = L"Software\\TritonScreenShare";

void LoadSettings() {
  DWORD role = 0, size = sizeof(role);
  if (RegGetValueW(HKEY_CURRENT_USER, kSettingsKey, L"StartAs", RRF_RT_REG_DWORD, nullptr, &role, &size) == ERROR_SUCCESS &&
      role <= 2) {
    L.role = static_cast<Role>(role);
  }
  wchar_t buf[4096];
  size = sizeof(buf);
  if (RegGetValueW(HKEY_CURRENT_USER, kSettingsKey, L"LastSharedTo", RRF_RT_REG_SZ, nullptr, buf, &size) == ERROR_SUCCESS) {
    L.lastShared = Narrow(buf);
  }
  size = sizeof(buf);
  if (RegGetValueW(HKEY_CURRENT_USER, kSettingsKey, L"KnownPCs", RRF_RT_REG_SZ, nullptr, buf, &size) == ERROR_SUCCESS) {
    std::string all = Narrow(buf), item;
    std::lock_guard<std::mutex> lock(L.knownMu);
    for (char c : all + "\n") {
      if (c == '\n') {
        if (!item.empty()) L.knownIps.push_back(item);
        item.clear();
      } else {
        item += c;
      }
    }
  }
  size = sizeof(buf);
  if (RegGetValueW(HKEY_CURRENT_USER, kSettingsKey, L"TrustedPCs", RRF_RT_REG_SZ, nullptr, buf, &size) == ERROR_SUCCESS) {
    std::string all = Narrow(buf), item;
    for (char c : all + "\n") {
      if (c == '\n') {
        if (!item.empty()) L.trusted.push_back(item);
        item.clear();
      } else {
        item += c;
      }
    }
  }
}

void SaveSettings() {
  const DWORD role = static_cast<DWORD>(L.role);
  RegSetKeyValueW(HKEY_CURRENT_USER, kSettingsKey, L"StartAs", REG_DWORD, &role, sizeof(role));
  std::wstring all;
  for (const std::string& t : L.trusted) all += Wide(t) + L"\n";
  RegSetKeyValueW(HKEY_CURRENT_USER, kSettingsKey, L"TrustedPCs", REG_SZ, all.c_str(),
                  static_cast<DWORD>((all.size() + 1) * sizeof(wchar_t)));
  const std::wstring last = Wide(L.lastShared);
  RegSetKeyValueW(HKEY_CURRENT_USER, kSettingsKey, L"LastSharedTo", REG_SZ, last.c_str(),
                  static_cast<DWORD>((last.size() + 1) * sizeof(wchar_t)));
  std::wstring known;
  {
    std::lock_guard<std::mutex> lock(L.knownMu);
    for (const std::string& ip : L.knownIps) known += Wide(ip) + L"\n";
  }
  RegSetKeyValueW(HKEY_CURRENT_USER, kSettingsKey, L"KnownPCs", REG_SZ, known.c_str(),
                  static_cast<DWORD>((known.size() + 1) * sizeof(wchar_t)));
}

// Remembers a PC this one shared with, so it is asked directly next time (see Discover).
void RememberIp(const std::string& ip) {
  {
    std::lock_guard<std::mutex> lock(L.knownMu);
    auto& v = L.knownIps;
    if (!v.empty() && v.front() == ip) return;
    v.erase(std::remove(v.begin(), v.end(), ip), v.end());
    v.insert(v.begin(), ip);
    if (v.size() > 8) v.resize(8);
  }
  SaveSettings();
}

// "Start with Windows": a per-user Run entry that opens just the bubble (--background).
constexpr wchar_t kRunKey[] = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
constexpr wchar_t kRunValue[] = L"TritonScreenShare";

bool AutostartOn() {
  return RegGetValueW(HKEY_CURRENT_USER, kRunKey, kRunValue, RRF_RT_REG_SZ, nullptr, nullptr, nullptr) == ERROR_SUCCESS;
}

void SetAutostart(bool on) {
  if (!on) {
    RegDeleteKeyValueW(HKEY_CURRENT_USER, kRunKey, kRunValue);
    return;
  }
  wchar_t exe[MAX_PATH];
  GetModuleFileNameW(nullptr, exe, MAX_PATH);
  const std::wstring cmd = L"\"" + std::wstring(exe) + L"\" --background";
  RegSetKeyValueW(HKEY_CURRENT_USER, kRunKey, kRunValue, REG_SZ, cmd.c_str(),
                  static_cast<DWORD>((cmd.size() + 1) * sizeof(wchar_t)));
}

bool Trusted(const std::string& name) { return std::find(L.trusted.begin(), L.trusted.end(), name) != L.trusted.end(); }

// "Start as: share automatically": share with a PC as soon as it clicks "Show here" (the one shared
// with last, if several do). While it can't be reached, tries again at most every 10 s.
void MaybeAutoShare() {
  if (L.role != Role::AlwaysShare || !L.autoShare || L.mode != Mode::Idle || L.pending || L.found.empty() ||
      !L.ip.empty() || GetTickCount64() < L.autoShareAfter) {
    return;
  }
  // Only paired PCs get the screen without a click (an unknown one could be anyone on the Wi-Fi).
  int pick = -1;
  for (size_t i = 0; i < std::min(L.found.size(), kMaxRows); ++i) {
    if (!IsPaired(L.found[i].fp)) continue;
    if (pick < 0 || L.found[i].name == L.lastShared) pick = static_cast<int>(i);
  }
  if (pick < 0) return;
  L.sel = pick;
  L.autoShareAfter = GetTickCount64() + 10'000;
  OnShare(false);
}

// Called on the handshake's thread: shows the pairing question in the panel and waits (up to 2
// minutes) for the answer.
bool AskToPair(const std::string& name, const std::string& code) {
  auto ask = std::make_shared<PairAsk>();
  ask->name = name;
  ask->code = code;
  std::future<bool> answer = ask->answer.get_future();
  auto* msg = new std::shared_ptr<PairAsk>(ask);
  if (!PostMessageW(L.hwnd, WM_APP_PAIR, 0, reinterpret_cast<LPARAM>(msg))) {
    delete msg;
    return false;
  }
  for (int i = 0; i < 600 && !L.closing && !g_stop; ++i) {
    if (answer.wait_for(std::chrono::milliseconds(200)) == std::future_status::ready) return answer.get();
  }
  PostMessageW(L.hwnd, WM_APP_PAIR, 0, 0);  // nobody answered: take the question down
  return false;
}

void AnswerPair(bool ok) {
  if (!L.pairAsk) return;
  L.pairAsk->answer.set_value(ok);
  L.pairAsk.reset();
  L.status = L.statusBeforePair;
  Relayout();
}

// ---------------------------------------------------------------------------
// Pull: the PC that shows the screen picks this one, and this one starts sharing to it.

// Accepts requests on kPullPort and hands them to the panel (which asks the user, or not).
void PullListenerLoop() {
  SOCKET ls = ListenTcp(kPullPort);
  if (ls == INVALID_SOCKET) {
    std::fprintf(stderr, "Can't accept \"show this screen\" requests (TCP %u in use?).\n", kPullPort);
    return;
  }
  while (!L.closing) {
    fd_set rd;
    FD_ZERO(&rd);
    FD_SET(ls, &rd);
    timeval tv{0, 500'000};
    if (select(0, &rd, nullptr, nullptr, &tv) <= 0) continue;
    SOCKET s = accept(ls, nullptr, nullptr);
    if (s == INVALID_SOCKET) continue;
    SetNoDelay(s);
    auto conn = std::make_shared<Connection>(s);
    uint8_t type = 0;
    std::vector<uint8_t> p;
    if (!conn->WaitReadable(3000) || !conn->Recv(type, p) || type != MSG_PULL || p.size() < sizeof(PullRequest)) continue;
    PullRequest req;
    std::memcpy(&req, p.data(), sizeof(req));
    req.name[sizeof(req.name) - 1] = '\0';
    if (req.version != kProtocolVersion) {
      conn->Send(MSG_PULL_REPLY, PullReply{0});
      continue;
    }
    auto* pull = new PendingPull{conn, conn->PeerIp(), req.name, req.controlPort};
    if (!PostMessageW(L.hwnd, WM_APP_PULL, 0, reinterpret_cast<LPARAM>(pull))) delete pull;
  }
  closesocket(ls);
}

void AnswerPull(uint8_t result) {
  if (!L.pending) return;
  KillTimer(L.hwnd, kPullTimeoutTimer);
  std::unique_ptr<PendingPull> pull = std::move(L.pending);
  pull->conn->Send(MSG_PULL_REPLY, PullReply{result});
  if (result != 1) {
    L.status = result == 2 ? kIdleStatus : L"Declined " + Wide(pull->name) + L"'s request.";
    Relayout();
    return;
  }
  SenderOptions o;
  o.to = pull->ip;
  o.port = pull->port ? pull->port : kControlPort;
  RememberIp(o.to);
  o.monitor = L.monitors.empty() ? -1 : L.monitor;
  o.audio = L.audio;
  L.status = L"Sharing this screen with " + Wide(pull->name) + L" (they asked)";
  if (pull->name != L.lastShared) {
    L.lastShared = pull->name;
    SaveSettings();
  }
  StartMode(Mode::Sharing, [o] { return RunSender(o); });
}

void OnPullRequest(std::unique_ptr<PendingPull> pull) {
  if (L.mode != Mode::Idle || L.pending) {
    pull->conn->Send(MSG_PULL_REPLY, PullReply{2});  // busy
    return;
  }
  L.pending = std::move(pull);
  // A paired PC showing screens (its beacon carries its key's fingerprint) gets it without a click, so
  // "show my laptop here" works by voice. A faked beacon gains nothing: the handshake that follows
  // checks the real device key, and an unknown one only gets the pairing question.
  const bool paired = std::any_of(L.found.begin(), L.found.end(), [](const FoundReceiver& r) {
    return r.ip == L.pending->ip && IsPaired(r.fp);
  });
  if (paired || Trusted(L.pending->name)) {
    AnswerPull(1);
    return;
  }
  L.status = Wide(L.pending->name) + L" wants to show this screen";
  SetTimer(L.hwnd, kPullTimeoutTimer, 30'000, nullptr);
  ShowPanel();
}

// From the "Show here" panel: ask a PC to share its screen to this one.
void RequestPull(size_t i) {
  if (i >= L.senders.size()) return;
  const FoundReceiver target = L.senders[i];
  L.status = L"Asking " + Wide(target.name) + L" to share... (it may need to click Allow)";
  Relayout();
  std::thread([target] {
    uint8_t result = 3;  // unreachable
    SOCKET s = ConnectTcp(target.ip, target.port ? target.port : kPullPort, 3000);
    if (s != INVALID_SOCKET) {
      SetNoDelay(s);
      Connection conn(s);
      PullRequest req{kProtocolVersion, kControlPort, {}};
      strncpy_s(req.name, ComputerName().c_str(), _TRUNCATE);
      uint8_t type = 0;
      std::vector<uint8_t> p;
      if (conn.Send(MSG_PULL, req) && conn.WaitReadable(35'000) && conn.Recv(type, p) && type == MSG_PULL_REPLY &&
          !p.empty()) {
        result = p[0];
      }
    }
    auto* name = new std::wstring(Wide(target.name));
    if (!PostMessageW(L.hwnd, WM_APP_PULL_RESULT, result, reinterpret_cast<LPARAM>(name))) delete name;
  }).detach();
}

// ---------------------------------------------------------------------------
// Control pipe (\\.\pipe\TritonScreenShare): the voice assistant on this PC sends one line, e.g.
// "show extend", and gets one JSON line back. Only this user's own programs can open it.
//   status                    what's going on (mode, picture, sound, mic, camera, PCs around)
//   show [mirror|extend]      show a paired PC's screen here (it shares by itself, or is asked to)
//   share [mirror|extend]     share this screen to a paired PC that is showing screens
//   view mirror|extend        switch the picture being shown here
//   stop                      stop showing / sharing
//   sound|mic|camera on|off   the shown PC's sound here; this PC's mic / webcam to the shown PC
//   hide | unhide             hide or bring back the picture

// Test hook: LFRD_TEST_INSTANCE=<x> gives a second copy its own lock, quit event and control pipe, so it
// can run beside the real one.
std::wstring Named(const wchar_t* name) {
  const char* suffix = std::getenv("LFRD_TEST_INSTANCE");
  return suffix ? std::wstring(name) + L"." + Wide(suffix) : std::wstring(name);
}

std::string JsonText(const std::string& s) {
  std::string out = "\"";
  for (char c : s) {
    if (c == '"' || c == '\\') out += '\\';
    if (static_cast<unsigned char>(c) >= 0x20) out += c;
  }
  return out + "\"";
}

std::string ControlStatus(const char* error = nullptr) {
  bool canExtend = false, micAvailable = false, camAvailable = false;
  const bool extended = ReceiverExtended(&canExtend);
  const bool micOn = ReceiverMicOn(&micAvailable), camOn = ReceiverCameraOn(&camAvailable);
  const bool showing = L.mode == Mode::Showing, session = showing && ReceiverInSession();
  std::string paired;
  for (const FoundReceiver& r : L.mode == Mode::Idle ? L.found : L.senders) {
    if (IsPaired(r.fp)) paired += (paired.empty() ? "" : ",") + JsonText(r.name);
  }
  std::string j = "{\"ok\":" + std::string(error ? "false" : "true");
  if (error) j += ",\"error\":" + JsonText(error);
  j += ",\"mode\":" + JsonText(L.mode == Mode::Idle ? "idle" : showing ? "showing" : "sharing");
  j += ",\"session\":" + std::string(session ? "true" : "false");
  j += ",\"extended\":" + std::string(session && extended ? "true" : "false");
  j += ",\"can_extend\":" + std::string(canExtend ? "true" : "false");
  j += ",\"muted\":" + std::string(ReceiverMuted() ? "true" : "false");
  j += ",\"hidden\":" + std::string(session && ReceiverHidden() ? "true" : "false");
  j += ",\"mic\":" + std::string(micOn ? "true" : "false");
  j += ",\"mic_available\":" + std::string(micAvailable ? "true" : "false");
  j += ",\"camera\":" + std::string(camOn ? "true" : "false");
  j += ",\"camera_available\":" + std::string(camAvailable ? "true" : "false");
  j += ",\"waiting\":" + std::string(L.wantUntil ? "true" : "false");
  j += ",\"paired_nearby\":[" + paired + "]";
  return j + ",\"status\":" + JsonText(Narrow(L.status.c_str())) + "}";
}

// The paired PC to use from `list` (the one used last, if several), or -1.
int PickPaired(const std::vector<FoundReceiver>& list) {
  int pick = -1;
  for (size_t i = 0; i < std::min(list.size(), kMaxRows); ++i) {
    if (!IsPaired(list[i].fp)) continue;
    if (pick < 0 || list[i].name == L.lastShared) pick = static_cast<int>(i);
  }
  return pick;
}

// A spoken request in progress: runs every 500 ms until the picture is here in the wanted view.
void VoiceTick() {
  const ULONGLONG now = GetTickCount64();
  auto done = [] {
    KillTimer(L.hwnd, kVoiceTimer);
    L.wantView = -1;
    L.wantUntil = L.pullAt = L.sessionSince = 0;
  };
  if (L.mode != Mode::Showing || now > L.wantUntil) return done();
  if (!ReceiverInSession()) {
    L.sessionSince = 0;
    // The other PC shares by itself when it is set to "share automatically"; otherwise ask it.
    if (L.pullAt && now >= L.pullAt) {
      const int i = PickPaired(L.senders);
      if (i >= 0) {
        RequestPull(static_cast<size_t>(i));
        L.pullAt = now + 15'000;  // not on screen by then: ask again
      }
    }
    return;
  }
  if (!L.sessionSince) L.sessionSince = now;
  if (L.wantView < 0) return done();
  bool canExtend = false;
  const bool extended = ReceiverExtended(&canExtend);
  if (extended == (L.wantView == 1)) return done();
  if (canExtend) {
    ReceiverSwitchMode();
    SetTimer(L.hwnd, kRelayoutTimer, 500, nullptr);
    return done();
  }
  if (now - L.sessionSince > 8'000) {  // the sharing PC never said it can extend: no driver there
    L.status = L"Showing - extend needs the driver on the sharing PC.";
    Relayout();
    done();
  }
}

std::string OnControl(const std::string& line) {
  std::vector<std::string> w;
  {
    std::string word;
    for (char c : line + " ") {
      if (c == ' ' || c == '\t' || c == '\r' || c == '\n') {
        if (!word.empty()) w.push_back(word);
        word.clear();
      } else {
        word += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
      }
    }
  }
  if (w.empty() || w[0] == "status") return ControlStatus();
  const std::string& cmd = w[0];
  const std::string arg = w.size() > 1 ? w[1] : "";
  const bool showing = L.mode == Mode::Showing, session = showing && ReceiverInSession();
  if (L.pairAsk || L.pending) return ControlStatus("waiting_for_answer");  // a question is on screen

  if (cmd == "show" || cmd == "view") {
    const int view = arg == "extend" ? 1 : arg == "mirror" ? 0 : -1;
    if (cmd == "view" && view < 0) return ControlStatus("bad_command");
    if (L.mode == Mode::Sharing) return ControlStatus("sharing_here");
    if (cmd == "view" && !showing) return ControlStatus("not_showing");
    if (L.mode == Mode::Idle) {
      L.autoShare = false;
      OnShow();
      L.pullAt = GetTickCount64() + 4'000;  // a PC set to "share automatically" is usually here by then
    } else if (!session && !L.pullAt) {
      L.pullAt = GetTickCount64();
    }
    L.wantView = view;
    L.wantUntil = GetTickCount64() + 60'000;
    SetTimer(L.hwnd, kVoiceTimer, 500, nullptr);
    VoiceTick();
    return ControlStatus();
  }
  if (cmd == "share") {
    if (L.mode == Mode::Sharing) return ControlStatus();
    if (showing) return ControlStatus("showing_here");
    const int i = PickPaired(L.found);
    if (i < 0) return ControlStatus("no_screen");
    L.sel = i;
    L.ip.clear();
    OnShare(arg == "extend" && VddAvailable());
    return ControlStatus(arg == "extend" && !VddAvailable() ? "no_extend_driver" : nullptr);
  }
  if (cmd == "stop") {
    L.wantView = -1;
    L.wantUntil = 0;
    if (L.mode == Mode::Idle) return ControlStatus("not_running");
    L.autoShare = false;
    StopMode();
    return ControlStatus();
  }
  if (cmd == "sound" && (arg == "on" || arg == "off")) {
    if (L.mode == Mode::Idle) {  // the next share sends sound or not
      L.audio = arg == "on";
      Invalidate();
      return ControlStatus();
    }
    if (ReceiverMuted() == (arg == "on")) ReceiverCommand('M');
    Relayout();
    return ControlStatus(showing ? nullptr : "not_showing");
  }
  if ((cmd == "mic" || cmd == "camera") && (arg == "on" || arg == "off")) {
    if (!session) return ControlStatus("not_showing");
    const bool mic = cmd == "mic";
    bool available = false;
    const bool on = mic ? ReceiverMicOn(&available) : ReceiverCameraOn(&available);
    if (on == (arg == "on")) return ControlStatus();
    if (arg == "on") {
      const std::vector<MediaDevice>& list = mic ? L.mics : L.cameras;
      if (!available) return ControlStatus(mic ? "mic_unavailable" : "camera_unavailable");
      if (L.devicesKnown && list.empty()) return ControlStatus(mic ? "no_mic" : "no_camera");
      const std::string& pick = mic ? L.micPick : L.cameraPick;
      if (!list.empty()) {
        if (mic) ReceiverSetMicDevice(pick.empty() ? list.front().id : pick);
        else ReceiverSetCameraDevice(pick.empty() ? list.front().id : pick);
      }
    }
    if (mic) ReceiverToggleMic();
    else ReceiverToggleCamera();
    SetTimer(L.hwnd, kRelayoutTimer, 500, nullptr);
    return ControlStatus();
  }
  if (cmd == "hide" || cmd == "unhide") {
    if (!session) return ControlStatus("not_showing");
    if (ReceiverHidden() != (cmd == "hide")) ReceiverCommand('H');
    SetTimer(L.hwnd, kRelayoutTimer, 150, nullptr);
    return ControlStatus();
  }
  return ControlStatus("bad_command");
}

// Only this user (and SYSTEM) may connect, and only from this PC.
void ControlPipeLoop() {
  std::wstring sddl = L"D:P(A;;GA;;;SY)";
  HANDLE token = nullptr;
  if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
    BYTE buf[256];
    DWORD size = 0;
    LPWSTR sid = nullptr;
    if (GetTokenInformation(token, TokenUser, buf, sizeof(buf), &size) &&
        ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(buf)->User.Sid, &sid)) {
      sddl += L"(A;;GA;;;" + std::wstring(sid) + L")";
      LocalFree(sid);
    }
    CloseHandle(token);
  }
  SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, FALSE};
  if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &sa.lpSecurityDescriptor,
                                                            nullptr)) {
    return;
  }
  while (!L.closing) {
    HANDLE pipe = CreateNamedPipeW(Named(kControlPipe).c_str(), PIPE_ACCESS_DUPLEX,
                                   PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS, 1, 4096,
                                   4096, 0, &sa);
    if (pipe == INVALID_HANDLE_VALUE) return;
    if (ConnectNamedPipe(pipe, nullptr) || GetLastError() == ERROR_PIPE_CONNECTED) {
      std::string line;
      char buf[256];
      DWORD got = 0;
      while (line.size() < 512 && line.find('\n') == std::string::npos && ReadFile(pipe, buf, sizeof(buf), &got, nullptr) &&
             got > 0) {
        line.append(buf, got);
      }
      line = line.substr(0, line.find('\n'));
      std::string reply = "{\"ok\":false,\"error\":\"busy\"}";
      auto* req = new ControlRequest{line, {}};
      std::future<std::string> answer = req->reply.get_future();
      if (!PostMessageW(L.hwnd, WM_APP_CONTROL, 0, reinterpret_cast<LPARAM>(req))) {
        delete req;
      } else if (answer.wait_for(std::chrono::seconds(5)) == std::future_status::ready) {
        reply = answer.get();
      }
      reply += "\n";
      DWORD put = 0;
      WriteFile(pipe, reply.data(), static_cast<DWORD>(reply.size()), &put, nullptr);
      FlushFileBuffers(pipe);
      DisconnectNamedPipe(pipe);
    }
    CloseHandle(pipe);
  }
  LocalFree(sa.lpSecurityDescriptor);
}

void UpdateFirewall() {
  L.firewallNeedsFix = FirewallNeedsFix();
  if (L.mode == Mode::Idle) Relayout();
}

void Quit() {
  if (L.closing) return;
  L.closing = true;
  if (L.quitEvent) SetEvent(L.quitEvent);  // lets the quit-event thread end
  if (L.mode != Mode::Idle) StopMode();
  ShowWindow(L.hwnd, SW_HIDE);
  ShowWindow(L.bubble, SW_HIDE);
  if (L.worker.joinable()) L.worker.join();
  if (L.discovery.joinable()) L.discovery.join();
  if (L.pullListener.joinable()) L.pullListener.join();
  DestroyWindow(L.bubble);
  DestroyWindow(L.hwnd);
}

void Activate(int id) {
  if (id >= ID_SENDER_ROW) {
    RequestPull(static_cast<size_t>(id - ID_SENDER_ROW));
    return;
  }
  if (id >= ID_ROW) {
    L.sel = id - ID_ROW;
    L.ip.clear();
    SetFocusId(id);
    return;
  }
  switch (id) {
    case ID_SHARE: OnShare(false); break;
    case ID_EXTEND: OnShare(true); break;
    case ID_SHOW:  // showing here instead: stop sharing automatically until the next start
      L.autoShare = false;
      OnShow();
      break;
    case ID_STOP:  // stopped by hand: don't share again by itself until the next start
      L.autoShare = false;
      StopMode();
      break;
    case ID_HIDE:  // the panel stays open; bring it back above the picture once that is shown
      ReceiverCommand('H');
      SetTimer(L.hwnd, kRelayoutTimer, 150, nullptr);
      break;
    case ID_MODE:  // the sender rebuilds its stream; the picture is back in a second or two
      ReceiverSwitchMode();
      SetTimer(L.hwnd, kRelayoutTimer, 500, nullptr);
      break;
    case ID_MIC_PICK:
    case ID_CAMERA_PICK:
      ShowDevicePicker(id == ID_MIC_PICK);
      break;
    case ID_MUTE:  // the panel stays open, so the button shows the new state
      ReceiverCommand('M');
      Relayout();
      break;
    case ID_QUIT: Quit(); break;
    case ID_LOGS:
      HidePanel();
      ShowLogs();
      break;
    case ID_PAIR_NEW:  // unknown PCs are refused unless this is open (secure.h)
      OpenPairing(120);
      SetStatus(L"Pairing open for 2 minutes: connect from the other PC (click it there too).");
      std::printf("Pairing opened for 2 minutes.\n");
      Relayout();
      break;
    case ID_CAMERA:  // like the mic: the receiver's thread opens the webcam
      if (!L.cameras.empty()) ReceiverSetCameraDevice(L.cameraPick.empty() ? L.cameras.front().id : L.cameraPick);
      ReceiverToggleCamera();
      SetTimer(L.hwnd, kRelayoutTimer, 500, nullptr);
      break;
    case ID_MIC:  // the receiver's thread opens the mic; re-layout once it has
      if (!L.mics.empty()) ReceiverSetMicDevice(L.micPick.empty() ? L.mics.front().id : L.micPick);
      ReceiverToggleMic();
      SetTimer(L.hwnd, kRelayoutTimer, 500, nullptr);
      break;
    case ID_ALLOW: AnswerPull(1); break;
    case ID_ALWAYS:
      if (L.pending && !Trusted(L.pending->name)) {
        L.trusted.push_back(L.pending->name);
        SaveSettings();
      }
      AnswerPull(1);
      break;
    case ID_DENY: AnswerPull(0); break;
    case ID_PAIR_OK: AnswerPair(true); break;
    case ID_PAIR_NO: AnswerPair(false); break;
    case ID_ROLE:
      L.role = static_cast<Role>((static_cast<int>(L.role) + 1) % 3);
      L.autoShare = L.role == Role::AlwaysShare;
      L.autoShareAfter = 0;
      SaveSettings();
      Relayout();
      break;
    case ID_IP: SetFocusId(ID_IP); break;
    case ID_MONITOR:
      if (!L.monitors.empty()) L.monitor = (L.monitor + 1) % static_cast<int>(L.monitors.size());
      Relayout();
      break;
    case ID_AUDIO:
      L.audio = !L.audio;
      Invalidate();
      break;
    case ID_AUTOSTART:
      L.autostart = !L.autostart;
      SetAutostart(L.autostart);
      Invalidate();
      break;
    case ID_FW_FIX:
      if (!FixFirewall(L.hwnd)) {
        MessageBoxW(L.hwnd, L"The firewall was not changed (the admin prompt was declined or failed).",
                    L"Triton Screen Share", MB_ICONWARNING);
      }
      UpdateFirewall();
      break;
  }
}

// Tab / arrows move between the panel's widgets; Enter / Space press the focused one.
void MoveFocus(int step) {
  std::vector<int> ids;
  for (const Widget& wd : L.widgets) {
    if (wd.enabled) ids.push_back(wd.id);
  }
  if (ids.empty()) return;
  auto it = std::find(ids.begin(), ids.end(), L.focus);
  int i = it == ids.end() ? (step > 0 ? -1 : 0) : static_cast<int>(it - ids.begin());
  i = (i + step + static_cast<int>(ids.size())) % static_cast<int>(ids.size());
  L.keyboard = true;
  SetFocusId(ids[i]);
}

void OnKey(WPARAM key) {
  const bool shift = GetKeyState(VK_SHIFT) < 0;
  switch (key) {
    case VK_TAB: MoveFocus(shift ? -1 : 1); break;
    case VK_DOWN: MoveFocus(1); break;
    case VK_UP: MoveFocus(-1); break;
    case VK_LEFT:
    case VK_RIGHT:
      if (L.focus == ID_MONITOR) Activate(ID_MONITOR);
      break;
    case VK_RETURN:
      if (L.focus == ID_IP || (L.focus >= ID_ROW && L.focus < ID_SENDER_ROW && L.mode == Mode::Idle)) {
        if (L.focus >= ID_ROW) Activate(L.focus);
        OnShare(false);
      } else if (const Widget* wd = FindWidget(L.focus); wd && wd->enabled) {
        Activate(wd->id);
      }
      break;
    case VK_SPACE:
      if (L.focus != ID_IP) {
        if (const Widget* wd = FindWidget(L.focus); wd && wd->enabled) Activate(wd->id);
      }
      break;
  }
}

void PasteIp() {
  if (!OpenClipboard(L.hwnd)) return;
  if (HANDLE data = GetClipboardData(CF_UNICODETEXT)) {
    if (const auto* text = static_cast<const wchar_t*>(GlobalLock(data))) {
      std::wstring s = text;
      GlobalUnlock(data);
      s.erase(std::remove_if(s.begin(), s.end(), [](wchar_t c) { return c <= L' '; }), s.end());
      L.ip = s.substr(0, 40);
    }
  }
  CloseClipboard();
}

// Typing anywhere in the idle panel goes to the IP field.
void OnChar(wchar_t ch) {
  if (L.mode != Mode::Idle || !FindWidget(ID_IP)) return;
  if (ch == 8) {
    if (L.focus == ID_IP && !L.ip.empty()) L.ip.pop_back();
  } else if (ch == 22) {  // Ctrl+V
    PasteIp();
    L.sel = -1;
    SetFocusId(ID_IP);
  } else if (ch > L' ' && L.ip.size() < 40) {
    L.ip += ch;
    L.sel = -1;
    SetFocusId(ID_IP);
  }
  L.caretOn = true;
  Invalidate();
}

LRESULT CALLBACK PanelProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
  switch (msg) {
    case WM_CREATE:
      L.hwnd = hwnd;
      return 0;
    case WM_ACTIVATE:
      // Clicking anywhere outside the panel hides it, except for its own message boxes and the bubble
      // (the bubble's own click toggles it).
      if (LOWORD(wp) == WA_INACTIVE) {
        HWND other = reinterpret_cast<HWND>(lp);
        // Our own picture window taking the focus without a click (it was resized or shown after a
        // panel button) is not "outside": keep the panel and put it back on top.
        DWORD pid = 0;
        if (other) GetWindowThreadProcessId(other, &pid);
        const bool clicked = (GetAsyncKeyState(VK_LBUTTON) | GetAsyncKeyState(VK_RBUTTON)) & 0x8000;
        if (pid == GetCurrentProcessId() && other != L.bubble && !clicked) {
          PostMessageW(hwnd, WM_APP_RAISE, 0, 0);
        } else if (!L.pending && !L.pairAsk && (!other || (GetWindow(other, GW_OWNER) != hwnd && other != L.bubble))) {
          HidePanel();
        }
      }
      return 0;
    case WM_APP_RAISE:
      if (IsWindowVisible(hwnd)) {
        SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
        SetForegroundWindow(hwnd);
      }
      return 0;
    case WM_ERASEBKGND:
      return 1;
    case WM_PAINT: {
      PAINTSTRUCT ps;
      HDC dc = BeginPaint(hwnd, &ps);
      PaintPanel(dc);
      EndPaint(hwnd, &ps);
      return 0;
    }
    case WM_SETCURSOR:
      if (LOWORD(lp) == HTCLIENT) {
        POINT p;
        GetCursorPos(&p);
        ScreenToClient(hwnd, &p);
        const Widget* wd = WidgetAt(MAKELPARAM(p.x, p.y));
        SetCursor(LoadCursorW(nullptr, !wd || !wd->enabled ? IDC_ARROW : wd->kind == Kind::Field ? IDC_IBEAM : IDC_HAND));
        return TRUE;
      }
      break;
    case WM_MOUSEMOVE: {
      if (!L.tracking) {
        TRACKMOUSEEVENT tme{sizeof(tme), TME_LEAVE, hwnd, 0};
        L.tracking = TrackMouseEvent(&tme);
      }
      const Widget* wd = WidgetAt(lp);
      const int hover = wd ? wd->id : ID_NONE;
      if (hover != L.hover) {
        L.hover = hover;
        Invalidate();
      }
      return 0;
    }
    case WM_MOUSELEAVE:
      L.tracking = false;
      L.hover = ID_NONE;
      Invalidate();
      return 0;
    case WM_LBUTTONDOWN:
    case WM_LBUTTONDBLCLK: {
      L.keyboard = false;
      const Widget* wd = WidgetAt(lp);
      L.press = wd && wd->enabled ? wd->id : ID_NONE;
      if (msg == WM_LBUTTONDBLCLK && L.press >= ID_ROW && L.press < ID_SENDER_ROW) {  // double-click a PC: share to it
        L.press = ID_NONE;
        Activate(wd->id);
        OnShare(false);
        return 0;
      }
      if (L.press != ID_NONE) SetCapture(hwnd);
      SetFocusId(L.press == ID_IP ? ID_IP : L.focus == ID_IP ? ID_NONE : L.focus);
      return 0;
    }
    case WM_LBUTTONUP: {
      const int pressed = L.press;
      L.press = ID_NONE;
      ReleaseCapture();
      const Widget* wd = WidgetAt(lp);
      if (pressed != ID_NONE && wd && wd->id == pressed) Activate(pressed);
      Invalidate();
      return 0;
    }
    case WM_KEYDOWN:
      OnKey(wp);
      return 0;
    case WM_CHAR:
      OnChar(static_cast<wchar_t>(wp));
      return 0;
    case WM_TIMER:
      if (wp == kCaretTimer) {
        L.caretOn = !L.caretOn;
        Invalidate();
      } else if (wp == kPullTimeoutTimer) {
        AnswerPull(0);  // nobody answered
      } else if (wp == kVoiceTimer) {
        VoiceTick();
      } else if (wp == kRelayoutTimer) {
        // Opening a webcam or mic takes a moment: refresh for ~6 s so the button shows the outcome.
        static int ticks = 0;
        Relayout();
        if (++ticks >= 12) {
          ticks = 0;
          KillTimer(hwnd, kRelayoutTimer);
        }
      }
      return 0;
    case WM_DPICHANGED:
      if (IsWindowVisible(hwnd)) PlacePanel();
      return 0;
    case WM_APP_LOG: {
      std::unique_ptr<std::string> s(reinterpret_cast<std::string*>(lp));
      OnOutput(*s);
      return 0;
    }
    case WM_APP_FOUND: {
      std::unique_ptr<std::vector<FoundReceiver>> f(reinterpret_cast<std::vector<FoundReceiver>*>(lp));
      std::vector<FoundReceiver> receivers, senders;
      static const std::string self = ComputerName();
      for (FoundReceiver& r : *f) {
        if (!r.sender) receivers.push_back(std::move(r));
        else if (r.name != self) senders.push_back(std::move(r));
      }
      const bool sendersChanged = senders.size() != L.senders.size() ||
          !std::equal(senders.begin(), senders.end(), L.senders.begin(),
                      [](const FoundReceiver& a, const FoundReceiver& b) { return a.ip == b.ip && a.name == b.name; });
      L.senders = std::move(senders);
      if (L.mode == Mode::Idle) {
        ShowFound(std::move(receivers));
        MaybeAutoShare();
      } else if (sendersChanged && L.mode == Mode::Showing) Relayout();
      return 0;
    }
    case WM_APP_DEVICES: {
      std::unique_ptr<DeviceLists> lists(reinterpret_cast<DeviceLists*>(lp));
      // Keep a pick only while that device is still there.
      auto keep = [](std::string& pick, const std::vector<MediaDevice>& list) {
        if (std::none_of(list.begin(), list.end(), [&](const MediaDevice& d) { return d.id == pick; })) pick.clear();
      };
      L.mics = std::move(lists->mics);
      L.cameras = std::move(lists->cameras);
      keep(L.micPick, L.mics);
      keep(L.cameraPick, L.cameras);
      L.devicesKnown = true;
      Relayout();
      return 0;
    }
    case WM_APP_PAIR:
      if (!lp) {  // withdrawn (timed out)
        AnswerPair(false);
        return 0;
      }
      {
        std::unique_ptr<std::shared_ptr<PairAsk>> ask(reinterpret_cast<std::shared_ptr<PairAsk>*>(lp));
        if (L.pairAsk) L.pairAsk->answer.set_value(false);  // an older question is replaced
        else L.statusBeforePair = L.status;
        L.pairAsk = *ask;
        L.status = L"Pair with " + Wide(L.pairAsk->name) + L"?";
        ShowPanel();
        Relayout();
      }
      return 0;
    case WM_APP_CONTROL: {
      std::unique_ptr<ControlRequest> req(reinterpret_cast<ControlRequest*>(lp));
      req->reply.set_value(L.closing ? std::string("{\"ok\":false,\"error\":\"closing\"}") : OnControl(req->command));
      return 0;
    }
    case WM_APP_PULL:
      OnPullRequest(std::unique_ptr<PendingPull>(reinterpret_cast<PendingPull*>(lp)));
      return 0;
    case WM_APP_PULL_RESULT: {
      std::unique_ptr<std::wstring> name(reinterpret_cast<std::wstring*>(lp));
      if (L.mode == Mode::Showing && !ReceiverInSession()) {
        L.status = wp == 1   ? *name + L" accepted - connecting..."
                   : wp == 0 ? *name + L" didn't allow it."
                   : wp == 2 ? *name + L" is busy right now."
                             : L"Couldn't reach " + *name + L".";
        Relayout();
      }
      return 0;
    }
    case WM_APP_DONE:
      if (L.worker.joinable()) L.worker.join();
      g_stop = false;
      L.status = wp == 0 ? kIdleStatus : L"Stopped - see the line below.";
      if (L.autoShare) L.status = L"Will share again when the other PC shows up.";
      if (wp == 0) L.detail.clear();
      L.showDetail = wp != 0;
      SetMode(Mode::Idle);
      if (wp != 0 && !L.autoShare) ShowPanel();  // an error: show it without waiting for a click
      return 0;
    case WM_DESTROY:
      PostQuitMessage(0);
      return 0;
  }
  return DefWindowProcW(hwnd, msg, wp, lp);
}

LRESULT CALLBACK BubbleProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
  switch (msg) {
    case WM_MOUSEACTIVATE:
      return MA_NOACTIVATE;  // clicking the bubble never steals focus from the video / other apps
    case WM_LBUTTONDOWN: {
      SetCapture(hwnd);
      GetCursorPos(&L.dragFrom);
      RECT r;
      GetWindowRect(hwnd, &r);
      L.bubbleFrom = {r.left, r.top};
      L.pressed = true;
      L.dragging = false;
      return 0;
    }
    case WM_MOUSEMOVE: {
      if (!L.hovered) {
        L.hovered = true;
        KillTimer(hwnd, kFadeTimer);
        SetBubbleAlpha(255);
        TRACKMOUSEEVENT tme{sizeof(tme), TME_LEAVE, hwnd, 0};
        TrackMouseEvent(&tme);
      }
      if (L.pressed) {
        POINT p;
        GetCursorPos(&p);
        const int dx = p.x - L.dragFrom.x, dy = p.y - L.dragFrom.y;
        if (!L.dragging && std::abs(dx) + std::abs(dy) > MulDiv(5, static_cast<int>(GetDpiForWindow(hwnd)), 96)) {
          L.dragging = true;
          HidePanel();
        }
        if (L.dragging) {
          SetWindowPos(hwnd, HWND_TOPMOST, L.bubbleFrom.x + dx, L.bubbleFrom.y + dy, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE);
        }
      }
      return 0;
    }
    case WM_LBUTTONUP:
      if (!L.pressed) return 0;
      L.pressed = false;
      ReleaseCapture();
      if (L.dragging) {
        SnapBubble();
      } else if (IsWindowVisible(L.hwnd)) {
        HidePanel();
      } else {
        ShowPanel();
      }
      L.dragging = false;
      return 0;
    case WM_MOUSELEAVE:
      L.hovered = false;
      if (!IsWindowVisible(L.hwnd)) SetTimer(hwnd, kFadeTimer, 2500, nullptr);
      return 0;
    case WM_TIMER:
      if (wp == kFadeTimer) {
        KillTimer(hwnd, kFadeTimer);
        if (!L.hovered && !IsWindowVisible(L.hwnd)) SetBubbleAlpha(150);  // fade out of the way when idle
      } else if (wp == kTopmostTimer && !L.dragging) {
        // Stay above everything, including full-screen windows (Nova Launcher, the video) that were
        // brought to the front after the bubble.
        SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER);
      }
      return 0;
    case WM_RBUTTONUP: {
      HMENU menu = CreatePopupMenu();
      AppendMenuW(menu, MF_STRING, 1, L"Quit Triton Screen Share");
      POINT p;
      GetCursorPos(&p);
      SetForegroundWindow(hwnd);
      const int cmd = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, p.x, p.y, 0, hwnd, nullptr);
      DestroyMenu(menu);
      if (cmd == 1) Quit();
      return 0;
    }
    case WM_DPICHANGED:
      RenderBubble();
      return 0;
    case WM_DISPLAYCHANGE:
      SnapBubble();
      return 0;
    case WM_APP_OPEN:
      ShowPanel();
      return 0;
    case WM_APP_QUIT:
      Quit();
      return 0;
  }
  return DefWindowProcW(hwnd, msg, wp, lp);
}

// Windows 11 look: dark mode, rounded corners, a subtle border and the blurred acrylic backdrop.
void StylePanel(HWND hwnd) {
  const BOOL dark = TRUE;
  DwmSetWindowAttribute(hwnd, 20 /* DWMWA_USE_IMMERSIVE_DARK_MODE */, &dark, sizeof(dark));
  const DWORD round = 2;  // DWMWCP_ROUND
  DwmSetWindowAttribute(hwnd, 33 /* DWMWA_WINDOW_CORNER_PREFERENCE */, &round, sizeof(round));
  const COLORREF border = RGB(74, 78, 100);
  DwmSetWindowAttribute(hwnd, 34 /* DWMWA_BORDER_COLOR */, &border, sizeof(border));
  const DWORD acrylic = 3;  // DWMSBT_TRANSIENTWINDOW, the glass of Windows 11 flyouts (22H2 and later)
  L.glass = SUCCEEDED(DwmSetWindowAttribute(hwnd, 38 /* DWMWA_SYSTEMBACKDROP_TYPE */, &acrylic, sizeof(acrylic)));
  if (L.glass) {
    const MARGINS all{-1, -1, -1, -1};  // the whole window is "frame", so the backdrop shows through
    DwmExtendFrameIntoClientArea(hwnd, &all);
  }
}

}  // namespace

int RunLauncher(bool openPanel) {
  // One bubble per PC: a second launch just opens the existing panel.
  CreateMutexW(nullptr, FALSE, Named(L"TritonScreenShare.Launcher").c_str());
  if (GetLastError() == ERROR_ALREADY_EXISTS) {
    if (HWND other = FindWindowW(kBubbleClass, nullptr); other && openPanel) PostMessageW(other, WM_APP_OPEN, 0, 0);
    return 0;
  }
  CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
  gp::GdiplusStartupInput gsi;
  ULONG_PTR gdiplus = 0;
  gp::GdiplusStartup(&gdiplus, &gsi, nullptr);
  L.regular = Family({L"Segoe UI Variable Text", L"Segoe UI"});
  L.semibold = Family({L"Segoe UI Variable Text Semibold", L"Segoe UI Semibold", L"Segoe UI"});
  L.monitors = ListMonitors();
  for (size_t i = 0; i < L.monitors.size(); ++i) {
    if (L.monitors[i].primary) L.monitor = static_cast<int>(i);
  }
  const HINSTANCE inst = GetModuleHandleW(nullptr);

  WNDCLASSEXW wc{sizeof(wc)};
  wc.style = CS_DBLCLKS;
  wc.lpfnWndProc = PanelProc;
  wc.hInstance = inst;
  wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
  wc.hIcon = LoadIconW(nullptr, IDI_APPLICATION);
  wc.lpszClassName = kPanelClass;
  RegisterClassExW(&wc);
  WNDCLASSEXW bc{sizeof(bc)};
  bc.lpfnWndProc = BubbleProc;
  bc.hInstance = inst;
  bc.hCursor = LoadCursorW(nullptr, IDC_HAND);
  bc.lpszClassName = kBubbleClass;
  RegisterClassExW(&bc);

  // Neither window is in the taskbar or Alt+Tab, and neither shows up in the shared screen.
  L.hwnd = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_TOPMOST, kPanelClass, L"Triton Screen Share", WS_POPUP,
                           0, 0, 10, 10, nullptr, nullptr, inst, nullptr);
  L.bubble = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_NOACTIVATE, kBubbleClass,
                             L"Triton Screen Share bubble", WS_POPUP, 0, 0, 10, 10, nullptr, nullptr, inst, nullptr);
  if (!L.hwnd || !L.bubble) return 1;
  StylePanel(L.hwnd);
  if (!std::getenv("LFRD_TEST_CAPTURABLE")) {  // test hook: let screenshots see the bubble and panel
    SetWindowDisplayAffinity(L.hwnd, WDA_EXCLUDEFROMCAPTURE);
    SetWindowDisplayAffinity(L.bubble, WDA_EXCLUDEFROMCAPTURE);
  }

  // Start on the right edge of the main screen, a bit above the middle.
  const RECT work = WorkAreaAt({0, 0});
  const int size = BubblePx();
  SetWindowPos(L.bubble, HWND_TOPMOST, work.right - size - MulDiv(6, size, kBubbleSize),
               work.top + (work.bottom - work.top) * 2 / 5, size, size, SWP_NOACTIVATE);
  RenderBubble();
  ShowWindow(L.bubble, SW_SHOWNOACTIVATE);
  SetTimer(L.bubble, kTopmostTimer, 2000, nullptr);

  // Whoever started us (Nova Launcher) can close us tidily by setting this event.
  L.quitEvent = CreateEventW(nullptr, FALSE, FALSE, Named(kQuitEvent).c_str());
  if (L.quitEvent) {
    std::thread([] {
      WaitForSingleObject(L.quitEvent, INFINITE);
      if (!L.closing) PostMessageW(L.bubble, WM_APP_QUIT, 0, 0);
    }).detach();
  }

  CaptureOutput();
  std::printf("Triton Screen Share started on %s.\n", ComputerName().c_str());
  SetPairPrompt(AskToPair);
  VddDetach();  // a virtual screen left on the desktop by an interrupted extend session
  L.firewallNeedsFix = FirewallNeedsFix();
  LoadSettings();
  L.advertise = true;
  L.autoShare = L.role == Role::AlwaysShare;
  if (L.autoShare) L.status = L"Shares by itself to PCs that “Show here”.";
  L.autostart = AutostartOn();
  if (L.autostart) SetAutostart(true);  // keep the entry pointing here if the folder moved
  if (L.role == Role::AlwaysShow) {
    OnShow();  // straight into "Show here"
  } else if (openPanel) {
    ShowPanel();  // open once so a first-time user sees what the bubble is for
  } else {
    Layout();
    SetTimer(L.bubble, kFadeTimer, 2500, nullptr);
  }
  L.discovery = std::thread(DiscoveryLoop);
  L.pullListener = std::thread(PullListenerLoop);
  std::thread(ControlPipeLoop).detach();  // blocks in ConnectNamedPipe; ends with the process

  MSG msg;
  while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
    if (msg.message == WM_KEYDOWN && msg.wParam == VK_ESCAPE && msg.hwnd == L.hwnd) {
      HidePanel();
      continue;
    }
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }
  L.regular.reset();  // GDI+ objects must go before GDI+ itself, not in L's static destructor
  L.semibold.reset();
  gp::GdiplusShutdown(gdiplus);
  CoUninitialize();
  return 0;
}

}  // namespace lfrd
