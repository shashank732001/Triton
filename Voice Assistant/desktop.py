"""
Low-level Windows controls the voice commands use: the speaker volume (exact percent, mute), the
open windows (find, close, minimize, maximize, bring to front) and key presses. Plain ctypes, so it
also works in the packaged TritonVoice.exe without extra packages.
"""

import ctypes
import time
from ctypes import POINTER, WINFUNCTYPE, byref, c_float, c_int, c_ulong, c_void_p, wintypes
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ole32 = ctypes.windll.ole32
dwmapi = ctypes.windll.dwmapi


# ---------- keys ----------

KEYEVENTF_KEYUP = 0x2
VK = {"alt": 0x12, "ctrl": 0x11, "shift": 0x10, "win": 0x5B, "tab": 0x09, "d": 0x44, "w": 0x57,
      "next track": 0xB0, "previous track": 0xB1, "play pause": 0xB3}


def press(*keys):
    """Press a key combination, e.g. press("ctrl", "w")."""
    codes = [VK[k] for k in keys]
    for c in codes:
        user32.keybd_event(c, 0, 0, 0)
    for c in reversed(codes):
        user32.keybd_event(c, 0, KEYEVENTF_KEYUP, 0)


# ---------- volume (Core Audio IAudioEndpointVolume, through its COM vtable) ----------

class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text):
        super().__init__()
        ole32.CLSIDFromString(text, byref(self))


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
CLSCTX_ALL = 23
ole32.CoInitializeEx.restype = ctypes.c_long        # S_FALSE / "already initialised" aren't errors here


def _method(obj, index, *argtypes, restype=ctypes.HRESULT):
    """Method `index` of a COM object's vtable, bound to the object (HRESULT failures raise OSError)."""
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    fn = WINFUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])
    return lambda *args: fn(obj, *args)


class _Speakers:
    """The default playback device's volume control; looked up per use, so a new default device is picked up."""

    def __enter__(self):
        ole32.CoInitializeEx(None, 0)
        enum, device, self.volume = c_void_p(), c_void_p(), c_void_p()
        ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                               byref(IID_IMMDeviceEnumerator), byref(enum))
        try:
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia, &device)
            _method(enum, 4, c_int, c_int, POINTER(c_void_p))(0, 1, byref(device))
            # IMMDevice::Activate(IID_IAudioEndpointVolume, CLSCTX_ALL, NULL, &volume)
            _method(device, 3, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))(
                byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None, byref(self.volume))
        finally:
            for obj in (device, enum):
                if obj:
                    _method(obj, 2, restype=c_ulong)()      # Release
        return self

    def __exit__(self, *exc):
        _method(self.volume, 2, restype=c_ulong)()

    def get(self):
        level = c_float()
        _method(self.volume, 9, POINTER(c_float))(byref(level))         # GetMasterVolumeLevelScalar
        return level.value

    def set(self, level):
        _method(self.volume, 7, c_float, c_void_p)(max(0.0, min(1.0, level)), None)   # SetMasterVolumeLevelScalar

    def muted(self):
        flag = wintypes.BOOL()
        _method(self.volume, 15, POINTER(wintypes.BOOL))(byref(flag))     # GetMute
        return bool(flag.value)

    def mute(self, on):
        _method(self.volume, 14, wintypes.BOOL, c_void_p)(bool(on), None)  # SetMute


def get_volume():
    """Speaker volume, 0-100."""
    with _Speakers() as s:
        return round(s.get() * 100)


def set_volume(percent):
    """Set the speaker volume to `percent` (0-100) and unmute; returns the new volume."""
    with _Speakers() as s:
        s.set(percent / 100)
        if percent > 0:
            s.mute(False)
        return round(s.get() * 100)


def set_mute(on):
    with _Speakers() as s:
        s.mute(on)


# ---------- windows ----------

WM_CLOSE = 0x0010
SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# The desktop and taskbar are explorer.exe windows too; never close or minimise those
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

EnumWindowsProc = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindow.restype = wintypes.HWND


@dataclass
class Window:
    hwnd: int
    title: str
    exe: str          # "chrome" for ...\chrome.exe ("" if Windows wouldn't say)

    def close(self):
        user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def minimize(self):
        user32.ShowWindow(self.hwnd, SW_MINIMIZE)

    def maximize(self):
        user32.ShowWindow(self.hwnd, SW_MAXIMIZE)

    def focus(self):
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
        # Windows only lets the foreground app hand focus around; a tap of Alt counts as user input
        press("alt")
        user32.SetForegroundWindow(self.hwnd)


def _text(hwnd, getter, size=512):
    buf = ctypes.create_unicode_buffer(size)
    getter(hwnd, buf, size)
    return buf.value


def _exe(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
            return Path(buf.value).stem.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _is_app_window(hwnd):
    """A window the user would call an app: visible, titled, on the taskbar, not a hidden UWP ghost."""
    if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
        return False
    if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return False
    cloaked = c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, byref(cloaked), ctypes.sizeof(cloaked))
    if cloaked.value:
        return False
    return _text(hwnd, user32.GetClassNameW) not in SHELL_CLASSES


def open_windows():
    """All app windows, frontmost first."""
    found = []

    def visit(hwnd, _):
        if _is_app_window(hwnd):
            title = _text(hwnd, user32.GetWindowTextW)
            if title:
                found.append(Window(hwnd, title, _exe(hwnd)))
        return True

    user32.EnumWindows(EnumWindowsProc(visit), 0)
    return found


def foreground_window():
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not _is_app_window(hwnd):
        return None
    return Window(hwnd, _text(hwnd, user32.GetWindowTextW), _exe(hwnd))


def close_tab(window):
    """Close the tab a browser window is showing (its title is that tab's title)."""
    window.focus()
    time.sleep(0.3)
    press("ctrl", "w")


def lock_screen():
    user32.LockWorkStation()
