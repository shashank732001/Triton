r"""
Triton-Mini v3 additions to gen_converse.py (python gen_converse.py --v3; README 5r). All code-written, so the labels
are exact. Each group puts near-identical sentences with different answers side by side, so the 0.6B learns
exactly where the line is (the N95 log of 2026-11-06 and the 1000-item test, README 5q, showed where it isn't):

  contrast-close    "close youtube" -> close it; "close this window" -> close {}; "close that thing" -> ask which;
                    "closet", "clothes it" -> ask again
  thanks            "thanks buddy that was quick", "it worked" (after a command, often with a screen) -> talk back,
                    never press play; "thanks, now pause it" -> pause
  results           "play the first one" on a page of results -> click that result; "keep playing" on a video ->
                    play; never click the site's own name
  clear-search      "clear the search bar" with "Clear search query" / "Clear input" on screen -> click it
  not-for-me        "do you want the browser", "shall we watch netflix later" (people talking) -> ask again
  salad             "banana purple elevator", "open your tooth heat" -> ask again (no escalate, no open)
  polite            long, polite commands ("could you start google photos for me whenever you get a chance")
                    -> the same calls as the short form (v2 said "ask again" to many)
  multi             two commands joined with and / then -> both calls in order (v2: 2 of 16)
Also used by gen_converse.py: says_it_acts(), to drop chat replies that claim an action ("I'll close the call now").
Run it alone for a look at a few rows of each: python gen_v3.py
"""

import json
import random
import re

from gen_data import APPS, SITES, call, stt_style

TARGETS = {**APPS, **SITES}
VERBS = {"open", "close", "play", "pause", "turn", "set", "mute", "unmute", "search", "go", "switch", "minimize",
         "maximize", "show", "skip", "scroll", "press", "launch", "start", "bring", "stop", "find", "increase",
         "decrease", "lower", "raise", "make", "reload", "refresh", "resume", "put", "volume", "next", "previous",
         "shut", "exit", "quit", "look", "take", "fire", "pull", "load"}
SIMPLE_TOOLS = {"open", "close", "switch_to", "window", "media", "change_volume", "set_volume", "mute", "unmute",
                "show_desktop", "go_home", "browser", "web_search", "youtube_search", "scroll", "screen", "info",
                "show_open_apps"}
CLAIM = re.compile(r"\b(i'?ll|i will|let me|i'?m going to|i'?m gonna|i'?m)\s+(\w+\s+){0,2}?(open|clos|play|paus|turn|"
                   r"switch|search|click|skip|mut|start|launch|shut|type|lock|scroll)", re.I)
CLAIM_START = re.compile(r"^\W*(sure|okay|ok|got it|alright|done)?\W*(opening|closing|playing|pausing|switching|"
                         r"searching|launching|turning|skipping|muting|clicking)\b", re.I)


def says_it_acts(text):
    """A spoken reply that claims to do something ("Got it, I'll close the call now"): only calls may act."""
    return bool(CLAIM.search(text) or CLAIM_START.search(text))


def user(text, screen=None):
    line = ""
    if screen:
        title, names = screen
        line = f"[screen: {title}" + (f" | on screen: {', '.join(names)}" if names else "") + "] "
    return {"role": "user", "content": line + text}


def said(text):
    return {"role": "assistant", "content": json.dumps({"say": text}, separators=(",", ":"))}


def row(messages, reply, kind):
    return {"messages": messages, "reply": reply, "kind": kind}


def maybe(rng, p, screens):
    return rng.choice(screens) if rng.random() < p else None


# ---------- close: act, act on this window, ask which, or ask again ----------

CLOSE_VERBS = ["close", "close", "shut", "exit", "quit", "close down", "get rid of"]
THIS_WINDOW = ["close this window", "close this", "close it", "close the current window", "shut this window",
               "close this app", "close what's in front", "exit this", "close the window"]
VAGUE = ["close that thing", "close whatever's running", "close the stuff", "shut something", "close all that",
         "close the other one", "could you take care of closing that", "get rid of that thing", "close those",
         "can you close something", "shut that thing down"]
WHICH = ["Which one should I close?", "Close which one?", "Which app should I close?", "Sure, which one?"]
CLOSE_SLIPS = ["closet", "clothes", "closed", "closer", "closing", "clothes it", "close call", "closely", "clothes line",
               "closet door", "close enough", "closed up", "the closet", "clothes on"]


