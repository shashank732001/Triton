"""
"Hey Triton" listening overlay, in Triton Launcher's style: an aurora glow (the launcher's cyan, violet,
pink and mint) that rises from the bottom edge of the screen and ripples with your voice, and a
glass caption pill above it - "Listening…", then what was understood ("Open Chrome") or
"Didn't catch that".

Drawn into a per-pixel-alpha layered window (UpdateLayeredWindow), so the glow melts into whatever
is on screen. Click-through, never takes focus, hidden (and costs nothing) when not in use.
Runs its own thread; show(), set_level() and done() are safe to call from anywhere.
"""

import ctypes
import logging
import math
import queue
import threading
import time
from ctypes import wintypes as wt

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont

FPS = 30
BAND_DIP = 230            # height of the overlay strip at 100% display scaling (glow + caption)
LOW = 4                   # the glow is computed at 1/4 resolution and scaled up (it's soft anyway)
CAPTION_Y_DIP = 52        # caption centre, this far above the bottom edge (below the launcher's tile labels)
IDLE_TIMEOUT_SEC = 8      # give up on its own if no result arrives
THINK_TIMEOUT_SEC = 30    # ...or, while "Thinking…", if the model or a long request never finishes
OK_HOLD_SEC, FAIL_HOLD_SEC, FADE_SEC = 0.9, 1.3, 0.45

# The launcher's aurora palette, in BGR order (the window bitmap is BGRA); the last entry wraps to the first
PALETTE = np.array([
    (255, 200, 0),        # cyan
    (255, 90, 120),       # violet
    (170, 60, 255),       # pink
    (190, 235, 40),       # mint
    (255, 200, 0),
], dtype=np.float32)
OK_TINT = np.array((190, 235, 60), dtype=np.float32)      # mint
FAIL_TINT = np.array((150, 118, 104), dtype=np.float32)   # muted slate
BAR_COLORS = [(0, 200, 255), (120, 90, 255), (255, 60, 170), (120, 90, 255), (0, 200, 255)]   # RGB
CHECK_RGB = (60, 235, 190)
FONT_FILES = [r"C:\Windows\Fonts\segoeuisb.ttf", r"C:\Windows\Fonts\segoeui.ttf"]

log = logging.getLogger("assistant")


def _ease_out(x):
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


# ---------- drawing (no window needed, so it can also be previewed into a picture) ----------

