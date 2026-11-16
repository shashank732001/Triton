"""
Understands spoken commands without a fixed list of sentences.

Every word you might use is mapped to an idea (IDEAS: "louder" means volume + up, "quit" means
close, "thirty five" is a number). Each command (COMMANDS) says which ideas it needs and which
details it takes (a number, or an app/site/window named after its verb). A sentence goes to the
command whose ideas it covers best, so "turn the volume down", "volume down a bit", "make it
quieter" and "lower the sound" all reach the same command, and "set the volume to 35 percent"
reaches another one.

Adding a command later: write a handler (it returns the caption to show, or raises Failed with
what went wrong) and add one Command(...) line; new words go into IDEAS. The speech recogniser's
vocabulary is built from all of this automatically.
"""

import logging
from dataclasses import dataclass, field

import actions
import desktop
from apps import words_of

log = logging.getLogger("assistant")


class Failed(Exception):
    """Understood, but couldn't be done; the message is shown to the user."""


# ---------- words -> ideas ----------

IDEAS = {
    "volume": "volume sound audio speaker speakers",
    "turn": "turn",              # "turn it up" / "turn it down" means the volume
    "up": "up increase raise higher boost more",
    "down": "down decrease lower reduce less",
    "max": "max maximum full loudest highest",
    "min": "minimum lowest",
    "mute": "mute silence silent",
    "on": "on back",
    "off": "off",
    "by": "by",
    "little": "bit little slightly tad",
    "lot": "lot much lots",
    "open": "open launch start run load",
    "close": "close quit exit kill",
    "switch": "switch focus bring go",
    "minimize": "minimize minimise hide",
    "maximize": "maximize maximise fullscreen enlarge",
    "play": "play resume continue unpause",
    "pause": "pause stop hold",
    "media": "music song songs track playlist video playback",
    "next": "next skip forward",
    "previous": "previous last",
    "home": "home",
    "desktop": "desktop",
    "apps": "apps applications programs windows",
    "screen": "screen monitor display tv",
    "sleep": "sleep",
    "wake": "wake",
    "lock": "lock",
    "tab": "tab tabs",
    "all": "all everything",
}
# Words that carry several ideas at once
COMBINED = {
    "louder": {"volume", "up"}, "quieter": {"volume", "down"}, "softer": {"volume", "down"},
    "unmute": {"volume", "on"},
}
# Words that only make a sentence sound natural
FILLERS = set("the a an to my me please can could would you will i want and for of in at with now just set "
              "make put change percent per cent okay ok some its is too it this that current one show running "
              "computer pc laptop".split())
# Words that name "a window" without saying which: "close this window", "close the app"
GENERIC = set("app application program window windows tab it this that current one".split())

WORD_IDEAS = {}
for _idea, _words in IDEAS.items():
    for _w in _words.split():
        WORD_IDEAS.setdefault(_w, set()).add(_idea)
for _w, _ideas in COMBINED.items():
    WORD_IDEAS.setdefault(_w, set()).update(_ideas)

ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen").split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
NUMBER_WORDS = set(ONES) | set(TENS) | {"hundred", "half"}


def _numbers(words):
    """Replace spoken numbers with digits: ['thirty', 'five', 'percent'] -> ['35', 'percent']."""
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w == "two" and i + 1 < len(words) and words[i + 1] in NUMBER_WORDS:
            out.append("to")                   # "volume two thirty" is "volume to thirty" misheard
        elif w in TENS:
            n = 20 + 10 * TENS.index(w)
            if i + 1 < len(words) and words[i + 1] in ONES[1:10]:
                n += ONES.index(words[i + 1])
                i += 1
            out.append(str(n))
        elif w in ONES:
            n = ONES.index(w)
            if i + 1 < len(words) and words[i + 1] == "hundred":
                n *= 100
                i += 1
            out.append(str(n))
        elif w == "hundred":
            out.append("100")              # "a hundred"
        elif w == "half":
            out.append("50")
        else:
            out.append(w)
        i += 1
    return out