def contrast_close(rng, screens, n=1600):
    rows = []
    for _ in range(n):
        k = rng.random()
        scr = maybe(rng, 0.5, screens)
        if k < 0.35:
            spoken = rng.choice(TARGETS[rng.choice(list(TARGETS))])
            text = f"{rng.choice(['', 'hey ', 'please ', 'can you '])}{rng.choice(CLOSE_VERBS)} {spoken}"
            rows.append(row([user(stt_style(text, rng), scr)], {"calls": [call("close", target=spoken)]},
                            "contrast-close"))
        elif k < 0.55:
            rows.append(row([user(stt_style(rng.choice(THIS_WINDOW), rng), scr)], {"calls": [call("close")]},
                            "contrast-close"))
        elif k < 0.75:
            rows.append(row([user(stt_style(rng.choice(VAGUE), rng), scr)], {"say": rng.choice(WHICH)},
                            "contrast-close"))
        else:
            slip = rng.choice(CLOSE_SLIPS)
            rows.append(row([user(rng.choice([slip, slip.capitalize()]), scr)], {"repeat": True}, "contrast-close"))
    return rows


# ---------- thanks after a command: talk, never act ----------

THANKS = ["thanks buddy that was quick", "thank you", "thanks", "nice one", "it worked", "that worked", "perfect thanks",
          "great job", "cool thanks", "awesome that worked", "thanks buddy", "cheers", "good job triton",
          "thanks a lot", "you're quick", "well done", "brilliant thanks", "that was fast", "lovely thank you",
          "nice that's the one", "yes that's it thanks", "oh nice it's playing", "great that's perfect", "wow quick",
          "thank you so much", "good stuff", "nice work", "thanks mate", "perfect", "sweet thanks"]
THANKS_SAY = ["Anytime!", "Happy to help.", "No problem!", "You're welcome.", "Glad that worked.", "Any time, enjoy!",
              "My pleasure.", "Always happy to help.", "Glad I could help!", "Enjoy!"]
DONE = [("open youtube", "Opening YouTube."), ("play some lofi on youtube", "Searching YouTube for lofi."),
        ("turn it up", "Turning it up."), ("open netflix", "Opening Netflix."), ("pause", "Paused."),
        ("next song", "Skipping."), ("mute", "Muted."), ("go home", "Going home."), ("open spotify", "Opening Spotify.")]
THEN = [("pause it", call("media", action="pause")), ("turn it down", call("change_volume", direction="down")),
        ("go home", call("go_home")), ("skip this one", call("media", action="next")), ("mute it", call("mute")),
        ("close it", call("close")), ("open netflix", call("open", target="netflix"))]


def thanks(rng, screens, n=1500):
    rows = []
    for _ in range(n):
        cmd, reply = rng.choice(DONE)
        hist = [user(cmd), said(reply)] if rng.random() < 0.6 else []
        scr = maybe(rng, 0.6, screens)
        if rng.random() < 0.8:
            rows.append(row(hist + [user(stt_style(rng.choice(THANKS), rng), scr)], {"say": rng.choice(THANKS_SAY)},
                            "thanks"))
        else:
            then, c = rng.choice(THEN)
            text = (f"{rng.choice(['thanks', 'thank you', 'nice', 'cool', 'great'])} "
                    f"{rng.choice(['now ', 'and ', ''])}{then}")
            rows.append(row(hist + [user(stt_style(text, rng), scr)], {"calls": [c]}, "thanks"))
    return rows


# ---------- a page of results: click the result, not the site ----------

ORD = [("first", 0), ("second", 1), ("third", 2), ("top", 0), ("1st", 0), ("2nd", 1)]
RESULT_FORMS = ["play the {w} one", "play the {w} video", "open the {w} video", "click the {w} result",
                "the {w} one please", "put on the {w} one", "select the {w} video", "watch the {w} one",
                "can you play the {w} one", "click on the {w} video", "open the {w} result", "play the {w} result"]
KEEP_PLAYING = ["keep playing", "resume", "continue playing", "play it again", "carry on playing", "unpause",
                "start it again", "resume the video", "play"]
RESULT_PAGES = [("(3) {q} - YouTube - Triton Browser", ["Guide", "YouTube Premium home", "Search", "Clear search query",
                                                        "Filters"]),
                ("{q} - YouTube - Google Chrome", ["Home", "Search", "Filters"])]


