"""
Seeing and using what's on screen, through Windows UI Automation (pywinauto's "uia" backend).

Every app window exposes a tree of controls (buttons, links, text boxes, list items...) with names,
the same tree screen readers use. snapshot() lists the visible ones in the window in front;
click("subscribe") finds the best-matching name and presses it (Invoke when the control supports
it, otherwise a real mouse click in its middle). Chrome and Edge expose web pages this way too.
"""

import ctypes
import ctypes.wintypes
import difflib
import logging
import re
import time
from dataclasses import dataclass

from pywinauto import mouse
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.keyboard import send_keys
from pywinauto.uia_defines import IUIA
from pywinauto.uia_element_info import UIAElementInfo

log = logging.getLogger("assistant")
user32 = ctypes.windll.user32

# Things worth clicking, best first when two names match equally well
CLICKABLE = ["Button", "Hyperlink", "MenuItem", "TabItem", "ListItem", "CheckBox", "RadioButton", "SplitButton",
             "TreeItem", "DataItem", "ComboBox", "Edit", "Image", "Text"]
MAX_CONTROLS = 600


@dataclass
class Control:
    name: str
    kind: str
    element: object      # the raw IUIAutomationElement

    def wrapper(self):
        return UIAWrapper(UIAElementInfo(self.element))


def _condition():
    uia = IUIA()
    prop = uia.UIA_dll.UIA_ControlTypePropertyId
    types = [uia.known_control_types[k] for k in CLICKABLE if k in uia.known_control_types]
    cond = uia.iuia.CreatePropertyCondition(prop, types[0])
    for t in types[1:]:
        cond = uia.iuia.CreateOrCondition(cond, uia.iuia.CreatePropertyCondition(prop, t))
    visible = uia.iuia.CreatePropertyCondition(uia.UIA_dll.UIA_IsOffscreenPropertyId, False)
    return uia.iuia.CreateAndCondition(cond, visible)


_COND = None
_KIND = {}


def foreground_title():
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def foreground_element():
    hwnd = user32.GetForegroundWindow()
    return IUIA().iuia.ElementFromHandle(hwnd) if hwnd else None


def snapshot(root=None):
    """Visible, named, clickable controls in the window in front (one native FindAll call)."""
    global _COND
    if _COND is None:
        _COND = _condition()
        _KIND.update({v: k for k, v in IUIA().known_control_types.items()})
    root = root or foreground_element()
    if root is None:
        return []
    t0 = time.perf_counter()
    found = root.FindAll(IUIA().tree_scope["descendants"], _COND)
    out = []
    for i in range(min(found.Length, MAX_CONTROLS * 3)):
        el = found.GetElement(i)
        try:
            name = (el.CurrentName or "").strip()
            if name and el.CurrentIsEnabled:
                out.append(Control(name, _KIND.get(el.CurrentControlType, "?"), el))
        except Exception:     # the page changed under us; skip that control
            continue
        if len(out) >= MAX_CONTROLS:
            break
    log.info("UI snapshot: %d controls in %.0f ms", len(out), (time.perf_counter() - t0) * 1000)
    return out


def _words(s):
    return re.findall(r"[a-z0-9]+", s.lower())


# Said about a control but not part of its name: "the subscribe button", "the sign in link"
UI_WORDS = {"the", "a", "on", "button", "link", "icon", "tab", "menu", "item", "option", "box", "field", "thing"}


def best_match(target, controls):
    """The control whose name best fits the spoken target, or None. Exact names win, then names
    containing all the spoken words (shortest first), then near-misses."""
    want = [w for w in _words(target) if w not in UI_WORDS] or _words(target)
    if not want:
        return None
    joined = " ".join(want)
    rank = {k: i for i, k in enumerate(CLICKABLE)}
    if {"box", "field", "bar"} & set(_words(target)):
        rank["Edit"] = -1                     # "the search box" is the text box, not the search button
    scored = []
    for c in controls:
        have = _words(c.name)
        if not have:
            continue
        name = " ".join(have)
        if name == joined:
            score = 3.0
        elif set(want) <= set(have):
            score = 2.0 + len(want) / len(have)
        else:
            score = difflib.SequenceMatcher(None, joined, name).ratio()
            if score < 0.75:
                continue
        scored.append((score, -rank.get(c.kind, 99), c))
    if not scored:
        # Misheard names: "Shashank Samit" / "Shashank73200" for the "Shashank" profile (letters-only, start-aware)
        from brain.clauses import closest
        name = closest(target, [c.name for c in controls if len(c.name) <= 40], cutoff=0.75)
        hits = [c for c in controls if c.name == name]
        return min(hits, key=lambda c: rank.get(c.kind, 99)) if hits else None
    return max(scored, key=lambda s: (s[0], s[1]))[2]


def page_element(root=None):
    """The web page inside a browser window (its biggest Document), or None for other apps."""
    root = root or foreground_element()
    if root is None:
        return None
    u = IUIA()
    cond = u.iuia.CreatePropertyCondition(u.UIA_dll.UIA_ControlTypePropertyId, u.known_control_types["Document"])
    docs = root.FindAll(u.tree_scope["descendants"], cond)
    best, area = None, 0
    for i in range(docs.Length):
        d = docs.GetElement(i)
        try:
            r = d.CurrentBoundingRectangle
            a = (r.right - r.left) * (r.bottom - r.top)
        except Exception:
            continue
        if a > area:
            best, area = d, a
    return best


# A freshly started Chrome shows only its own toolbar (tabs, its profile button...) to UI Automation, and builds
# the page's tree a moment after someone asks for it: wait this long for the page before giving up on it
PAGE_WAIT_SEC = 3.0