@dataclass
class Request:
    text: str
    words: list
    ideas: set = field(default_factory=set)
    number: int = None

    def after(self, idea):
        """The words after the last word for `idea` (minus fillers): what a verb is applied to."""
        last = max((i for i, w in enumerate(self.words) if idea in WORD_IDEAS.get(w, ())), default=None)
        if last is None:
            return ""
        rest = [w for w in self.words[last + 1:] if w not in FILLERS and w not in GENERIC and not w.isdigit()]
        while rest and rest[0] in WORD_IDEAS and len(rest) > 1:      # "bring up excel" -> "excel"
            rest.pop(0)
        return " ".join(rest)


def parse(text):
    words = _numbers(words_of(text.replace("%", " percent")))
    req = Request(text, words)
    for w in words:
        req.ideas |= WORD_IDEAS.get(w, set())
        if w.isdigit():
            req.number = int(w)                # the last one: "set it to 2... no, 30" means 30
    if req.number is not None:
        req.ideas.add("number")
    return req


# ---------- what each command does ----------

@dataclass
class Command:
    name: str
    needs: list                  # alternatives: any one of these idea sets is enough
    run: object                  # handler(commands, request) -> caption
    avoid: set = field(default_factory=set)   # ideas that mean it's a different command
    uses_number: bool = False
    target: str = None           # idea whose following words must name something ("open" + "chrome")
    example: str = ""
    shown_first: str = None      # caption shown BEFORE running (for commands that hide the screen)


class Commands:
    def __init__(self, catalog, monitor):
        self.catalog, self.monitor = catalog, monitor

    # ----- understanding -----

    def understand(self, text):
        """(command, request) for a sentence, or (None, request) if no command fits."""
        req = parse(text)
        best, best_score = None, 0.0
        for cmd in COMMANDS:
            if cmd.avoid & req.ideas:
                continue
            matched = [needs for needs in cmd.needs if needs <= req.ideas]
            if not matched:
                continue
            score = max(len(n) for n in matched)
            if cmd.uses_number and req.number is not None:
                score += 0.5
            if cmd.target:
                spoken = req.after(cmd.target).split()
                # "open" with nothing after it, or "go" + "home"/"to sleep" (other commands' words), isn't this one
                if not spoken or all(w in WORD_IDEAS for w in spoken):
                    continue
                score += 1
            if score > best_score:
                best, best_score = cmd, score
        return best, req

    def vocabulary(self, known=lambda w: True):
        """Every word worth recognising (ideas, fillers, numbers, app/site names) that `known` says can be heard."""
        own = {w for w in set(WORD_IDEAS) | FILLERS | GENERIC | NUMBER_WORDS if known(w)}
        return sorted(own | self.catalog.vocabulary(known))

    def run(self, cmd, req):
        log.info("Command '%s' for %r", cmd.name, req.text)
        return cmd.run(self, req)

    def windows(self, req, verb):
        """The windows a sentence talks about: the named app/site's, or the one in front.
        Returns (target or None, [windows], name to show)."""
        spoken = req.after(verb)
        if not spoken:
            front = desktop.foreground_window()
            if not front:
                raise Failed("No window in front")
            return None, [front], front.title.split(" - ")[-1] or front.exe
        target, wins = self.catalog.windows_for(spoken)
        name = target.name if target else spoken.title()
        if not wins:
            raise Failed(f"{name} isn't open")
        return target, wins, name


# ---------- handlers: (commands, request) -> caption ----------

def set_volume(c, req):
    level = 100 if "max" in req.ideas else 0 if "min" in req.ideas else max(0, min(100, req.number))
    return f"Volume {desktop.set_volume(level)}%"


def change_volume(c, req):
    step = (req.number if "by" in req.ideas and req.number is not None
            else 5 if "little" in req.ideas else 20 if "lot" in req.ideas else 10)
    sign = -1 if "down" in req.ideas else 1
    return f"Volume {desktop.set_volume(max(0, min(100, desktop.get_volume() + sign * step)))}%"


def mute(c, req):
    desktop.set_mute(True)
    return "Muted"


def unmute(c, req):
    desktop.set_mute(False)
    return f"Sound on, {desktop.get_volume()}%"


def play(c, req):
    return actions.play_music()


def pause(c, req):
    return actions.stop_music()


