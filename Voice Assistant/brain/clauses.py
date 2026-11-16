"""
Sentences the rules can't place but that need no model: multi-part commands, searches, "select X", "type X".

Why (the 2026-11-03 log): "Open Netflix and select Shashank" and "search Brooklyn 99 here" were heard almost right,
but the rules gave up on the whole sentence, the small model rewrote the words ("Brooklyn99" -> "bronley99",
YouTube instead of Netflix's own search), and the laptop timed out: 12-40 s, then "say that again".

Here each part ("open Netflix" / "select Shashank") is placed on its own: the fixed rules first, then the patterns
below. Search words and names are COPIED from what was heard (never re-typed by a model), and names are matched
against what's on screen, so "Shashant" clicks the "Shashank" profile.

    place(text, rules, screen, catalog) -> (calls, unclear part or None)
    fix_calls(calls, text, screen)      -> a model's calls with search/typing words copied from the sentence
    closest(name, names)                -> the on-screen name that sounds most like it, or None
"""

import difflib
import re

# A new part of the sentence starts at "and / then / and then" followed by one of these
VERBS = ("open", "launch", "start", "close", "search", "look", "find", "select", "choose", "pick", "click", "tap",
         "press", "type", "write", "play", "pause", "stop", "scroll", "go", "turn", "set", "mute", "unmute", "show",
         "switch", "put", "make", "skip", "bring", "pull", "fire")
SPLIT = re.compile(r"\s*,?\s+\b(?:and then|and|then|after that)\s+(?=(?:" + "|".join(VERBS) + r")\b)", re.I)
# Said around a command but not part of it
LEAD = re.compile(r"^(?:(?:hey |ok(?:ay)? )?triton,?\s+|please\s+|can you\s+|could you\s+|would you\s+|now\s+|"
                  r"just\s+|i want to\s+|i'd like to\s+|let's\s+)+", re.I)
TAIL = re.compile(r"(?:\s+(?:please|now|for me|right now|thanks|thank you))+[.!?]*$", re.I)
PLACE_WORDS = (r"(?:\s+(?:here|in here|over here|in that|in this|on this|on it|in it|there|on there|"
               # "search for X in the current (opened) chrome / tab / window" (said live, 2026-11-04): where, not what
               r"(?:in|on)\s+(?:the\s+|this\s+|my\s+)?(?:current(?:ly)?\s+)?(?:open(?:ed)?\s+)?"
               r"(?:chrome|browser|(?:\w+\s+)?(?:tab|window|page|screen))))+")      # "current core tab" = misheard Chrome
SITES = ("youtube", "netflix", "google", "spotify", "prime video", "amazon prime", "disney plus",
         "hotstar", "jiocinema", "twitch", "amazon")
SEARCH = re.compile(r"^(?:search|look up|look for|find)(?:\s+me)?(?:\s+for)?\s+(?P<q>.+?)"
                    r"(?:\s+(?:on|in|at)\s+(?:the\s+)?(?P<site>" + "|".join(SITES) + r"))?"
                    r"(?:" + PLACE_WORDS + r")?$", re.I)
PLAY_ON_YT = re.compile(r"^play\s+(?P<q>.+?)\s+on\s+youtube$", re.I)
# "play some lofi on youtube music" / "put on lofi in spotify": open that app, then search it there
PLAY_ON = re.compile(r"^(?:play|put on)\s+(?:some\s+|me\s+some\s+|me\s+)?(?P<q>.+?)\s+(?:on|in)\s+(?:the\s+)?"
                     r"(?P<site>youtube music|spotify|netflix|prime video|apple music|soundcloud)$", re.I)
# Where a typed text goes, said after it: "type LinkedIn here on the search bar"
TYPE_TAIL = re.compile(r"(?:\s+(?:here|there|in here))?(?:\s+(?:on|in|into)\s+(?:the\s+|this\s+)?"
                       r"(?:search\s*(?:bar|box|field)|box|field|bar|text\s*box))?$", re.I)
# "open Netflix on Chrome": the browser is where, not what (else it opened Chrome and maximized "netflix")
OPEN_WHERE = re.compile(r"\s+(?:on|in|using|with)\s+(?:the\s+|a\s+)?(?:chrome|google chrome|browser|edge|tv)$", re.I)
SELECT = re.compile(r"^(?:select|choose|pick|click(?:\s+on)?|tap(?:\s+on)?|press)\s+(?:the\s+|my\s+)?"
                    r"(?:profile\s+|user\s+|account\s+|button\s+|option\s+)?(?P<t>.+?)"
                    r"(?:\s+(?:profile|button|option|account|link|tab))?(?:" + PLACE_WORDS + r")?$", re.I)
