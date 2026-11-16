"""
Everything you can name in a command: the apps installed on this PC (found automatically from
Windows' Start menu list, Store apps included), your websites and nicknames from shortcuts.json,
and the windows that are open right now. find() turns spoken words ("chrome", "the calculator",
"youtube music") into one of them, forgivingly: extra words, a missing vendor name or a slightly
misheard word still match.
"""

import difflib
import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass

import desktop

log = logging.getLogger("assistant")

CREATE_NO_WINDOW = 0x08000000
# Start-menu entries that aren't apps you'd ask for
JUNK_NAME = re.compile(r"uninstall|\bhelp\b|documentation|release notes|web ?site|\bfaq|getting started|"
                       r"user guide|read ?me|manual|license|what'?s new|on the web")
JUNK_TARGET = re.compile(r"^(https?:|\\\\)|\.(chm|html?|url|pdf|txt|md)$", re.I)
# Leading words people leave out: "Google Chrome" is just "chrome"
VENDORS = {"microsoft", "google", "mozilla", "adobe", "intel", "dell", "apple", "windows"}
MAX_NAME_WORDS = 4          # longer names are rarely spoken; they'd only bloat the speech vocabulary
FUZZY_CUTOFF = 0.8


def words_of(text):
    """'Intel® Graphics Command Center (x64) 6.8' -> ['intel', 'graphics', 'command', 'center']"""
    text = re.sub(r"\(.*?\)", " ", text.lower())
    return [w for w in re.findall(r"[a-z0-9]+", text) if not any(c.isdigit() for c in w) or w.isdigit()]


def _split(word, known):
    """'chatgpt' -> ['chat', 'gpt'] if the recogniser knows both halves but not the whole."""
    for i in range(3, len(word) - 1):
        if known(word[:i]) and known(word[i:]):
            return [word[:i], word[i:]]
    return None


@dataclass
class Target:
    kind: str            # "app" or "site"
    name: str            # how it's shown: "Google Chrome", "YouTube Music"
    words: tuple         # its spoken words, for finding its windows
    launch_id: str = ""  # app: Start-menu AppID;  site: URL


class Catalog:
    def __init__(self, shortcuts_file):
        self.sites, self.nicknames = {}, {}
        try:
            data = json.loads(shortcuts_file.read_text(encoding="utf-8"))
            self.sites = {" ".join(words_of(k)): v for k, v in data.get("sites", {}).items()}
            self.nicknames = {" ".join(words_of(k)): " ".join(words_of(v)) for k, v in data.get("nicknames", {}).items()}
        except (OSError, ValueError) as e:
            log.warning("No shortcuts from %s (%s)", shortcuts_file, e)
        self.apps = {}                # spoken key -> Target
        self.ready = threading.Event()
        threading.Thread(target=self._load_apps, daemon=True).start()

    # ---------- the installed apps ----------

    def _load_apps(self):
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                  "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                                 timeout=60, creationflags=CREATE_NO_WINDOW).stdout.decode("utf-8", "replace")
            entries = json.loads(out or "[]")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            log.warning("Couldn't list installed apps: %s", e)
            entries = []
        for e in entries if isinstance(entries, list) else [entries]:
            name, app_id = e.get("Name") or "", e.get("AppID") or ""
            words = words_of(name)
            if not words or len(words) > MAX_NAME_WORDS or JUNK_NAME.search(name.lower()) or JUNK_TARGET.search(app_id):
                continue
            target = Target("app", name, tuple(words), app_id)
            self.apps.setdefault(" ".join(words), target)
            if len(words) > 1 and words[0] in VENDORS:
                self.apps.setdefault(" ".join(words[1:]), target)
        log.info("Found %d installed apps", len(self.apps))
        self.ready.set()

    # ---------- spoken name -> app or site ----------

    def vocabulary(self, known=lambda w: True):
        """Every word a name can be said with, for the speech recogniser. `known(word)` says whether the
        recogniser can hear a word: a joined-up name it doesn't know gets a spoken form made of two
        halves it does, and names it can't hear at all are left out."""
        self.ready.wait(timeout=60)
        vocab = set()
        for pool in (self.sites, self.nicknames, self.apps):
            for key in list(pool):
                spoken = []
                for w in key.split():
                    parts = [w] if known(w) else _split(w, known)
                    if not parts:
                        break
                    spoken += parts
                else:
                    vocab.update(spoken)
                    alias = " ".join(spoken)
                    if alias != key:
                        self.nicknames.setdefault(alias, pool[key] if pool is self.nicknames else key)
        return vocab

    def find(self, spoken):
        """Best app or site for the spoken words, or None."""
        words = words_of(spoken)
        if not words:
            return None
        key = " ".join(words)
        key = self.nicknames.get(key, key)
        if key in self.sites:
            return self._site(key)
        if key in self.apps:
            return self.apps[key]
        # All spoken words inside one name ("visual studio" -> "visual studio code"); the shortest wins
        want = set(key.split())
        candidates = [(len(k.split()), k) for k in list(self.sites) + list(self.apps) if want <= set(k.split())]
        if candidates:
            k = min(candidates)[1]
            return self._site(k) if k in self.sites else self.apps[k]
        # Slightly misheard ("calculater"), or a nickname of one
        for pool in (self.nicknames, self.sites, self.apps):
            close = difflib.get_close_matches(key, list(pool), n=1, cutoff=FUZZY_CUTOFF)
            if close:
                return self.find(self.nicknames[close[0]]) if pool is self.nicknames else (
                    self._site(close[0]) if pool is self.sites else pool[close[0]])
        return None

    def _site(self, key):
        return Target("site", key.title().replace("Youtube", "YouTube"), tuple(key.split()), self.sites[key])

    @staticmethod
    def launch(target):
        if target.kind == "site":
            from actions import open_in_chrome
            open_in_chrome(target.launch_id)
        else:
            os.startfile("shell:AppsFolder\\" + target.launch_id)

    # ---------- spoken name -> open windows ----------

    def windows_for(self, spoken):
        """Open windows that belong to the spoken app/site: (target or None, [windows])."""
        target = self.find(spoken)
        names = {tuple(words_of(spoken))}
        exes = set(words_of(spoken))
        if target:
            names.add(target.words)
            exes |= set(target.words)
            if target.kind == "app" and target.launch_id.lower().endswith(".exe"):
                exes.add(re.split(r"[\\/]", target.launch_id)[-1][:-4].lower())
        matches = []
        for w in desktop.open_windows():
            title = set(words_of(w.title))
            by_title = any(set(n) <= title for n in names if n)
            by_exe = target is None or target.kind == "app"
            if by_title or (by_exe and w.exe and (w.exe in exes or w.exe == "".join(words_of(spoken)))):
                matches.append(w)
        return target, matches
