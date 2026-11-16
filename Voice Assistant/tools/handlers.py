"""
What each tool in tools/schema.py actually does on this PC.

Tools(catalog, monitor).run(call) carries out one {"tool", "args"} call and returns the caption for
the bubble ("Volume 30%"), or raises Failed with what went wrong. Built on the same helpers as the
rule commands (desktop.py, actions.py, the app catalog), plus tools/uia.py for on-screen control.
"""

import datetime
import logging
import subprocess
import time
import urllib.parse

import psutil

import actions
import desktop
from commands import Failed
from tools import uia
from tools.screenshare import PREVIEW as SCREEN_SHARE_PREVIEW

log = logging.getLogger("assistant")

# Captions shown BEFORE running, for tools that hide the screen (monitor off, lock, power)
SHOWN_FIRST = {("screen", "off"): "Screen off", ("lock", None): "Locking", ("power", "shutdown"): "Shutting down",
               ("power", "restart"): "Restarting", ("power", "sleep"): "Going to sleep"}
BROWSER_KEYS = {"new_tab": "ctrl+t", "close_tab": "ctrl+w", "next_tab": "ctrl+tab", "previous_tab": "ctrl+shift+tab",
                "back": "alt+left", "forward": "alt+right", "reload": "f5"}


def shown_first(call):
    args = call.get("args") or {}
    return SHOWN_FIRST.get((call["tool"], args.get("state") or args.get("action")))


def _preview(call):
    a, t = call.get("args") or {}, (call.get("args") or {}).get("target")
    match call["tool"]:
        case "open":
            return f"Opening {t}"
        case "close":
            return f"Closing {t}" if t else "Closing it"
        case "switch_to":
            return f"Switching to {t}"
        case "set_volume":
            return f"Volume {a.get('level')}%"
        case "change_volume":
            return "Turning it up" if a.get("direction") == "up" else "Turning it down"
        case "mute" | "unmute":
            return call["tool"].capitalize() + "d"
        case "media":
            return {"play": "Playing", "pause": "Pausing", "next": "Next one", "previous": "Going back"}.get(a.get("action"))
        case "web_search":
            return f"Searching for {a.get('query')}"
        case "youtube_search":
            return f"Searching YouTube for {a.get('query')}"
        case "browser":
            return a.get("action", "").replace("_", " ").capitalize() or None
        case "click":
            return f"Clicking {t}"
        case "type_text":
            return "Typing it"
        case "press_keys":
            return "Okay"
        case "scroll":
            return "Scrolling"
        case "window":
            return ("Minimizing " if a.get("action") == "minimize" else "Maximizing ") + (t or "it")
        case "show_desktop" | "go_home":
            return "Going home" if call["tool"] == "go_home" else "Showing the desktop"
        case "show_open_apps":
            return "Here are your open apps"
        case "screen_share":
            return SCREEN_SHARE_PREVIEW.get(a.get("action"))
    return None          # info (time, battery...): the answer is the result, so it's said afterwards


def say_first(calls):
    """What to say the moment a command is understood, before running it (so the voice doesn't lag the
    action): "Opening YouTube, then searching YouTube for lofi". None when the result itself is the answer."""
    parts = [_preview(c) for c in calls[:2]]
    if not parts or not all(parts):
        return None
    return ", then ".join([parts[0]] + [p[0].lower() + p[1:] for p in parts[1:]])


