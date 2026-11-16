r"""
Writes Triton-Mini's training data with a big teacher model (vLLM, one GPU, offline).

    python gen_data.py [--seeds-per-tool 800] [--paraphrases 6]

Labels come from code, not from the teacher: we pick the tool calls first, and the teacher only
writes ways a person might *say* them. A sentence is kept only if it contains its call's words
(the app name, the search query...), so its label is right by construction.

Stages, each saved under $WORK_DIR/data so a rerun skips what's done:
  1. pools       lists code can't invent well: search queries, button names, dictated text,
                 general questions, ways to say "never mind" (teacher)
  2. seeds       tool calls sampled from tools/schema.py and the pools: single steps, two steps,
                 incomplete requests (clarify), not-a-command (escalate), call-offs (cancel)
  3. paraphrase  N ways to say each seed (teacher)
  4. finish      filter, add speech-recognition mistakes, add code-written TEMPLATES for patterns the
                 teacher rarely says plainly, drop anything in the eval set, split into train.jsonl / val.jsonl

Round 2 (2026-10-30), after all round-1 students plateaued at ~86% with the same misses: seeds now
carry ["any", ...] / ["not", ...] word rules (press_keys must name its key, typing needs a typing
verb, switch_to may not say "open"/"bring up"), "max volume" seeds survive the number filter,
"open youtube + search" is always youtube_search, command-like "not a command" sentences are dropped,
and TEMPLATES add plain forms ("search for X", "close this", "that's all thanks").
Delete $WORK_DIR/data before rerunning: pools and paraphrases are cached there.
"""

import argparse
import json
import random
import re

from common import CODE, DATA, model_path, read_jsonl, structured, write_jsonl
from scoring import norm
from tools.schema import BY_NAME

APPS = {   # spoken forms; the label is whichever form the sentence uses
    "chrome": ["chrome", "google chrome"], "brave": ["brave", "brave browser"], "edge": ["edge", "microsoft edge"],
    "firefox": ["firefox"], "word": ["word", "microsoft word"], "excel": ["excel"], "powerpoint": ["powerpoint"],
    "outlook": ["outlook"], "teams": ["teams", "microsoft teams"], "notepad": ["notepad"], "calculator": ["calculator"],
    "files": ["files", "file explorer", "explorer"], "settings": ["settings"], "spotify": ["spotify"], "vlc": ["vlc"],
    "paint": ["paint"], "terminal": ["terminal", "windows terminal"], "camera": ["camera"], "photos": ["photos"],
    "clock": ["clock"], "calendar": ["calendar"], "task manager": ["task manager"], "control panel": ["control panel"],
    "store": ["store", "microsoft store"], "steam": ["steam"], "discord": ["discord"], "zoom": ["zoom"],
    "obs": ["obs"], "vs code": ["vs code", "visual studio code"], "snipping tool": ["snipping tool"],
    "media player": ["media player"], "kodi": ["kodi"], "telegram": ["telegram"], "skype": ["skype"],
}
SITES = {
    "youtube": ["youtube"], "youtube music": ["youtube music"], "gmail": ["gmail"], "netflix": ["netflix"],
    "prime video": ["prime video", "amazon prime"], "disney plus": ["disney plus", "hotstar"], "whatsapp": ["whatsapp"],
    "google maps": ["google maps"], "chat gpt": ["chat gpt"], "google": ["google"], "amazon": ["amazon"],
    "wikipedia": ["wikipedia"], "reddit": ["reddit"], "instagram": ["instagram"], "facebook": ["facebook"],
    "twitch": ["twitch"], "linkedin": ["linkedin"], "google drive": ["google drive"], "google photos": ["google photos"],
    "google news": ["google news"], "github": ["github"], "jio cinema": ["jio cinema"],
}
TARGETS = {**APPS, **SITES}
BROWSERS = {k: APPS[k] for k in ("chrome", "brave", "edge", "firefox")}
KEYS = [("enter", "enter"), ("esc", "escape"), ("space", "space"), ("tab", "tab"), ("backspace", "backspace"),
        ("delete", "delete"), ("ctrl+t", "control t"), ("ctrl+w", "control w"), ("ctrl+c", "control c"),
        ("ctrl+v", "control v"), ("ctrl+z", "control z"), ("ctrl+a", "control a"), ("ctrl+s", "control s"),
        ("ctrl+f", "control f"), ("alt+f4", "alt f4"), ("alt+tab", "alt tab"), ("f5", "f5"), ("f11", "f11"),
        ("win", "the windows key"), ("up", "the up arrow"), ("down", "the down arrow"), ("left", "the left arrow"),
        ("right", "the right arrow"), ("pagedown", "page down"), ("pageup", "page up"), ("ctrl+shift+t", "control shift t")]
