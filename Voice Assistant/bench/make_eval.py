r"""
Builds bench/eval/eval.jsonl: spoken requests with the tool calls they should produce.

    .venv\Scripts\python.exe bench\make_eval.py

Each line: {"id", "category", "text", "expected": [calls], "also_ok": [[calls], ...]}. An argument
value may be a list of acceptable answers ("chrome" or "google chrome"). Fixed seed, so the file is
the same every time; edit the lists here rather than the file. Real requests from the trace store
join it later (M3).
"""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.schema import BY_NAME  # noqa: E402

OUT = Path(__file__).parent / "eval" / "eval.jsonl"
rng = random.Random(7)

APPS = {
    "chrome": ["chrome", "google chrome"], "brave": ["brave"], "edge": ["edge", "microsoft edge"],
    "word": ["word", "microsoft word"], "excel": ["excel"], "notepad": ["notepad"],
    "calculator": ["calculator"], "files": ["files", "file explorer", "explorer"], "settings": ["settings"],
    "spotify": ["spotify"], "vlc": ["vlc"], "paint": ["paint"], "terminal": ["terminal", "windows terminal"],
}
SITES = {
    "youtube": ["youtube"], "gmail": ["gmail"], "netflix": ["netflix"], "whatsapp": ["whatsapp"],
    "google maps": ["google maps", "maps"], "prime video": ["prime video"], "youtube music": ["youtube music"],
}
TARGETS = {**APPS, **SITES}
QUERIES = ["lofi hip hop", "weather in berlin", "pasta recipes", "champions league highlights",
           "how to tie a tie", "taylor swift", "news today", "python tutorial", "rain sounds",
           "best movies 2026", "cricket score", "mr bean"]

items = []


def add(category, text, expected, also_ok=()):
    for calls in [expected, *also_ok]:
        for c in calls:
            assert c["tool"] in BY_NAME, c
    items.append({"category": category, "text": text, "expected": expected, "also_ok": list(also_ok)})


def call(tool, **args):
    return {"tool": tool, "args": args}


def some(pool, k):
    return rng.sample(list(pool), k)


# ---------- volume ----------
for n, t in zip(some(range(10, 95, 5), 6), ["set the volume to {n}", "volume {n} percent", "put the sound at {n}",
                                             "make the volume {n}%", "change the volume to {n} please",
                                             "can you set volume at {n}"]):
    add("volume", t.format(n=n), [call("set_volume", level=n)])
for t in ["volume up", "turn it up", "louder please", "increase the volume", "crank it up a bit", "i can't hear it"]:
    add("volume", t, [call("change_volume", direction="up")])
for t in ["turn the volume down", "quieter", "lower the sound a little", "turn it down", "it's too loud"]:
    add("volume", t, [call("change_volume", direction="down")])
for n, (t, d) in zip(some(range(5, 40, 5), 4), [("turn the volume up by {n}", "up"), ("lower the volume by {n} percent", "down"),
                                                 ("raise it by {n}", "up"), ("decrease volume by {n}", "down")]):
    add("volume", t.format(n=n), [call("change_volume", direction=d, amount=n)])
for t in ["max volume", "full volume please"]:
    add("volume", t, [call("set_volume", level=100)])
for t in ["mute", "silence", "mute the tv", "kill the sound"]:
    add("volume", t, [call("mute")])
for t in ["unmute", "sound back on", "turn the sound on again"]:
    add("volume", t, [call("unmute")])

# ---------- media ----------
for t, a in [("play", "play"), ("play the music", "play"), ("resume the video", "play"), ("continue playing", "play"),
             ("pause", "pause"), ("pause the video", "pause"), ("stop the music", "pause"), ("hold on pause it", "pause"),
             ("next song", "next"), ("skip this track", "next"), ("play the next one", "next"), ("skip", "next"),
             ("previous song", "previous"), ("go back a track", "previous"), ("play the last song again", "previous")]:
    add("media", t, [call("media", action=a)])

# ---------- apps and windows ----------
open_phrases = ["open {x}", "launch {x}", "start {x}", "can you open {x} for me", "fire up {x}", "bring up {x}",
                "i want to use {x}", "open up {x} please"]
for x in some(TARGETS, 13):
    add("apps", rng.choice(open_phrases).format(x=x), [call("open", target=TARGETS[x])])