TYPE = re.compile(r"^(?:type|write|enter)(?:\s+in|\s+out)?\s+(?P<x>.+)$", re.I)
OPEN = re.compile(r"^(?:open|launch|start|bring up|pull up|fire up|put on)\s+(?:up\s+)?(?:the\s+|our\s+|my\s+|a\s+)?"
                  r"(?P<t>.+?)$", re.I)
SCROLL = re.compile(r"^scroll\s+(?:it\s+|the page\s+)?(?P<d>up|down|to the top|to the bottom|top|bottom)\b", re.I)
SCROLL_DIR = {"up": "up", "down": "down", "to the top": "top", "top": "top", "to the bottom": "bottom",
              "bottom": "bottom"}


def clean(part):
    part = re.sub(r"[.!?]+$", "", part.strip())
    return TAIL.sub("", LEAD.sub("", part)).strip(" ,")


def split(text):
    return [p for p in (clean(x) for x in SPLIT.split(text)) if p]


def _letters(s):
    return re.sub(r"[^a-z]", "", s.lower())


def closest(name, names, cutoff=0.7):
    """The on-screen name that sounds most like `name`: letters only, so "Shashank73200" and "shashank" match, and
    a longer hearing ("Shashank Samit") still matches its start. None below the cutoff."""
    want = _letters(name)
    if not want or not names:
        return None
    best, score = None, 0.0
    for n in names:
        have = _letters(n)
        if not have:
            continue
        s = difflib.SequenceMatcher(None, want, have).ratio()
        # Heard more than the name ("shashank samit" for "Shashank"): compare the start
        if len(want) > len(have):
            s = max(s, difflib.SequenceMatcher(None, want[:len(have)], have).ratio() - 0.05)
        if s > score:
            best, score = n, s
    return best if score >= cutoff else None


def _digits(q):
    """Whisper writes "Brooklyn 9/9" or "Brooklyn99" for "Brooklyn Nine-Nine": search engines want "Brooklyn 99"."""
    q = re.sub(r"(\d)\s*/\s*(\d)", r"\1\2", q)
    return re.sub(r"(?<=[A-Za-z])(?=\d{2})", " ", q).strip(" ,.")


def _search(part, screen, catalog, opened):
    m = SEARCH.match(part)
    if not m:
        m2 = PLAY_ON_YT.match(part)
        return [{"tool": "youtube_search", "args": {"query": _digits(m2["q"])}}] if m2 else None
    q, site = _digits(m["q"]), (m["site"] or "").lower()
    if not q or q.lower() in ("it", "that", "this", "something", "for", "a", "the"):
        return None
    q = re.sub(r"^just\s+", "", q, flags=re.I)
    if not site and catalog is not None and q.lower() in SITES and catalog.find(q):
        # "search for Netflix" / "search just Netflix": go to Netflix, not a Google page about it
        return [{"tool": "open", "args": {"target": q.lower()}}]
    if site == "youtube" and not (screen and screen.shows("youtube")):
        return [{"tool": "youtube_search", "args": {"query": q}}]
    calls = []
    if site and site != "google" and not (screen and screen.shows(site)) and site != opened:
        if catalog is None or catalog.find(site) is None:
            return None
        calls.append({"tool": "open", "args": {"target": site}})
    # web_search uses the search box of the site in front (and waits for one just opened), else Google
    calls.append({"tool": "web_search", "args": {"query": q}})
    return calls


KEYS = re.compile(r"^(?:enter|return|escape|esc|space|spacebar|tab|backspace|delete|home|end|page up|page down|"
                  r"up|down|left|right|f\d{1,2}|(?:ctrl|control|alt|shift|windows)(?:\s*\+?\s*\w+)+)(?:\s+key)?$", re.I)


ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4,
            "5th": 4, "last": -1}
ORDINAL = re.compile(r"^(?:the\s+)?(?P<n>" + "|".join(ORDINALS) + r")(?:\s+(?:one|profile|option|video|result|item|"
                     r"link|button|thing|show|movie|song|account|user))?$", re.I)