def next_track(c, req):
    return actions.skip_track(forward=True)


def previous_track(c, req):
    return actions.skip_track(forward=False)


def open_target(c, req):
    spoken = req.after("open")
    target = c.catalog.find(spoken)
    if not target:
        raise Failed(f"No app called “{spoken}”")
    if not actions.open_launcher_tile(target.name):
        c.catalog.launch(target)
    return f"Opening {target.name}"


def close(c, req):
    spoken = req.after("close")
    if "tab" in req.ideas and not spoken:
        desktop.press("ctrl", "w")
        return "Closed tab"
    if "all" in req.ideas:
        raise Failed("Say which app to close")      # never close everything by mistake
    target, wins, name = c.windows(req, "close")
    if target and target.kind == "site":
        for w in wins:                  # a site is a browser tab: close the tab, not the whole browser
            desktop.close_tab(w)
    else:
        for w in wins:
            w.close()
    return f"Closed {name}"


def switch_to(c, req):
    _, wins, name = c.windows(req, "switch")
    wins[0].focus()
    return f"Switched to {name}"


def minimize(c, req):
    if "all" in req.ideas:
        return show_desktop(c, req)
    _, wins, name = c.windows(req, "minimize")
    for w in wins:
        w.minimize()
    return f"Minimized {name}"


def maximize(c, req):
    _, wins, name = c.windows(req, "maximize")
    wins[0].maximize()
    wins[0].focus()
    return f"Maximized {name}"


def show_desktop(c, req):
    desktop.press("win", "d")
    return "Desktop"


def go_home(c, req):
    if not actions.go_home():
        desktop.press("win", "d")
    return "Home"


def show_open_apps(c, req):
    if not actions.show_open_apps():
        desktop.press("win", "tab")
    return "Open apps"


def screen_off(c, req):
    c.monitor.off()
    return "Screen off"


def screen_on(c, req):
    c.monitor.on()
    return "Screen on"


def lock(c, req):
    desktop.lock_screen()
    return "Locked"


S = frozenset
COMMANDS = [
    Command("set volume", [S({"volume", "number"}), S({"volume", "max"}), S({"volume", "min"})], set_volume,
            avoid={"by"}, uses_number=True, example="set the volume to 30 percent"),
    # "up to 50" sets the volume (above); "up by 20" changes it (here, as "set volume" avoids "by")
    Command("change volume", [S({"volume", "up"}), S({"volume", "down"}), S({"turn", "up"}), S({"turn", "down"})],
            change_volume,
            avoid={"mute"}, example="turn the volume down a bit"),
    Command("mute", [S({"mute"}), S({"volume", "off"})], mute, example="mute"),
    Command("unmute", [S({"volume", "on"})], unmute, avoid={"mute", "off"}, example="turn the sound back on"),
    Command("play", [S({"play"})], play, avoid={"next", "previous"}, example="play music"),
    Command("pause", [S({"pause"})], pause, example="pause the music"),
    Command("next track", [S({"next"})], next_track, avoid={"tab"}, example="next song"),
    Command("previous track", [S({"previous"})], previous_track, avoid={"tab"}, example="previous song"),
    Command("open", [S({"open"})], open_target, target="open", example="open youtube / excel / calculator"),
    Command("close", [S({"close"})], close, example="close chrome / close this window"),
    Command("switch to", [S({"switch"})], switch_to, target="switch", example="switch to chrome"),
    Command("minimize", [S({"minimize"})], minimize, example="minimize this / minimize everything"),
    Command("maximize", [S({"maximize"})], maximize, example="maximize chrome"),
    Command("desktop", [S({"desktop"})], show_desktop, example="show the desktop"),
    Command("home", [S({"home"})], go_home, example="go home"),
    Command("open apps", [S({"apps"})], show_open_apps, avoid={"close", "minimize"},
            example="show open apps"),
    Command("screen off", [S({"sleep"}), S({"screen", "off"})], screen_off, example="turn off the screen",
            shown_first="Screen off"),
    Command("screen on", [S({"wake"}), S({"screen", "on"})], screen_on, example="wake up"),
    Command("lock", [S({"lock"})], lock, example="lock the computer", shown_first="Locking"),
]