for x in some(TARGETS, 5):
    add("apps", rng.choice(open_phrases).format(x=x), [call("open", target=TARGETS[x])])
for x in some(APPS, 5):
    add("apps", rng.choice(["close {x}", "quit {x}", "exit {x}", "shut {x}"]).format(x=x), [call("close", target=APPS[x])])
for t in ["close this window", "close this", "close the app", "exit this program"]:
    add("apps", t, [call("close")])
for x in some(TARGETS, 6):
    add("apps", rng.choice(["switch to {x}", "go to {x}", "bring {x} to the front", "show me {x}"]).format(x=x),
        [call("switch_to", target=TARGETS[x])], also_ok=[[call("open", target=TARGETS[x])]])
for x in some(APPS, 3):
    add("apps", f"minimize {x}", [call("window", action="minimize", target=APPS[x])])
for x in some(APPS, 3):
    add("apps", rng.choice(["maximize {x}", "make {x} full screen"]).format(x=x),
        [call("window", action="maximize", target=APPS[x])])
for t, a in [("minimize this", "minimize"), ("hide this window", "minimize"), ("make it full screen", "maximize"),
             ("maximize the window", "maximize")]:
    add("apps", t, [call("window", action=a)])
for t in ["minimize everything", "show the desktop", "show me the desktop"]:
    add("apps", t, [call("show_desktop")])
for t in ["go home", "back to the home screen", "take me home"]:
    add("apps", t, [call("go_home")])
for t in ["show open apps", "what apps are open", "show me all the windows"]:
    add("apps", t, [call("show_open_apps")])

# ---------- browser and web ----------
for t, a in [("new tab", "new_tab"), ("open a new tab", "new_tab"), ("close this tab", "close_tab"),
             ("close the tab", "close_tab"), ("go back", "back"), ("back to the previous page", "back"),
             ("go forward", "forward"), ("next tab", "next_tab"), ("previous tab", "previous_tab"),
             ("refresh the page", "reload"), ("reload", "reload")]:
    add("browser", t, [call("browser", action=a)])
for q in some(QUERIES, 8):
    t = rng.choice(["search for {q}", "google {q}", "look up {q}", "search the web for {q}"])
    add("web", t.format(q=q), [call("web_search", query=q)])
for q in some(QUERIES, 8):
    t = rng.choice(["search youtube for {q}", "find {q} on youtube", "play {q} on youtube", "youtube {q}"])
    add("web", t.format(q=q), [call("youtube_search", query=q)])

# ---------- on-screen control ----------
for t, x in [("click subscribe", "subscribe"), ("click on sign in", "sign in"), ("hit the skip ad button", "skip ad"),
             ("press the like button", "like"), ("click the search box", "search"), ("click accept all", "accept all"),
             ("click on the first video", "first video"), ("tap on settings", "settings"), ("click next episode", "next episode"),
             ("click download", "download")]:
    add("screen", t, [call("click", target=[x, x + " button"])])
for t, x in [("type hello world", "hello world"), ("type in good morning", "good morning"),
             ("write see you tomorrow", "see you tomorrow"), ("type my name is shashank", "my name is shashank"),
             ("enter the text happy birthday", "happy birthday")]:
    add("screen", t, [call("type_text", text=x)])
for t, k in [("press enter", ["enter"]), ("hit escape", ["esc", "escape"]), ("press control t", ["ctrl+t"]),
             ("press space", ["space"]), ("press alt f4", ["alt+f4"]), ("hit the tab key", ["tab"]),
             ("press control c", ["ctrl+c"]), ("press f11", ["f11"])]:
    add("screen", t, [call("press_keys", keys=k)])
for t, d in [("scroll down", "down"), ("scroll up", "up"), ("scroll to the top", "top"), ("go to the bottom of the page", "bottom"),
             ("scroll down a bit", "down")]:
    add("screen", t, [call("scroll", direction=d)])

# ---------- system ----------
for t, s in [("turn off the screen", "off"), ("go to sleep", "off"), ("screen off", "off"), ("turn the tv picture off", "off"),
             ("wake up", "on"), ("turn the screen back on", "on"), ("screen on", "on")]:
    add("system", t, [call("screen", state=s)])
for t in ["lock the computer", "lock it", "lock the pc"]:
    add("system", t, [call("lock")])
