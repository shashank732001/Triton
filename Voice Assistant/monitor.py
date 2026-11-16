"""
Turn external monitors off/on over DDC/CI (the monitor's own power control), so the PC stays
awake and unlocked. Falls back to Windows' SC_MONITORPOWER (which puts this laptop into
Modern Standby, i.e. PIN on wake) if the monitor doesn't respond.
"""

import ctypes
import logging
import threading
import time
from ctypes import wintypes

log = logging.getLogger("assistant")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dxva2 = ctypes.windll.dxva2

VCP_POWER_MODE = 0xD6
POWER_ON = 1
POWER_OFF = 4               # DPM "off"; the monitor keeps listening on DDC/CI so it can be woken

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", wintypes.HANDLE),
                ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


def set_ddc_power(mode):
    """Send the power mode to every monitor that accepts DDC/CI. Returns how many accepted it."""
    hmons = []
    user32.EnumDisplayMonitors(None, None, _MonitorEnumProc(lambda h, dc, r, p: hmons.append(h) or True), 0)
    accepted = 0
    for hmon in hmons:
        count = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)) or not count.value:
            continue
        monitors = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count, monitors):
            continue
        for m in monitors:
            if dxva2.SetVCPFeature(m.hPhysicalMonitor, VCP_POWER_MODE, mode):
                accepted += 1
            dxva2.DestroyPhysicalMonitor(m.hPhysicalMonitor)
    return accepted


def _last_input_tick():
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(info))
    return info.dwTime


class MonitorControl:
    def __init__(self):
        self.is_off = False
        self._lock = threading.Lock()
        self._woken = threading.Event()

    def off(self):
        with self._lock:
            if self.is_off:
                return
            if set_ddc_power(POWER_OFF):
                log.info("Monitor off via DDC/CI")
                self.is_off = True
                self._woken.clear()
                threading.Thread(target=self._stay_awake, daemon=True).start()
                threading.Thread(target=self._wake_on_input, daemon=True).start()
                return
        log.warning("Monitor didn't accept DDC/CI; falling back to SC_MONITORPOWER (PIN needed on wake)")
        user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)   # HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, off

    def on(self):
        with self._lock:
            accepted = set_ddc_power(POWER_ON)
            self.is_off = False
            self._woken.set()
        # In case Windows turned the display off itself, nudge the mouse to wake it too
        user32.mouse_event(0x0001, 0, 0, 0, 0)            # MOUSEEVENTF_MOVE by 0 px
        log.info("Monitor on (%d monitor(s) accepted DDC/CI)", accepted)

    def _stay_awake(self):
        # Windows still thinks the display is on; stop it (and the PC) from timing out into
        # Modern Standby, which would lock the PC and pause the mic. The request belongs to
        # this thread, so it holds it until the monitor is switched back on.
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        self._woken.wait()
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)

    def _wake_on_input(self):
        # The monitor ignores the mouse/keyboard while DDC-off (Windows thinks it's still on),
        # so watch for input ourselves and switch it back on.
        time.sleep(1.0)                      # ignore input from the moment of switching off
        tick_at_off = _last_input_tick()
        while self.is_off:
            if _last_input_tick() != tick_at_off:
                log.info("Keyboard/mouse input while monitor off; waking it")
                self.on()
                return
            time.sleep(0.3)