# On-screen names that are never "the first one": menus, headers, page furniture
FURNITURE = re.compile(r"^(home|search|menu|account|notifications?|settings|help|help center|more|next|previous|back|"
                       r"manage profiles?|add profile|sign in|sign out|log ?in|log ?out|profile|my list|browse|"
                       r"close|skip|cancel|ok|done|edit|share|save|like|dislike|subscribe|filters?|tools|all|"
                       r"youtube|netflix|google|home page|go back)$", re.I)


def _select(part, screen):
    m = SELECT.match(part)
    if not m:
        return None
    t = m["t"].strip()
    if KEYS.match(t):                          # "press enter" is a key, not a button called "enter"
        key = re.sub(r"\s+key$", "", t.lower()).replace("control", "ctrl").replace(" + ", "+")
        return [{"tool": "press_keys", "args": {"keys": re.sub(r"\s+", "+", key) if " " in key else key}}]
    if not t or len(t.split()) > 4:
        return None
    o = ORDINAL.match(t)
    if o and screen:
        # "the first profile", "the second one", "the last video": count the things on screen, not their names
        items = [n for n in screen.names if not FURNITURE.match(n) and not n.endswith("?")
                 and n.lower() != screen.app.lower()]
        n = ORDINALS[o["n"].lower()]
        if items and (n == -1 or n < len(items)):
            return [{"tool": "click", "args": {"target": items[n]}}]
        return None
    on_screen = closest(t, screen.names) if screen else None
    return [{"tool": "click", "args": {"target": on_screen or t}}]


def place(text, rules, screen=None, catalog=None):
    """(calls, unclear) for a sentence of one or more parts. calls is None when nothing could be placed; unclear is
    the first part that couldn't be (the rest is still done, and that part asked about)."""
    parts = split(text)
    if not parts:
        return None, None
    calls, unclear, opened = [], None, ""
    for part in parts:
        part = OPEN_WHERE.sub("", part) if OPEN.match(part) else part
        got = rules(part)
        if got is None and (m := OPEN.match(part)) and catalog is not None and catalog.find(m["t"]):
            got = [{"tool": "open", "args": {"target": m["t"]}}]       # "open our Netflix"
        if got is None and (m := PLAY_ON.match(part)) and catalog is not None and catalog.find(m["site"]):
            site = m["site"].lower()
            got = ([] if screen and screen.shows(site) else [{"tool": "open", "args": {"target": site}}]) + \
                  [{"tool": "web_search", "args": {"query": _digits(m["q"])}}]
        if got is None:
            got = _search(part, screen, catalog, opened)
        if got is None and (m := TYPE.match(part)):
            got = [{"tool": "type_text", "args": {"text": TYPE_TAIL.sub("", m["x"]).strip() or m["x"]}}]
        if got is None and (m := SCROLL.match(part)):
            got = [{"tool": "scroll", "args": {"direction": SCROLL_DIR[m["d"].lower()]}}]
        if got is None:
            got = _select(part, screen)
        if got is None:
            unclear = unclear or part
            continue
        for c in got:
            if c["tool"] == "open":
                opened = str(c["args"].get("target", "")).lower()
        calls += got
    return (calls or None), unclear


def fix_calls(calls, text, screen=None):
    """A model's calls with words copied from the sentence instead of re-typed ("bronley99" -> "Brooklyn 99"),
    click targets spelled as on screen, and YouTube searches that weren't asked for turned into a search here."""
    if not calls:
        return calls
    parts = split(text)
    queries = [_digits(m["q"]) for p in parts if (m := SEARCH.match(p))]
    typed = [m["x"] for p in parts if (m := TYPE.match(p))]
    said_youtube = "youtube" in text.lower()
    out = []
    for c in calls:
        c = {"tool": c["tool"], "args": dict(c.get("args") or {})}
        if c["tool"] in ("web_search", "youtube_search") and queries:
            c["args"]["query"] = queries.pop(0)
            if c["tool"] == "youtube_search" and not said_youtube and not (screen and screen.shows("youtube")):
                c["tool"] = "web_search"
        elif c["tool"] == "type_text" and typed and str(c["args"].get("text", "")).lower() not in text.lower():
            c["args"]["text"] = typed.pop(0)
        elif c["tool"] == "click" and screen and screen.names:
            c["args"]["target"] = closest(str(c["args"].get("target", "")), screen.names) or c["args"].get("target", "")
        out.append(c)
    return out