for t, a in [("shut down the computer", "shutdown"), ("turn off the pc", "shutdown"), ("restart the computer", "restart"),
             ("reboot", "restart"), ("put the computer to sleep", "sleep")]:
    add("system", t, [call("power", action=a)])
for t, w in [("what time is it", "time"), ("tell me the time", "time"), ("what's the date today", "date"),
             ("what day is it", "date"), ("how much battery is left", "battery")]:
    add("system", t, [call("info", what=w)])

# ---------- several steps ----------
for q in some(QUERIES, 4):
    add("multi", f"open youtube and search for {q}", [call("open", target="youtube"), call("youtube_search", query=q)],
        also_ok=[[call("youtube_search", query=q)]])
for x, y in [("chrome", "gmail"), ("brave", "netflix")]:
    add("multi", f"open {x} and go to {y}", [call("open", target=TARGETS[x]), call("open", target=TARGETS[y])],
        also_ok=[[call("open", target=TARGETS[y])]])
for text, calls in [
    ("mute and turn off the screen", [call("mute"), call("screen", state="off")]),
    ("open notepad and type shopping list", [call("open", target="notepad"), call("type_text", text="shopping list")]),
    ("close chrome and open brave", [call("close", target=APPS["chrome"]), call("open", target="brave")]),
    ("pause the video and lower the volume", [call("media", action="pause"), call("change_volume", direction="down")]),
    ("open a new tab and search for pizza near me", [call("browser", action="new_tab"), call("web_search", query="pizza near me")]),
    ("set volume to 20 and play music", [call("set_volume", level=20), call("media", action="play")]),
    ("minimize everything and lock the computer", [call("show_desktop"), call("lock")]),
    ("open spotify then skip the song", [call("open", target="spotify"), call("media", action="next")]),
    ("type hello and press enter", [call("type_text", text="hello"), call("press_keys", keys=["enter"])]),
    ("go home and turn the volume up", [call("go_home"), call("change_volume", direction="up")]),
]:
    add("multi", text, calls)

# ---------- clarify / escalate / cancel ----------
for t in ["open it", "close that thing", "set the volume", "switch", "search for", "click the", "turn it"]:
    add("clarify", t, [call("clarify", question="*")])
for t in ["what's the capital of france", "tell me a joke", "how tall is mount everest", "explain photosynthesis",
          "who is the prime minister of india", "what should i cook tonight", "how are you doing", "translate thank you to german",
          "what is two plus two times three", "write a poem about the sea", "why is the sky blue", "recommend a good movie"]:
    add("escalate", t, [call("escalate")])
for t in ["never mind", "cancel", "forget it", "no stop", "that's all thanks", "nothing, sorry"]:
    add("cancel", t, [call("cancel")])

# ---------- speech-recognition mistakes ----------
for text, calls in [
    ("open crome", [call("open", target=APPS["chrome"])]),
    ("open you tube", [call("open", target="youtube")]),
    ("valium up", [call("change_volume", direction="up")]),
    ("turn the bolume down", [call("change_volume", direction="down")]),
    ("paws the music", [call("media", action="pause")]),
    ("next sung", [call("media", action="next")]),
    ("open net flicks", [call("open", target="netflix")]),
    ("close the brave", [call("close", target="brave")]),
    ("search for whether in berlin", [call("web_search", query=["weather in berlin", "whether in berlin"])]),
    ("open note pad", [call("open", target="notepad")]),
    ("new tap", [call("browser", action="new_tab")]),
    ("set the volume to thirty", [call("set_volume", level=30)]),
    ("mute the toy", [call("mute")]),
    ("open what's app", [call("open", target="whatsapp")]),
    ("screw down", [call("scroll", direction="down")]),
    ("lock the come puter", [call("lock")]),
]:
    add("asr_noise", text, calls)

counts = {}
for it in items:
    counts[it["category"]] = counts.get(it["category"], 0) + 1
    it["id"] = f"{it['category']}-{counts[it['category']]:03d}"
OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", encoding="utf-8") as f:
    for it in items:
        f.write(json.dumps({k: it[k] for k in ("id", "category", "text", "expected", "also_ok")}) + "\n")
print(f"{len(items)} items -> {OUT}")
print("  " + ", ".join(f"{c} {n}" for c, n in counts.items()))