# Words a press_keys sentence must name, so "close this window" can't be labelled alt+f4 (round 1 did that)
KEY_NEED = {"enter": ["any", "enter", "return"], "esc": ["any", "escape", "esc"], "delete": ["any", "delete", "del"],
            "alt+f4": "f4", "alt+tab": "alt tab", "win": "windows", "pagedown": ["any", "page down", "pagedown"],
            "pageup": ["any", "page up", "pageup"], "ctrl+shift+t": "shift"}
KEY_NEED.update({k: ["any", f"control {k[-1]}", f"ctrl {k[-1]}"] for k, _ in KEYS
                 if k.startswith("ctrl+") and k.count("+") == 1})
KEY_NEED.update({k: k for k in ("space", "tab", "backspace", "f5", "f11", "up", "down", "left", "right")})
TYPE_VERBS = ["any", "type", "write", "enter", "input", "fill in", "key in", "put"]   # else it's not dictation
STYLES = ["a short command", "a polite request", "casual and relaxed", "indirect: describe the need, not the action",
          "with filler words (um, please, can you, hey)", "Indian English", "British English",
          "very terse, two or three words", "a bit impatient", "spoken by a child"]

POOLS = {   # name: (prompt, hints, how many to keep)
    "web_queries": ("Write 40 different things people search for on Google about {h}. 2 to 7 words each, lowercase, "
                    "no quotes, no question marks.",
                    ["cooking", "sports", "weather", "news", "fixing computer problems", "travel", "health", "shopping",
                     "movies and shows", "music", "science", "places nearby", "money", "how to do things", "history",
                     "games", "studying", "cars", "pets", "Indian cities", "German cities", "jobs", "celebrities"], 900),
    "youtube_queries": ("Write 40 different searches someone types into YouTube on a TV, about {h}. 1 to 6 words "
                        "each, lowercase, no quotes.",
                        ["music genres", "famous singers and bands", "tutorials", "comedy", "kids shows",
                         "cricket and football highlights", "podcasts", "documentaries", "movie trailers",
                         "relaxing sounds", "cooking videos", "gaming", "news channels", "bollywood songs",
                         "workouts", "tech reviews", "travel vlogs"], 900),
    "click_targets": ("Write 40 different labels of buttons, links, tabs or menu items a person might click on "
                      "{h}. 1 to 4 words each, lowercase, exactly as shown on screen, no quotes.",
                      ["a YouTube page", "Netflix", "Gmail", "a web browser", "Windows settings", "File Explorer",
                       "Spotify", "a shopping website", "a news website", "a login page", "a cookie banner",
                       "a video player", "Microsoft Word", "WhatsApp Web", "Google search results", "a game launcher"], 700),
    "dictation": ("Write 40 short texts someone might dictate for the computer to type: {h}. 2 to 10 words each, "
                  "lowercase, no quotes.",
                  ["chat messages to friends", "text for a search box", "email subject lines", "notes to self",
                   "made-up names and addresses", "reminders", "website addresses", "file names", "song titles",
                   "short greetings"], 600),
    "escalate": ("Write 40 different things a person might say to a voice assistant that are NOT about controlling "
                 "the computer, about {h}. Natural spoken English, one sentence each, no numbering.",
                 ["general knowledge", "math", "jokes and chit-chat", "life advice", "word meanings", "history",
                  "science", "recommendations", "translations", "opinions", "questions about the assistant itself",
                  "writing a poem or story", "health", "sports facts", "geography", "cooking advice", "the future",
                  "feelings and small talk"], 3500),
    "tricky": ("Write 40 questions or remarks that use words like volume, open, close, screen, play, search, window, "
               "tab, mute, click, home or lock, but are NOT commands to control a computer, about {h}. "
               "For example: what is the volume of a cylinder. One sentence each.",
               ["math and science", "curiosity about technology", "history", "stories and books", "comparing things",
                "what words mean", "sports", "everyday life"], 900),
    "cancel": ("Write 40 different ways a person calls off something they just asked a voice assistant to do "
               "(like never mind, or stop), or tells it they need nothing more, {h}. 1 to 6 words each.",
               ["politely", "casually", "annoyed", "very short", "in Indian English", "in British English",
                "ending the conversation (that's all, I'm done, nothing else, thanks)"], 600),
}

