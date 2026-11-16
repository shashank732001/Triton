"""
Tier 1 of the router: today's rule parser (commands.py), answering in tool calls.

to_calls(cmd, req) turns a matched Command into the same {"tool", "args"} calls the model produces.
confident(...) says whether to trust it or hand the sentence to the model instead: every word must
be one the rules know, and a named app must really exist. "kill the sound" maps to close("sound")
by the rules; no app is called "sound", so the model gets it.
"""

from apps import words_of
from commands import FILLERS, GENERIC, NUMBER_WORDS, WORD_IDEAS


def to_calls(cmd, req):
    def c(tool, **args):
        return [{"tool": tool, "args": args}]

    ideas, n = req.ideas, req.number
    match cmd.name:
        case "set volume":
            return c("set_volume", level=100 if "max" in ideas else 0 if "min" in ideas else n)
        case "change volume":
            d = "down" if "down" in ideas else "up"
            return c("change_volume", direction=d, amount=n) if "by" in ideas and n is not None \
                else c("change_volume", direction=d)
        case "mute" | "unmute" | "lock":
            return c(cmd.name)
        case "play" | "pause":
            return c("media", action=cmd.name)
        case "next track" | "previous track":
            return c("media", action=cmd.name.split()[0])
        case "open":
            return c("open", target=req.after("open"))
        case "close":
            spoken = req.after("close")
            if "tab" in ideas and not spoken:
                return c("browser", action="close_tab")
            return c("close", target=spoken) if spoken else c("close")
        case "switch to":
            return c("switch_to", target=req.after("switch"))
        case "minimize" | "maximize":
            if cmd.name == "minimize" and "all" in ideas:
                return c("show_desktop")
            spoken = req.after(cmd.name)
            return c("window", action=cmd.name, target=spoken) if spoken else c("window", action=cmd.name)
        case "desktop":
            return c("show_desktop")
        case "home":
            return c("go_home")
        case "open apps":
            return c("show_open_apps")
        case "screen off" | "screen on":
            return c("screen", state=cmd.name.split()[1])
    return []


def known_words(catalog):
    words = set(WORD_IDEAS) | FILLERS | GENERIC | NUMBER_WORDS
    for pool in (catalog.sites, catalog.nicknames, catalog.apps):
        for key in list(pool):
            words.update(key.split())
    return words


# "play"/"pause" alone is a media key; "play lofi on youtube" is a search the rules can't do (they turned it
# into play_music, which opens YouTube Music). Only these words may come with a media command.
MEDIA_ONLY = {"play", "pause", "resume", "stop", "music", "song", "video", "the", "it", "this", "that", "again",
              "track", "next", "previous", "skip", "back", "go", "one", "last"}


def confident(calls, req, catalog, known):
    """Trust the rules for this sentence?"""
    if not calls:
        return False
    words = words_of(req.text)
    if any(w not in known and not w.isdigit() for w in words):
        return False
    if any(c["tool"] == "media" for c in calls) and any(w not in MEDIA_ONLY | FILLERS for w in words):
        return False
    if "youtube" in words and not all(c["tool"] in ("open", "youtube_search") for c in calls):
        return False
    for call in calls:
        target = call["args"].get("target")
        if target and call["tool"] in ("open", "switch_to", "close", "window") and not catalog.find(target):
            return False
        if call["tool"] == "set_volume" and call["args"].get("level") is None:
            return False
    return True