def results(rng, pools, n=1500):
    rows = []
    for _ in range(n):
        titles = [q.title() for q in rng.sample(pools["youtube_queries"], 3)]
        page, chrome = rng.choice(RESULT_PAGES)
        q = rng.choice(pools["youtube_queries"])
        if rng.random() < 0.8:
            word, i = ("last", 2) if rng.random() < 0.15 else rng.choice(ORD)
            hist = [user(f"search {q}"), said(f"Searching for {q}.")] if rng.random() < 0.5 else []
            text = rng.choice(RESULT_FORMS).format(w=word)
            rows.append(row(hist + [user(stt_style(text, rng), (page.format(q=q), chrome + titles + ["Shorts"]))],
                            {"calls": [call("click", target=titles[i])]}, "results"))
        else:
            rows.append(row([user(stt_style(rng.choice(KEEP_PLAYING), rng),
                                  (f"{titles[0]} - YouTube - Triton Browser",
                                   ["Play", "Next", "Mute", "Settings", "Full screen", "Subscribe"]))],
                            {"calls": [call("media", action="play")]}, "results"))
    return rows


# ---------- clear the search box: the button that's on screen ----------

CLEAR_FORMS = ["clear the search bar", "clear the search", "empty the search box", "delete what's in the search",
               "clear that", "clear the search bar on the screen", "remove the search text", "clear the search field",
               "wipe the search box", "clear search", "can you clear the search bar", "clear the text in the search"]
CLEAR_SCREENS = [("(25) {q} - YouTube - Triton Browser", ["Guide", "YouTube Premium home", "Search",
                                                          "Clear search query", "Search with your voice", "Create",
                                                          "Notifications", "Home", "Shorts"], "Clear search query"),
                 ("Netflix - Google Chrome", ["Netflix", "Home", "Shows", "Movies", "New & Popular", "My Netflix",
                                              "Titles, people, genres", "Clear input", "Notifications"], "Clear input"),
                 ("{q} - Google Search - Triton Browser", ["Google", "Search", "Clear", "Search by voice",
                                                           "Search by image", "Images", "Videos"], "Clear"),
                 ("Spotify", ["Home", "Search", "Clear search field", "Your Library"], "Clear search field")]


def clear_search(rng, pools, n=600):
    rows = []
    for _ in range(n):
        title, names, button = rng.choice(CLEAR_SCREENS)
        title = title.format(q=rng.choice(pools["youtube_queries"]))
        rows.append(row([user(stt_style(rng.choice(CLEAR_FORMS), rng), (title, names))],
                        {"calls": [call("click", target=button)]}, "clear-search"))
    return rows


# ---------- people talking to each other, with command words in it ----------

LEADS = ["do you want", "did you want", "shall we", "should i", "did you", "have you", "are you going to",
         "would you like", "do we need", "can i", "did he", "she wants to", "we should", "they will", "you could",
         "mum do you want", "dad did you", "honey shall we", "do you guys want", "are we gonna"]
OBJECTS = ["the browser", "open the window", "close the door", "watch netflix later", "play outside",
           "turn off the lights", "some tea", "search for your keys", "the remote", "switch seats", "pause for a bit",
           "go home now", "the volume thing", "start dinner", "open the gift", "lock the car", "the next one",
           "mute your phone", "close the shop early", "skip breakfast", "turn the heating up", "play cards",
           "search the garage"]


def not_for_me(rng, screens, n=1200):
    rows = []
    for _ in range(n):
        text = f"{rng.choice(LEADS)} {rng.choice(OBJECTS)}"
        if rng.random() < 0.3:
            text = text.capitalize() + rng.choice(["", "?"])
        rows.append(row([user(stt_style(text, rng), maybe(rng, 0.6, screens))], {"repeat": True}, "not-for-me"))
    return rows


# ---------- word salad and "open <nonsense>" ----------

NOUNS = ["banana", "purple", "elevator", "tooth", "heat", "riding", "deposit", "camilla", "field", "ferrosion",
         "window", "pillow", "seven", "rocket", "garden", "yellow", "monkey", "river", "candle", "biscuit", "thunder",
         "pocket", "silver", "ladder", "kettle", "orange", "tiger", "blanket", "mirror", "carpet", "dragon", "lemon",
         "pencil", "bottle", "jacket", "planet", "basket", "zebra", "piano", "marble", "glove", "cactus", "ocean",
         "button", "turtle", "puzzle", "hammer", "violet", "noodle", "feather", "m2", "qs", "life", "that", "far",
         "how's", "this", "your", "it's", "the", "of", "so", "very"]