class Renderer:
    def __init__(self, width, height, scale):
        self.w, self.h, self.s = width, height, scale
        lw, lh = math.ceil(width / LOW), math.ceil(height / LOW)
        self.xs = np.linspace(0, 1, lw, dtype=np.float32)
        # distance of each low-res row from the bottom edge, in DIPs
        self.d = ((lh - 1 - np.arange(lh, dtype=np.float32)) * LOW / scale)[:, None]
        self.center = 1 - 0.32 * ((self.xs - 0.5) * 2) ** 2      # a little taller in the middle
        self.top_fade = np.clip((BAND_DIP - 10 - self.d) / 70, 0, 1) ** 2   # fade out well before the strip ends
        self.font = self._font(int(26 * scale))
        self._caption_cache = (None, None)

    @staticmethod
    def _font(size):
        for path in FONT_FILES:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
        return ImageFont.load_default()

    def glow(self, t, level, grow, bright, tint=None, tint_amount=0.0):
        """The aurora at full window size: a premultiplied BGRA image (Pillow mode "RGBa")."""
        xs = self.xs
        u = (xs * 1.15 + t * 0.07) % 1.0 * (len(PALETTE) - 1)
        i = u.astype(int)
        f = (u - i)[:, None]
        color = PALETTE[i] * (1 - f) + PALETTE[i + 1] * f
        if tint is not None and tint_amount:
            color = color * (1 - tint_amount) + tint * tint_amount

        wave = (1 + 0.30 * np.sin(xs * 9.0 + t * 1.9)
                + 0.22 * np.sin(xs * 17.0 - t * 2.7 + 1.3)
                + 0.12 * np.sin(xs * 31.0 + t * 4.1))
        height = np.maximum((32 + 100 * level) * grow * wave * self.center, 1.0)
        alpha = np.exp(-(self.d / height[None, :]) ** 2) * 0.8 * bright * self.top_fade
        core = np.exp(-(self.d / (2.5 * grow + 0.01)) ** 2) * grow       # bright line right at the edge
        alpha = np.clip(alpha + core * bright, 0, 1)

        whiten = (core * 0.55)[..., None]
        rgb = color[None, :, :] * (1 - whiten) + 255 * whiten
        low = np.empty(alpha.shape + (4,), np.uint8)
        low[..., :3] = np.clip(rgb * alpha[..., None], 0, 255)
        low[..., 3] = alpha * 255
        # "RGBa" = already premultiplied, so Pillow scales it as-is (no premultiply/unpremultiply round trip)
        img = Image.frombuffer("RGBa", (low.shape[1], low.shape[0]), low.tobytes(), "raw", "RGBa", 0, 1)
        return img.resize((self.w, self.h), Image.BILINEAR)

    def _caption_base(self, text):
        """The glass pill with its text (straight RGBA); cached because the text rarely changes."""
        if self._caption_cache[0] == text:
            return self._caption_cache[1]
        s = self.s
        ph, pad, icon_w, gap = int(62 * s), int(22 * s), int(46 * s), int(12 * s)
        pw = int(pad + icon_w + gap + self.font.getlength(text) + pad + 6 * s)
        ss = 3                                                     # supersample the shape for smooth edges
        big = Image.new("RGBA", (pw * ss, ph * ss), (0, 0, 0, 0))
        ImageDraw.Draw(big).rounded_rectangle((0, 0, pw * ss - 1, ph * ss - 1), radius=ph * ss // 2,
                                              fill=(16, 18, 30, 214), outline=(255, 255, 255, 52), width=ss)
        # Faint glass sheen: white fading out down the top half, clipped to the pill's shape
        fade = Image.linear_gradient("L").resize((pw * ss, ph * ss)).point(lambda v: max(0, 26 - v // 5))
        sheen = Image.new("RGBA", big.size, (255, 255, 255, 0))
        sheen.putalpha(ImageChops.multiply(fade, big.getchannel("A")))
        big.alpha_composite(sheen)
        base = big.resize((pw, ph), Image.LANCZOS)
        ImageDraw.Draw(base).text((pad + icon_w + gap, ph / 2), text, font=self.font,
                                  fill=(255, 255, 255, 238), anchor="lm")
        self._caption_cache = (text, (base, pad, icon_w))
        return self._caption_cache[1]

    def _icon(self, mode, t, level, size):
        """Voice meter bars (listening), a check (ok) or a flat line (fail), supersampled."""
        ss = 3
        w, h = size
        img = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if mode == "ok":
            pts = [(w * 0.22, h * 0.52), (w * 0.42, h * 0.70), (w * 0.78, h * 0.32)]
            d.line([(x * ss, y * ss) for x, y in pts], fill=CHECK_RGB + (255,), width=int(h * 0.09 * ss), joint="curve")
        else:
            bar_w, gap = w * 0.12, w * 0.07
            x = (w - (5 * bar_w + 4 * gap)) / 2
            for k, rgb in enumerate(BAR_COLORS):
                if mode == "fail":
                    frac, rgb = 0.14, (150, 156, 175)
                else:
                    env = (0.55, 0.85, 1.0, 0.85, 0.55)[k]
                    wobble = 0.55 + 0.45 * math.sin(t * (6.0 + k * 1.7) + k * 1.3)
                    frac = 0.16 + 0.84 * min(1.0, level * 1.4 + 0.12) * env * wobble
                bh = max(bar_w, h * 0.62 * frac)
                y0 = (h - bh) / 2
                d.rounded_rectangle((x * ss, y0 * ss, (x + bar_w) * ss, (y0 + bh) * ss),
                                    radius=bar_w * ss / 2, fill=rgb + (255,))
                x += bar_w + gap
        return img.resize(size, Image.LANCZOS)

    def frame(self, t, level, grow, bright, tint, tint_amount, text, mode, rise):
        """One complete frame as premultiplied BGRA bytes. rise (0..1) slides the caption in."""
        out = self.glow(t, level, grow, bright, tint, tint_amount)
        base, pad, icon_w = self._caption_base(text)
        pill = base.copy()
        pill.alpha_composite(self._icon(mode, t, level, (icon_w, pill.height)), (pad, 0))

        # Premultiplied "over": paste(src, mask) computes src*mask + dst*(1-mask) per band, so the source is
        # the caption's straight colours (as BGR) with alpha 255, and the mask is its alpha.
        r, g, b, a = pill.split()
        src = Image.merge("RGBa", (b, g, r, Image.new("L", pill.size, 255)))
        mask = a if rise >= 1 else a.point(lambda v: int(v * rise))
        x0 = (self.w - pill.width) // 2
        y0 = int(self.h - CAPTION_Y_DIP * self.s - pill.height / 2 + (1 - rise) * 18 * self.s)
        out.paste(src, (x0, y0), mask)
        return out.tobytes()


# ---------- Win32 layered window ----------

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x80000, 0x20, 0x80   # transparent = click-through
WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x08000000, 0x8
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
HWND_TOPMOST = wt.HWND(-1)
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
PM_REMOVE = 0x1


class Overlay:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)    # real pixels on scaled (TV) displays
        except Exception:
            pass
        self._cmds = queue.Queue()
        self._target_level = 0.0
        threading.Thread(target=self._run, name="overlay", daemon=True).start()

    # ---------- thread-safe API ----------

    def show(self, text=None):
        """Start listening; text replaces "Listening…" (e.g. a question waiting for an answer)."""
        self._cmds.put(("show", text))

    def thinking(self, text=None):
        """Working on it (the local model, or between the steps of a longer request)."""
        self._cmds.put(("think", text))

    def loading(self, text):
        """Starting up: stays until done() (no "took too long")."""
        self._cmds.put(("load", text))

    def set_level(self, level):
        """Voice loudness, 0..1."""
        self._target_level = max(0.0, min(1.0, level))

    def done(self, success, text=None):
        """success: understood or not; text: what to show, e.g. "Open Chrome"."""
        self._cmds.put(("ok" if success else "fail", text))

    def hide(self):
        """Fade out quietly, no verdict (a wake-up with nothing said after it: most likely a false one)."""
        self._cmds.put(("hide", None))

    # ---------- overlay thread ----------

    def _run(self):
        try:
            self._loop()
        except Exception:
            log.exception("Overlay stopped working")

    def _loop(self):
        scale = user32.GetDpiForSystem() / 96
        width, screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        height = int(BAND_DIP * scale)
        self.pos, self.size = wt.POINT(0, screen_h - height), wt.SIZE(width, height)
        self.renderer = Renderer(width, height, scale)
        self._create_window(width, height)

        state, state_at, level, text = "hidden", 0.0, 0.0, ""
        msg = wt.MSG()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                cmd, arg = self._cmds.get(timeout=0.1) if state == "hidden" else self._cmds.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd == "show":
                if state == "hidden":
                    level = 0.0
                state, state_at, text = "in", time.monotonic(), (arg or "Listening…")[:44]
            elif cmd in ("think", "load"):
                if state == "hidden":
                    level = 0.0
                state, state_at, text = cmd, time.monotonic(), (arg or "Thinking…")[:44]
            elif cmd in ("ok", "fail") and state != "hidden":
                state, state_at = cmd, time.monotonic()
                text = (arg or ("Got it" if cmd == "ok" else "Didn't catch that"))[:44]
            elif cmd == "hide" and state not in ("hidden", "gone"):
                state, state_at = "gone", time.monotonic()
            if state == "hidden":
                continue

            started = time.monotonic()
            age = started - state_at
            level += (self._target_level - level) * 0.3
            alpha, grow, bright, rise, tint, tint_amount, mode = 1.0, 1.0, 1.0, 1.0, None, 0.0, "listen"
            if state == "in":
                alpha = _ease_out(age / 0.25)
                grow = _ease_out(age / 0.5)
                rise = _ease_out(age / 0.4)
                if age > IDLE_TIMEOUT_SEC:
                    state, state_at, text = "fail", started, "Didn't catch that"
            elif state in ("think", "load"):
                # A slow, calm pulse instead of following the voice
                level = 0.22 + 0.18 * math.sin(age * 3.2)
                if state == "think" and age > THINK_TIMEOUT_SEC:
                    state, state_at, text = "fail", started, "That took too long"
            elif state == "ok":
                mode, tint, tint_amount = "ok", OK_TINT, min(0.4, age * 2)
                bright = 1 + 0.5 * max(0.0, 1 - age / 0.4)        # a brief swell of light
                level = max(level, 0.6 * max(0.0, 1 - age / 0.6))
                fade = max(0.0, age - OK_HOLD_SEC) / FADE_SEC
                alpha, grow = 1 - _ease_out(fade), 1 - 0.6 * _ease_out(fade)
            elif state == "fail":
                mode, tint, tint_amount = "fail", FAIL_TINT, min(0.65, age * 2)
                fade = max(0.0, age - FAIL_HOLD_SEC) / FADE_SEC
                grow = (1 - 0.35 * _ease_out(age / 0.4)) * (1 - 0.6 * _ease_out(fade))
                alpha = 1 - _ease_out(fade)
            elif state == "gone":
                fade = age / FADE_SEC
                alpha, grow = 1 - _ease_out(fade), 1 - 0.6 * _ease_out(fade)

            if alpha <= 0.0 and state in ("ok", "fail", "gone"):
                state = "hidden"
                user32.ShowWindow(self.hwnd, SW_HIDE)
                self._visible = False
                continue

            frame = self.renderer.frame(started, level, grow, bright, tint, tint_amount, text, mode, rise)
            self._present(frame, alpha)
            time.sleep(max(0.0, 1 / FPS - (time.monotonic() - started)))

    def _create_window(self, width, height):
        self._wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))   # keep a reference
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinst, lpszClassName="TritonVoiceOverlay")
        user32.RegisterClassW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        self.hwnd = user32.CreateWindowExW(ex, "TritonVoiceOverlay", "Triton", WS_POPUP, self.pos.x, self.pos.y,
                                           width, height, None, None, hinst, None)
        screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width,
                                         biHeight=-height, biPlanes=1, biBitCount=32)   # top-down BGRA
        self.bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.mem_dc, dib)
        user32.ReleaseDC(None, screen_dc)
        self._visible = False

    def _present(self, frame, alpha):
        ctypes.memmove(self.bits, frame, len(frame))
        blend = BLENDFUNCTION(0, 0, int(255 * max(0.0, min(1.0, alpha))), AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(self.pos), ctypes.byref(self.size), self.mem_dc,
                                   ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
        if not self._visible:
            # Shown only once the first frame is in place, so it never flashes empty
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
            self._visible = True