class Tools:
    def __init__(self, catalog, monitor, screen_share=None):
        self.catalog, self.monitor, self.screen_share = catalog, monitor, screen_share
        self.opened_at = 0.0         # when an app or site was last opened (a search right after waits for it)
        self.opened_name = ""

    def run(self, call):
        tool, args = call["tool"], call.get("args") or {}
        handler = getattr(self, "do_" + tool, None)
        if handler is None:
            raise Failed(f"Can't {tool.replace('_', ' ')} yet")
        log.info("Tool %s %s", tool, args)
        return handler(**args)

    # ----- sound and media -----

    def do_set_volume(self, level):
        return f"Volume {desktop.set_volume(max(0, min(100, int(level))))}%"

    def do_change_volume(self, direction, amount=10):
        step = int(amount) if direction == "up" else -int(amount)
        return f"Volume {desktop.set_volume(max(0, min(100, desktop.get_volume() + step)))}%"

    def do_mute(self):
        desktop.set_mute(True)
        return "Muted"

    def do_unmute(self):
        desktop.set_mute(False)
        return f"Sound on, {desktop.get_volume()}%"

    def do_media(self, action):
        if action == "play":
            return actions.play_music()
        if action == "pause":
            return actions.stop_music()
        return actions.skip_track(forward=action == "next")

    # ----- apps and windows -----

    def _windows(self, target):
        """(name, [windows]) for a spoken app/site, or the window in front when target is empty."""
        if not target:
            front = desktop.foreground_window()
            if not front:
                raise Failed("No window in front")
            return front.title.split(" - ")[-1] or front.exe, [front]
        found, wins = self.catalog.windows_for(target)
        name = found.name if found else target.title()
        if not wins:
            raise Failed(f"{name} isn't open")
        return name, wins

    def do_open(self, target):
        found = self.catalog.find(target)
        if not found:
            raise Failed(f"No app called “{target}”")
        if not actions.open_launcher_tile(found.name):
            self.catalog.launch(found)
        self.opened_at, self.opened_name = time.monotonic(), found.name
        return f"Opening {found.name}"

    def do_close(self, target=None):
        found = self.catalog.find(target) if target else None
        name, wins = self._windows(target)
        for w in wins:
            if found and found.kind == "site":
                desktop.close_tab(w)          # a site is a browser tab: close the tab, not the browser
            else:
                w.close()
        return f"Closed {name}"

    def do_switch_to(self, target):
        name, wins = self._windows(target)
        wins[0].focus()
        return f"Switched to {name}"

    def do_window(self, action, target=None):
        name, wins = self._windows(target)
        if action == "minimize":
            for w in wins:
                w.minimize()
            return f"Minimized {name}"
        wins[0].maximize()
        wins[0].focus()
        return f"Maximized {name}"

    def do_show_desktop(self):
        desktop.press("win", "d")
        return "Desktop"

    def do_go_home(self):
        if not actions.go_home():
            desktop.press("win", "d")
        return "Home"

    def do_show_open_apps(self):
        if not actions.show_open_apps():
            desktop.press("win", "tab")
        return "Open apps"

    # ----- browser and web -----

    def do_browser(self, action):
        uia.press(BROWSER_KEYS[action])
        return action.replace("_", " ").capitalize()

    def do_web_search(self, query):
        # On a site or app with its own search (Netflix, YouTube, Spotify...), search there: "search for
        # stranger things" on Netflix means Netflix's search, not Google. Right after "open netflix" the site is
        # still loading: give it a few seconds to show its search
        just_opened = time.monotonic() - self.opened_at < 10
        end = time.monotonic() + (8 if just_opened else 0)
        while True:
            # (and only in what was opened, not in whatever window was in front before it)
            in_front = not just_opened or self.opened_name.lower() in uia.foreground_title().lower()
            if in_front and uia.search_here(query):
                return f"Searching for “{query}”"
            if time.monotonic() >= end:
                break
            time.sleep(0.7)
        actions.open_in_chrome("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
        return f"Searching “{query}”"

    def do_youtube_search(self, query):
        actions.open_in_chrome("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
        return f"YouTube: “{query}”"

    # ----- on-screen control -----

    def do_click(self, target):
        # Right after "open netflix" the page (its profiles) takes a few seconds to appear: keep looking for up to 8 s
        end = time.monotonic() + (8 if time.monotonic() - self.opened_at < 10 else 0)
        while True:
            clicked = uia.click(target)
            if clicked or time.monotonic() >= end:
                break
            time.sleep(0.7)
        if not clicked:
            raise Failed(f"No “{target}” on screen")
        return f"Clicked {clicked}"

    def do_type_text(self, text):
        uia.focus_text_box()
        uia.type_text(text)
        return f"Typed “{text}”"

    def do_press_keys(self, keys):
        try:
            uia.press(keys)
        except ValueError as e:
            raise Failed(str(e)) from None
        return f"Pressed {keys}"

    def do_scroll(self, direction):
        uia.scroll(direction)
        return f"Scrolled {direction}"

    # ----- system -----

    def do_screen(self, state):
        if state == "off":
            self.monitor.off()
            return "Screen off"
        self.monitor.on()
        return "Screen on"

    def do_lock(self):
        desktop.lock_screen()
        return "Locked"

    def do_power(self, action):
        time.sleep(1)
        cmd = {"shutdown": ["shutdown", "/s", "/t", "0"], "restart": ["shutdown", "/r", "/t", "0"],
               "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]}[action]
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
        return {"shutdown": "Shutting down", "restart": "Restarting", "sleep": "Going to sleep"}[action]

    def do_screen_share(self, action):
        if self.screen_share is None:
            raise Failed("Screen sharing isn't set up here")
        return self.screen_share.run(action)

    def do_info(self, what):
        now = datetime.datetime.now()
        if what == "time":
            return now.strftime("It's %I:%M %p").replace(" 0", " ")    # "It's 9:39 PM" reads aloud better than 21:39
        if what == "date":
            return now.strftime("%A, %d %B")
        battery = psutil.sensors_battery()
        if battery is None:
            return "No battery (plugged in)"
        return f"Battery {battery.percent:.0f}%" + (", charging" if battery.power_plugged else "")

    # ----- control (the assistant handles these itself; here so every tool has a handler) -----

    def do_clarify(self, question):
        return question

    def do_escalate(self):
        raise Failed("That needs the laptop brain (not connected yet)")

    def do_cancel(self):
        return "Okay"