def find(target):
    """The on-screen control named like `target`. In a browser the page comes first, and is waited for: a fresh
    Chrome hides the page at first, and "click shashank" would hit Chrome's own profile button named "shashank"
    instead of the Netflix profile. Only then the browser's own buttons (back, tabs...), or the app's controls."""
    root = foreground_element()
    if root is None:
        return None
    page = page_element(root)
    if page is not None:
        end = time.monotonic() + PAGE_WAIT_SEC
        while True:
            c = best_match(target, snapshot(page))
            if c is not None or time.monotonic() >= end:
                break
            time.sleep(0.3)
        if c is not None:
            return c
    return best_match(target, snapshot(root))


def _press(c):
    w = c.wrapper()
    log.info("Clicking %s %r", c.kind, c.name)
    try:
        w.invoke()
    except Exception:
        try:
            w.select()
        except Exception:
            w.click_input()


def click(target):
    """Click the on-screen control named like `target`. Returns its name, or None if there's none."""
    c = find(target)
    if c is None:
        return None
    _press(c)
    return c.name


def _search_box(controls):
    return next((c for c in controls if c.kind in ("Edit", "ComboBox") and "search" in c.name.lower()), None)


def search_here(query):
    """Search inside the page or app in front, with its own search box (YouTube, Netflix, Spotify, Settings...):
    click the box, or the Search button that opens one (Netflix), type, Enter. False if there's no search there."""
    root = foreground_element()
    if root is None:
        return False
    scope = page_element(root) or root
    controls = snapshot(scope)
    box = _search_box(controls)
    if box is None:
        # Search buttons only on web pages (an app's "Search" menu is something else)
        button = next((c for c in controls if c.kind in ("Button", "Hyperlink")
                       and _words(c.name)[:1] == ["search"]), None) if scope is not root else None
        if button is None:
            return False
        _press(button)                     # Netflix: the box appears (focused) only after this
        time.sleep(0.8)
        box = _search_box(snapshot(scope))
    if box is not None:
        log.info("Searching in %s %r", box.kind, box.name)
        try:
            box.wrapper().set_focus()
        except Exception:
            box.wrapper().click_input()
    send_keys("^a{BACKSPACE}")
    type_text(query)
    send_keys("{ENTER}")
    return True


def focused_is_password():
    try:
        return bool(IUIA().iuia.GetFocusedElement().CurrentIsPassword)
    except Exception:
        return False


# ---------- keyboard and mouse ----------

KEY_NAMES = {"enter": "{ENTER}", "return": "{ENTER}", "esc": "{ESC}", "escape": "{ESC}", "space": "{SPACE}",
             "tab": "{TAB}", "backspace": "{BACKSPACE}", "delete": "{DELETE}", "del": "{DELETE}", "up": "{UP}",
             "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}", "home": "{HOME}", "end": "{END}",
             "pagedown": "{PGDN}", "pgdn": "{PGDN}", "page down": "{PGDN}", "pageup": "{PGUP}", "pgup": "{PGUP}",
             "page up": "{PGUP}", "insert": "{INSERT}", **{f"f{i}": f"{{F{i}}}" for i in range(1, 13)}}
MODIFIERS = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+"}


def to_send_keys(keys):
    """"ctrl+shift+t" -> "^+t", "enter" -> "{ENTER}", "win+d" -> Windows-key down, d, up."""
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", keys.strip()) if p.strip()]
    if len(parts) == 1 and " " in parts[0] and parts[0] not in KEY_NAMES:
        parts = parts[0].split()          # "control t", as said
    if not parts:
        raise ValueError("no keys")
    *mods, key = parts
    win = "win" in mods or "windows" in mods
    unknown = [m for m in mods if m not in MODIFIERS and m not in ("win", "windows")]
    if unknown:
        raise ValueError(f"unknown key {unknown[0]!r}")
    prefix = "".join(MODIFIERS[m] for m in mods if m in MODIFIERS)
    if key in ("win", "windows"):
        main = "{VK_LWIN}"
    elif key in KEY_NAMES:
        main = KEY_NAMES[key]
    elif len(key) == 1:
        main = "{%s}" % key if key in "+^%~(){}[]" else key
    else:
        raise ValueError(f"unknown key {key!r}")
    sequence = prefix + main
    return "{VK_LWIN down}" + sequence + "{VK_LWIN up}" if win and key not in ("win", "windows") else sequence


def press(keys):
    send_keys(to_send_keys(keys))


def focus_text_box():
    """Make sure typing lands somewhere: if no text box has the keyboard, focus the first one on the page (or in
    the app). Returns its name, "" if one already had focus, None if there's none (typing goes where it goes)."""
    u = IUIA()
    edit, combo = u.known_control_types["Edit"], u.known_control_types["ComboBox"]
    try:
        if u.iuia.GetFocusedElement().CurrentControlType in (edit, combo):
            return ""
    except Exception:
        pass
    root = foreground_element()
    scope = page_element(root) or root
    box = next((c for c in snapshot(scope) if c.kind == "Edit"), None) if scope is not None else None
    if box is None:
        return None
    log.info("Focusing text box %r before typing", box.name)
    try:
        box.wrapper().set_focus()
    except Exception:
        box.wrapper().click_input()
    return box.name


def type_text(text):
    escaped = "".join("{%s}" % ch if ch in "+^%~(){}[]" else ch for ch in text)
    send_keys(escaped, with_spaces=True, with_newlines=False, pause=0.01)


def scroll(direction):
    if direction in ("top", "bottom"):
        # Home/End (not Ctrl+Home, which only moves the cursor when a text box has focus)
        send_keys("{ESC}" + ("{HOME}" if direction == "top" else "{END}"))
        return
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(user32.GetForegroundWindow(), ctypes.byref(rect))
    centre = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    mouse.scroll(coords=centre, wheel_dist=-5 if direction == "down" else 5)
