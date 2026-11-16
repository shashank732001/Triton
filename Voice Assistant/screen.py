"""
What's on screen when you give a command: the window in front and the names of the things you can click there
(Windows UI Automation, tools/uia.py). The brain needs it to know where "here" is ("search Brooklyn 99 here" on
Netflix is Netflix's search, not YouTube) and which "Shashank" you mean (the Netflix profile, spelled right).

look() takes ~0.1-0.5 s. The assistant calls it when listening starts, while you're still talking: the mic's audio
waits in its queue meanwhile, so nothing is lost and the time hides behind your sentence. (It runs in the
assistant's own thread on purpose: UI Automation's COM objects don't like being shared between threads.)
"""

import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("assistant")

MAX_NAMES = 12               # sent to the laptop's brain: each name is text it must read first (measured on the
                             # laptop's 4B: 36 names 14.9 s, 12 names 10.0 s, none 9.2 s)
MAX_CONTEXT_NAME = 30        # long names (search results, video titles) are left out of that line
MAX_NAME_LEN = 40
BROWSERS = ("google chrome", "microsoft edge", "mozilla firefox", "brave", "chrome")
# Browser and window furniture: never what you mean by "select X"
SKIP = re.compile(r"^(minimi[sz]e|maximi[sz]e|restore|close|back|forward|reload|new tab|tab search|bookmark.*|"
                  r"view site information|extensions|side panel|chrome|google chrome|address and search bar|"
                  r"customi[sz]e and control.*|you|search tabs)$", re.I)


@dataclass
class Screen:
    title: str = ""                               # window in front, e.g. "Netflix - Google Chrome"
    names: list = field(default_factory=list)     # visible clickable names, page first

    @property
    def app(self):
        """'Netflix' from 'Netflix - Google Chrome', 'Notepad' from 'notes.txt - Notepad'."""
        parts = [p.strip() for p in re.split(r"\s[-–—|]\s", self.title) if p.strip()]
        if len(parts) >= 2 and parts[-1].lower() in BROWSERS:
            return parts[-2]
        return parts[-1] if parts else ""

    def shows(self, site):
        return bool(site) and site.lower() in self.title.lower()

    def context(self):
        """One line for the laptop's brain: '[screen: Netflix - Google Chrome | on screen: Shashank, Kids, …]'."""
        if not self.title:
            return ""
        names = ", ".join([n for n in self.names if len(n) <= MAX_CONTEXT_NAME][:MAX_NAMES])
        return f"[screen: {self.title}" + (f" | on screen: {names}" if names else "") + "]"


def look():
    """The window in front and its clickable names. Never raises: an empty Screen if UI Automation fails."""
    t0 = time.perf_counter()
    try:
        from tools import uia
        title = uia.foreground_title()
        root = uia.foreground_element()
        page = uia.page_element(root) if root is not None else None
        controls = uia.snapshot(page) if page is not None else []
        if root is not None and len(controls) < 5:
            controls += uia.snapshot(root)
    except Exception as e:                  # a window closing mid-look, COM hiccups: just no context
        log.info("Couldn't look at the screen (%s)", e)
        return Screen()
    names, seen = [], set()
    for c in controls:
        n = " ".join(c.name.split())
        key = n.lower()
        if not n or len(n) > MAX_NAME_LEN or key in seen or SKIP.match(n):
            continue
        seen.add(key)
        names.append(n)
    s = Screen(title, names)
    log.info("Screen: %r, %d names (%.0f ms)", s.app, len(names), (time.perf_counter() - t0) * 1000)
    return s