CLARIFY = [  # what the incomplete request is missing -> the question the assistant asks
    ("asks to open something but doesn't say what", "What should I open?"),
    ("asks to close something but doesn't say which app", "Which app should I close?"),
    ("asks to change the volume without saying louder, quieter or how much", "Louder or quieter?"),
    ("asks to search for something but doesn't say what", "What should I search for?"),
    ("asks to click something on screen but doesn't say what", "What should I click?"),
    ("asks to type something but doesn't say the text", "What should I type?"),
    ("asks to switch to another app but doesn't say which", "Which app should I switch to?"),
    ("starts a command and trails off before finishing it", "Sorry, could you say that again?"),
]

MULTI_PAIRS = [("open", "youtube_search"), ("open", "web_search"), ("open", "type_text"), ("open", "media"),
               ("open", "window"), ("media", "change_volume"), ("mute", "screen"), ("browser", "web_search"),
               ("close", "open"), ("type_text", "press_keys"), ("go_home", "change_volume"), ("set_volume", "media"),
               ("show_desktop", "lock"), ("switch_to", "media"), ("click", "type_text"), ("scroll", "click"),
               ("open", "open")]
# Sentences a teacher paraphrase rarely produces plainly, written by code (kind "template"). Round 1's
# misses (README "Round 1 verdict"): plain "search for X", closing the front window, chat vs typing,
# "max volume", open vs switch_to, "that's all". {q} web query, {y} YouTube query, {a} app, {s} site, {b} browser.
TEMPLATES = [
    (["search for {q}", "search the web for {q}", "look up {q}", "google {q}", "search {q}", "search online for {q}",
      "find {q} on google", "do a search for {q}", "look up {q} online"], lambda v: [call("web_search", query=v["q"])]),
    (["search youtube for {y}", "look up {y} on youtube", "find {y} on youtube", "youtube search {y}"],
     lambda v: [call("youtube_search", query=v["y"])]),
    (["open youtube and search for {y}", "open youtube and look up {y}", "go to youtube and search {y}",
      "open youtube then search for {y}", "put on youtube and find {y}"],
     lambda v: [call("open", target="youtube"), call("youtube_search", query=v["y"])]),
    (["open {b} and go to {s}", "open {b} and open {s}", "start {b} then go to {s}"],
     lambda v: [call("open", target=v["b"]), call("open", target=v["s"])]),
    (["close this", "close it", "close this window", "close the app", "close this app", "close this program",
      "exit this program", "exit this", "quit this app", "shut this window", "get rid of this window",
      "close whatever is open", "close the current window", "exit the app"], lambda v: [call("close")]),
    (["i want to use {a}", "bring up {a}", "launch {a}", "start {a}", "fire up {a}", "open up {a}",
      "can i get {a}", "put {a} on", "i need {a}"], lambda v: [call("open", target=v["a"])]),
    (["switch to {a}", "switch over to {a}", "bring {a} to the front", "go back to {a}", "show {a} again",
      "jump to {a}", "bring {a} forward"], lambda v: [call("switch_to", target=v["a"])]),
    (["max volume", "maximum volume", "full volume", "volume all the way up", "turn it all the way up",
      "crank the volume to max", "volume to the max", "full blast"], lambda v: [call("set_volume", level=100)]),
    (["i can't hear anything", "it's too quiet", "i can barely hear it", "i can't hear this", "it's way too soft",
      "make it louder", "louder"], lambda v: [call("change_volume", direction="up")]),
    (["it's too loud", "that's way too loud", "make it quieter", "quieter", "it's hurting my ears"],
     lambda v: [call("change_volume", direction="down")]),
    (["go forward", "forward a page", "go forward one page", "go forward in the browser"],
     lambda v: [call("browser", action="forward")]),
    (["how are you", "how are you doing today", "what's up", "who are you", "good morning", "tell me a joke",
      "how's it going", "what can you do", "are you there", "why is the ocean salty", "what is the capital of france",
      "translate good morning to german", "write a short story about a cat", "explain gravity"],
     lambda v: [call("escalate")]),
    (["that's all", "that's all thanks", "that's it", "i'm done", "nothing else", "all good thanks",
      "no that's fine", "that will be all", "nothing thanks"], lambda v: [call("cancel")]),
]
TEMPLATE_PREFIX = ["", "", "", "hey ", "please ", "can you ", "could you ", "um "]
TEMPLATE_SUFFIX = ["", "", "", " please", " now", " thanks"]
COMMAND_START = ("search ", "look up ", "google ", "open ", "close ", "play ", "mute ", "click ", "type ",
                 "switch ", "turn ", "set ", "go to ", "lock ", "show ", "launch ", "start ")

ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(n):
    if n < 20:
        return ONES[n]
    if n == 100:
        return "hundred"
    return TENS[n // 10 - 2] + ("" if n % 10 == 0 else " " + ONES[n % 10])


# ---------- teacher ----------

class Teacher:
    def __init__(self, path):
        from transformers import AutoTokenizer
        from vllm import LLM
        self.tok = AutoTokenizer.from_pretrained(path)
        self.llm = LLM(model=str(path), max_model_len=4096, gpu_memory_utilization=0.90)

    def lists(self, prompts, n):
        """Each prompt -> a list of n strings (or None if it went wrong)."""
        schema = {"type": "array", "items": {"type": "string", "maxLength": 200}, "minItems": n, "maxItems": n}
        params = structured(schema, temperature=1.0, top_p=0.95, max_tokens=60 * n)
        texts = [self.tok.apply_chat_template([{"role": "user", "content": p}], tokenize=False,
                                              add_generation_prompt=True) for p in prompts]
        out = []
        for o in self.llm.generate(texts, params):
            try:
                out.append([s.strip() for s in json.loads(o.outputs[0].text) if isinstance(s, str) and s.strip()])
            except ValueError:
                out.append(None)
        return out


# ---------- stage 1: pools ----------

def make_pools(teacher, rng):
    path = DATA / "pools.json"
    if path.exists():
        return json.loads(path.read_text())
    pools = {}
    for name, (prompt, hints, keep) in POOLS.items():
        calls = int(keep / 40 * 1.6) + 1
        prompts = [prompt.format(h=hints[i % len(hints)]) for i in range(calls)]
        seen, items = set(), []
        for lst in teacher.lists(prompts, 40):
            for s in lst or []:
                key = norm(s)
                if key and key not in seen and len(key.split()) <= 25:
                    seen.add(key)
                    items.append(s.lower().rstrip(".") if name not in ("escalate", "tricky") else s)
        rng.shuffle(items)
        pools[name] = items[:keep]
        print(f"  pool {name}: {len(pools[name])}")
    path.write_text(json.dumps(pools, indent=1, ensure_ascii=False))
    return pools


# ---------- stage 2: seeds ----------

def call(tool, **args):
    return {"tool": tool, "args": args}


def sample_call(tool, rng, pools):
    """A random call of `tool`, plus the exact words a sentence asking for it must contain
    (strings, or ["num", n] for a number that may be said as digits or words)."""
    def target(pool):
        spoken = rng.choice(pool[rng.choice(list(pool))])
        return spoken, [spoken]

    match tool:
        case "set_volume":
            n = rng.choice(range(0, 101, 5)) if rng.random() < 0.8 else rng.randint(1, 99)
            if n == 100 and rng.random() < 0.6:     # "max volume" has no number: round 1 dropped all of these
                return call(tool, level=100), [["any", "max", "maximum", "full", "all the way", "100", "hundred"]]
            return call(tool, level=n), [["num", n]]
        case "change_volume":
            d = rng.choice(["up", "down"])
            if rng.random() < 0.3:
                n = rng.choice(range(5, 55, 5))
                return call(tool, direction=d, amount=n), [["num", n]]
            return call(tool, direction=d), []
        case "media":
            return call(tool, action=rng.choice(["play", "pause", "next", "previous"])), []
        case "open":
            t, need = target(TARGETS)
            return call(tool, target=t), need + [["not", "switch", "to the front"]]
        case "switch_to":
            t, need = target(TARGETS)
            return call(tool, target=t), need + [["not", "open", "launch", "start", "bring up", "use"]]
        case "close":
            if rng.random() < 0.5:
                return call(tool), [["not", "f4", "tab"]]
            t, need = target(APPS)
            return call(tool, target=t), need
        case "window":
            a = rng.choice(["minimize", "maximize"])
            if rng.random() < 0.5:
                return call(tool, action=a), [["not", "front"]]
            t, need = target(APPS)
            return call(tool, action=a, target=t), need + [["not", "front"]]
        case "browser":
            return call(tool, action=rng.choice(BY_NAME["browser"].args["action"][0])), []
        case "web_search" | "youtube_search":
            q = rng.choice(pools["web_queries" if tool == "web_search" else "youtube_queries"])
            return call(tool, query=q), [q]
        case "click":
            t = rng.choice(pools["click_targets"])
            return call(tool, target=t), [t]
        case "type_text":
            t = rng.choice(pools["dictation"])
            return call(tool, text=t), [t, TYPE_VERBS]
        case "press_keys":
            keys, _ = rng.choice(KEYS)
            return call(tool, keys=keys), [KEY_NEED[keys]]
        case "scroll":
            return call(tool, direction=rng.choice(["up", "down", "top", "bottom"])), []
        case "screen":
            return call(tool, state=rng.choice(["off", "on"])), []
        case "power":
            return call(tool, action=rng.choice(["shutdown", "restart", "sleep"])), []
        case "info":
            return call(tool, what=rng.choice(["time", "date", "battery"])), []
    return call(tool), []          # no arguments: mute, unmute, show_desktop, go_home, show_open_apps, lock


def describe(c):
    a = c["args"]
    t = a.get("target")
    match c["tool"]:
        case "set_volume":
            return "turn the volume all the way up to the maximum" if a["level"] == 100 \
                else f"set the volume to {a['level']} percent"
        case "change_volume":
            return f"turn the volume {a['direction']}" + (f" by {a['amount']} percent" if "amount" in a else "")
        case "mute":
            return "mute the sound"
        case "unmute":
            return "unmute the sound"
        case "media":
            return {"play": "play or resume the music or video", "pause": "pause the music or video",
                    "next": "skip to the next song or video",
                    "previous": "go back to the previous song or video"}[a["action"]]
        case "open":
            return f"open {t} (start it: open, launch, start, bring up, I want to use...)"
        case "close":
            return f"close {t}" if t else "close the app or window that's in front right now (close this, exit, quit)"
        case "switch_to":
            return (f"switch to {t}, which is already open in the background "
                    f"(switch to, go back to, bring it to the front, show it again)")
        case "window":
            return f"{a['action']} " + (t if t else "the current window")
        case "show_desktop":
            return "minimize everything and show the desktop"
        case "go_home":
            return "go back to the TV's home screen"
        case "show_open_apps":
            return "show the list of open apps"
        case "browser":
            return {"new_tab": "open a new browser tab", "close_tab": "close the current browser tab",
                    "next_tab": "go to the next browser tab", "previous_tab": "go to the previous browser tab",
                    "back": "go back a page in the browser", "forward": "go forward a page in the browser",
                    "reload": "reload the page"}[a["action"]]
        case "web_search":
            return f"search the web for: {a['query']}"
        case "youtube_search":
            return f"search YouTube for: {a['query']}"
        case "click":
            return f"click the '{t}' button or link on screen"
        case "type_text":
            return f"type this text: {a['text']}"
        case "press_keys":
            return f"press {dict(KEYS)[a['keys']]} on the keyboard"
        case "scroll":
            return {"up": "scroll up", "down": "scroll down", "top": "scroll to the top",
                    "bottom": "scroll to the bottom"}[a["direction"]]
        case "screen":
            return ("turn the TV picture off (the computer keeps running)" if a["state"] == "off"
                    else "turn the TV picture back on")
        case "lock":
            return "lock the computer"
        case "power":
            return {"shutdown": "shut down the computer", "restart": "restart the computer",
                    "sleep": "put the computer to sleep"}[a["action"]]
        case "info":
            return f"tell the {a['what']}" if a["what"] != "battery" else "tell how much battery is left"
    raise ValueError(c)


def make_seeds(rng, pools, per_tool):
    command_tools = [t for t in BY_NAME if t not in ("clarify", "escalate", "cancel")]
    no_args = {t for t in command_tools if not BY_NAME[t].args}
    seeds = []
    for tool in command_tools:
        for _ in range(per_tool // 4 if tool in no_args else per_tool):
            c, need = sample_call(tool, rng, pools)
            seeds.append({"kind": "single", "calls": [c], "need": need, "desc": describe(c)})
    for _ in range(per_tool * 3):
        first, second = rng.choice(MULTI_PAIRS)
        (c1, n1), (c2, n2) = sample_call(first, rng, pools), sample_call(second, rng, pools)
        # Keep searches consistent with where they happen: YouTube -> youtube_search, a browser -> web_search
        # (round 1 paired "open youtube" with web_search and taught exactly the wrong thing)
        if first == "open" and second in ("youtube_search", "web_search"):
            site = "youtube" if second == "youtube_search" else rng.choice(list(BROWSERS) + ["google"])
            spoken = rng.choice(TARGETS[site])
            c1, n1 = call("open", target=spoken), [spoken]
        if (first, second) == ("open", "open"):        # "open chrome and go to gmail"
            b, s = rng.choice(list(BROWSERS)), rng.choice(list(SITES))
            (b_said, s_said) = rng.choice(BROWSERS[b]), rng.choice(SITES[s])
            (c1, n1), (c2, n2) = (call("open", target=b_said), [b_said]), (call("open", target=s_said), [s_said])
        seeds.append({"kind": "multi", "calls": [c1, c2], "need": n1 + n2,
                      "desc": f"first {describe(c1)}, then {describe(c2)}"})
    for _ in range(per_tool // 8):
        for missing, question in CLARIFY:
            seeds.append({"kind": "clarify", "calls": [call("clarify", question=question)], "need": [],
                          "desc": missing})
    return seeds


def make_templates(rng, pools, per_template=40):
    """Code-written sentences for the patterns in TEMPLATES, each filled with random queries and names."""
    rows = []
    for forms, calls_for in TEMPLATES:
        for form in forms:
            # sentences with a blank get many fillings; fixed ones only vary by prefix/suffix
            for _ in range(per_template if "{" in form else 6):
                v = {"q": rng.choice(pools["web_queries"]), "y": rng.choice(pools["youtube_queries"]),
                     "a": rng.choice(APPS[rng.choice(list(APPS))]), "s": rng.choice(SITES[rng.choice(list(SITES))]),
                     "b": rng.choice(BROWSERS[rng.choice(list(BROWSERS))])}
                text = rng.choice(TEMPLATE_PREFIX) + form.format(**v) + rng.choice(TEMPLATE_SUFFIX)
                rows.append((text, calls_for(v)))
    return rows


# ---------- stage 3: paraphrase ----------

def paraphrase(teacher, seeds, n, rng):
    prompts = []
    for s in seeds:
        words = [w for w in s["need"] if isinstance(w, str)]
        rules = ["Natural spoken English, like real speech: no emojis, no quotation marks, no numbering."]
        if words:
            rules.append("Every sentence must contain these exact words: " + "; ".join(words) + ".")
        for w in s["need"]:
            if isinstance(w, list) and w[0] == "num":
                rules.append("Numbers may be digits or words.")
            elif isinstance(w, list) and w[0] == "any":
                rules.append("Every sentence must use one of: " + ", ".join(w[1:]) + ".")
            elif isinstance(w, list) and w[0] == "not":
                rules.append("Never use these words: " + ", ".join(w[1:]) + ".")
        if s["kind"] == "clarify":
            what = (f"Write {n} different things a person might say to a voice assistant on a TV computer, where the "
                    f"person {s['desc']}. Keep each one incomplete in that way.")
        else:
            what = (f"A voice assistant on a Windows TV computer will do this: {s['desc']}.\n"
                    f"Write {n} different things a person might SAY to ask for exactly that, and nothing more.")
        style = f"Mix styles, and make at least two of them {rng.choice(STYLES)}."
        prompts.append(f"{what}\n{style}\nRules:\n- " + "\n- ".join(rules) + f"\nReturn a JSON array of {n} strings.")
    return teacher.lists(prompts, n)


# ---------- stage 4: finish ----------

def contains(text, need):
    """need: exact words ("rain sounds"), ["num", n] digits or words, ["any", ...] one of them,
    ["not", ...] none of them."""
    t = " " + norm(text) + " "

    def has(w):
        return f" {norm(w)} " in t

    for w in need:
        if isinstance(w, str):
            if not has(w):
                return False
        elif w[0] == "num":
            if f" {w[1]} " not in t and not has(number_words(w[1])):
                return False
        elif w[0] == "any":
            if not any(has(x) for x in w[1:]):
                return False
        elif w[0] == "not":
            if any(has(x) for x in w[1:]):
                return False
    return True


MISHEARD = {   # what speech recognition often writes instead (the label stays the real word)
    "chrome": ["crome", "rome", "crohn"], "volume": ["valium", "bolume", "column"], "youtube": ["you tube", "u tube"],
    "netflix": ["net flicks", "netflicks"], "notepad": ["note pad"], "pause": ["paws", "pores"],
    "whatsapp": ["what's app", "whats up"], "next": ["necks", "text"], "tab": ["tap", "tub"], "scroll": ["screw", "school"],
    "mute": ["mood", "moot"], "spotify": ["spot if i", "spotty fi"], "excel": ["accel", "x l"], "brave": ["grave", "breathe"],
    "open": ["often", "hoping"], "close": ["clothes", "cloze"], "screen": ["scream", "green"], "louder": ["lauder"],
    "search": ["such", "surge"], "click": ["clique", "clip"], "minimize": ["minimise", "mini mice"], "type": ["tight"],
    "gmail": ["g mail", "gee mail"], "settings": ["setting"], "lock": ["log", "look"], "skip": ["ship", "skit"],
}
FILLERS = {"please", "um", "uh", "hey", "can", "you", "just", "the", "a", "now"}


def add_noise(text, protected, rng):
    """Speech-recognition style mistakes outside the protected phrases (queries, typed text, button names)."""
    words = norm(text).split()
    keep = set()
    for p in protected:
        pw = norm(p).split()
        for i in range(len(words) - len(pw) + 1):
            if words[i:i + len(pw)] == pw:
                keep.update(range(i, i + len(pw)))
    out, changed = [], False
    for i, w in enumerate(words):
        if i not in keep and w in MISHEARD and rng.random() < 0.5:
            out.append(rng.choice(MISHEARD[w]))
            changed = True
        elif i not in keep and w in FILLERS and rng.random() < 0.2:
            changed = True                                   # dropped
        else:
            out.append(w)
    return " ".join(out) if changed else None


def stt_style(text, rng):
    """Most speech recognisers write lowercase without punctuation; Whisper-like ones don't."""
    if rng.random() < 0.65:
        return " ".join(re.sub(r"[^\w' ]", " ", text.lower()).split())
    return text


def finish(seeds, outs, pools, rng, eval_texts):
    rows, seen = [], set()
    dropped = {"missing words": 0, "in eval set": 0, "duplicate": 0, "bad output": 0}

    def add(text, calls, kind):
        key = norm(text)
        if not key:
            return
        if key in eval_texts:
            dropped["in eval set"] += 1
        elif key in seen:
            dropped["duplicate"] += 1
        else:
            seen.add(key)
            rows.append({"text": text, "calls": calls, "kind": kind})

    for s, lst in zip(seeds, outs):
        if lst is None:
            dropped["bad output"] += 1
            continue
        protected = [c["args"][k] for c in s["calls"] for k in ("query", "text") if k in c["args"]] \
            + [c["args"]["target"] for c in s["calls"] if c["tool"] == "click"]
        for text in lst:
            if not contains(text, s["need"]):
                dropped["missing words"] += 1
                continue
            add(stt_style(text, rng), s["calls"], s["kind"])
            if s["kind"] in ("single", "multi") and rng.random() < 0.25:
                noisy = add_noise(text, protected, rng)
                if noisy:
                    add(noisy, s["calls"], "noise")
    for text in pools["escalate"] + pools["tricky"]:
        # a "not a command" that starts like one ("search for the meaning of...") taught round 1 to
        # escalate real searches
        if norm(text).startswith(tuple(norm(c) + " " for c in COMMAND_START)):
            dropped["looks like a command"] = dropped.get("looks like a command", 0) + 1
            continue
        add(stt_style(text, rng), [call("escalate")], "escalate")
    for text in pools["cancel"]:
        add(stt_style(text, rng), [call("cancel")], "cancel")
    for text, calls in make_templates(rng, pools):
        add(stt_style(text, rng), calls, "template")
    print("  dropped: " + ", ".join(f"{k} {v}" for k, v in dropped.items()))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teacher", default="Qwen3-30B-A3B-Instruct-2507-FP8")
    ap.add_argument("--seeds-per-tool", type=int, default=800)
    ap.add_argument("--paraphrases", type=int, default=6)
    ap.add_argument("--val", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    DATA.mkdir(parents=True, exist_ok=True)

    print("== loading teacher", a.teacher, flush=True)
    teacher = Teacher(model_path(a.teacher))
    print("== 1/4 pools", flush=True)
    pools = make_pools(teacher, rng)

    print("== 2/4 seeds", flush=True)
    seeds = make_seeds(rng, pools, a.seeds_per_tool)
    print(f"  {len(seeds)} seeds")

    print("== 3/4 paraphrasing", flush=True)
    raw = DATA / "paraphrases.jsonl"
    if raw.exists():
        saved = read_jsonl(raw)
        seeds, outs = [r["seed"] for r in saved], [r["out"] for r in saved]
    else:
        outs = paraphrase(teacher, seeds, a.paraphrases, rng)
        write_jsonl(raw, [{"seed": s, "out": o} for s, o in zip(seeds, outs)])

    print("== 4/4 finishing", flush=True)
    eval_texts = {norm(r["text"]) for r in read_jsonl(CODE / "bench" / "eval" / "eval.jsonl")}
    rows = finish(seeds, outs, pools, rng, eval_texts)
    rng.shuffle(rows)
    n_val = int(len(rows) * a.val)
    write_jsonl(DATA / "val.jsonl", rows[:n_val])
    write_jsonl(DATA / "train.jsonl", rows[n_val:])
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"  train {len(rows) - n_val}, val {n_val}: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    print("  examples:")
    for r in rows[:12]:
        print(f"    {r['text']!r} -> {json.dumps(r['calls'])}")


if __name__ == "__main__":
    main()