def salad(rng, screens, n=1500):
    rows = []
    for _ in range(n):
        words = rng.sample(NOUNS, rng.randint(2, 4))
        if rng.random() < 0.35:
            words = [rng.choice(["open", "play", "close", "search", "click"])] + words[:2]
        if " ".join(words[1:]) in TARGETS:
            continue
        text = " ".join(words)
        if rng.random() < 0.4:
            text = text.capitalize()
        rows.append(row([user(text, maybe(rng, 0.5, screens))], {"repeat": True}, "salad"))
    return rows


# ---------- polite and long commands, and two at once ----------

PREFIX = ["could you please ", "hey triton can you ", "can you just ", "i want you to ", "please ",
          "hey can you ", "quickly ", "um can you ", "would you ", "can you go ahead and ", "do me a favour and ",
          "i need you to ", "hey ", "okay so "]
SUFFIX = ["", " for me", " please", " whenever you get a chance", " would you", " thanks", " right now", " if you can",
          " for me please", " real quick", " when you can", ", cheers"]


def simple(r):
    """A one-turn command, no screen, starting with a verb, only calls that don't depend on the screen."""
    if len(r["messages"]) != 1 or "calls" not in r["reply"]:
        return False
    text = r["messages"][0]["content"]
    first = text.split()[0].lower() if text.split() else ""
    return (not text.startswith("[") and first in VERBS and len(text.split()) <= 8
            and all(c["tool"] in SIMPLE_TOOLS for c in r["reply"]["calls"]))


def polite(rng, commands, n=3000):
    pool = [r for r in commands if simple(r)]
    rows = []
    for r in rng.sample(pool, min(n, len(pool))):
        text = r["messages"][0]["content"].rstrip(".?!")
        text = rng.choice(PREFIX) + text[0].lower() + text[1:] + rng.choice(SUFFIX)
        rows.append(row([user(stt_style(text, rng))], r["reply"], "polite"))
    return rows


JOIN = [" and ", " and then ", " then ", ", then ", " and also ", " after that "]


def multi(rng, commands, n=2500):
    pool = [r for r in commands if simple(r) and len(r["reply"]["calls"]) == 1]
    rows = []
    for _ in range(n):
        a, b = rng.sample(pool, 2)
        if a["reply"]["calls"][0]["tool"] == b["reply"]["calls"][0]["tool"]:
            continue
        ta, tb = (x["messages"][0]["content"].rstrip(".?!") for x in (a, b))
        text = ta + rng.choice(JOIN) + tb[0].lower() + tb[1:]
        if rng.random() < 0.3:
            text = rng.choice(PREFIX) + text[0].lower() + text[1:]
        rows.append(row([user(stt_style(text, rng))], {"calls": a["reply"]["calls"] + b["reply"]["calls"]}, "multi"))
    return rows


def all_rows(rng, pools, screens, commands):
    return (contrast_close(rng, screens) + thanks(rng, screens) + results(rng, pools) + clear_search(rng, pools)
            + not_for_me(rng, screens) + salad(rng, screens) + polite(rng, commands) + multi(rng, commands))


if __name__ == "__main__":       # a look at a few of each, without the cluster
    rng = random.Random(3)
    pools = {"youtube_queries": ["lofi beats", "real madrid highlights", "pasta recipes", "rain sounds", "news today"]}
    screens = [("Netflix - Google Chrome", ["Home", "Shows", "Search"]), ("Triton Launcher", ["Hey Triton", "YouTube"])]
    cmds = [row([user(t)], {"calls": c}, "command") for t, c in [
        ("open spotify", [call("open", target="spotify")]), ("mute the sound", [call("mute")]),
        ("turn the volume up", [call("change_volume", direction="up")]),
        ("pause the video", [call("media", action="pause")]), ("go home", [call("go_home")]),
        ("open a new tab", [call("browser", action="new_tab")])]]
    groups = {}
    for r in all_rows(rng, pools, screens, cmds):
        groups.setdefault(r["kind"], []).append(r)
    for k, rs in groups.items():
        print(f"== {k}: {len(rs)}")
        for r in rs[:4]:
            print("  ", r["messages"][-1]["content"][:95], "->", json.dumps(r["reply"])[:90])
    for t in ["Got it, I'll close the call now.", "Opening YouTube.", "Why did the scarecrow win an award?",
              "I'm Triton, your TV buddy.", "Sure thing! I'll close VLC for you now.", "I'm partial to comedies."]:
        print(f"  claims an action: {says_it_acts(t)!s:5} {t}")
