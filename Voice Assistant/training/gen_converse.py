r"""
Triton-Mini v2 ("converse-mini") training data: the N95's own model learns to act, talk, or ask again, like the
laptop's brain, instead of only turning sentences into tool calls. Built on top of gen_data.py's round-2 data.

    python gen_converse.py [--teacher Qwen3-30B-A3B-Instruct-2507-FP8]
    (needs $WORK_DIR/data/train.jsonl, val.jsonl, pools.json from gen_data.py; writes $WORK_DIR/data_v2/)

Each row: {"messages": [{"role": "user"|"assistant", "content": ...}, ...], "reply": {...}, "kind": ...}
  reply is one of {"calls": [...]}, {"say": "..."}, {"repeat": true}; the student sees COMPACT_CONVERSE as its
  system prompt (brain/prompt.py), earlier turns as user / {"say": ...} pairs, and what was just said, with the
  screen line first when there is one: "[screen: Netflix - Google Chrome | on screen: Shashank, Kids] pick shashank"

What's new compared with round 2 (README 5m: on 49 real commands the 0.6B got 67%, and couldn't chat at all):
  1. commands      round-2 rows, as {"calls"}; clarify -> {"say": its question}
  2. chat          round-2 "escalate" sentences: the teacher sorts them into small talk (answered here, in a short
                   spoken {"say"}) and real questions (escalate: the laptop answers those)
  3. not for me    TV chatter, people talking to each other, half sentences, word salad -> {"repeat": true}
                   (live, the TV's own sound woke it up and it acted on it)
  4. screen        commands with a screen line: click names copied as on screen ("shashant" -> "Shashank"),
                   "the first profile", "search X here" = search in the site in front, and unrelated screens
  5. follow-ups    a turn before ("what's the capital of france" / "Paris." -> "and germany?"; "open youtube" ->
                   "search lofi there"); the teacher writes the chat ones
  6. real use      what you actually said to Triton (real_seeds.jsonl, from conversation.log; never the eval set
                   bench/eval/real.jsonl), labelled by the teacher with the laptop's full rules, x3 with noise
Teacher stages are cached in data_v2/teacher_*.json: delete one to redo it.

v3 (--v3, writes data_v3/; README 5r): everything above, reusing data_v2's cached teacher stages, plus
  7. gen_v3.py     code-written contrast groups (close / thanks / results / clear search / people talking / word
                   salad / polite / multi-step), labels exact
  8. chat + screen small talk also with a screen line (v2 answered "you're awesome" over YouTube with "ask again")
  9. no fake acts  chat replies that claim an action ("I'll close the call now") are dropped
 10. checked       every round-2 command is labelled again by the teacher with the laptop's rules; rows where the
                   two disagree on the tools are dropped (teacher_verify.json; the drop rate is printed)
  and the N95 test sentences (bench/eval/n95.jsonl) are kept out of training like the other tests.
"""

import argparse
import json
import random

from common import CODE, WORK, model_path, read_jsonl, structured, write_jsonl
from gen_data import MISHEARD, Teacher, add_noise, call, stt_style
from gen_v3 import all_rows, says_it_acts
from scoring import norm

V1 = WORK / "data"
OUT = WORK / "data_v2"

SCREENS = [   # (window title, clickable names) seen on the N95 (screen.py), plus a few likely ones
    ("Netflix - Google Chrome", ["Home", "Shows", "Movies", "Games", "New & Popular", "My List", "Search", "Kids"]),
    ("Netflix - Google Chrome", ["Who's watching?", "Shashank", "Shamitha K shetty", "Kids", "Manage Profiles"]),
    ("YouTube - Google Chrome", ["Home", "Shorts", "Subscriptions", "You", "Search", "Explore", "Music", "Gaming"]),
    ("YouTube Music - Google Chrome", ["Home", "Explore", "Library", "Search", "Upgrade", "Play", "Shuffle"]),
    ("Google - Google Chrome", ["Gmail", "Images", "Google apps", "Search", "I'm Feeling Lucky", "Sign in"]),
    ("Triton Launcher", ["Hey Triton", "Windows", "Chrome", "Netflix", "YouTube", "Settings"]),
    ("Prime Video - Google Chrome", ["Home", "Movies", "TV Shows", "Live TV", "Search", "My Stuff"]),
    ("Settings", ["System", "Bluetooth & devices", "Network & internet", "Personalization", "Apps", "Accounts"]),
    ("Spotify", ["Home", "Search", "Your Library", "Play", "Shuffle", "Liked Songs"]),
]
PROFILE_NAMES = ["Shashank", "Shamitha", "Priya", "Rahul", "Anna", "Max", "Ravi", "Meera", "Tom", "Sophie", "Arjun",
                 "Lena", "Kids", "Guest", "Dad", "Mom", "Nikhil", "Ananya"]
ORDINAL_WORDS = [("first", 0), ("second", 1), ("third", 2), ("last", -1)]
HERE_SEARCH = ["search for {q} here", "find {q} in here", "look for {q} here", "search {q} on this",
               "find me {q}", "can you search {q} here", "search for {q} in this", "look up {q} here"]
CLICK_FORMS = ["click {t}", "click on {t}", "select {t}", "pick {t}", "press {t}", "tap on {t}", "go to {t}",
               "choose the {t} one", "click the {t} button", "select the {t} profile"]
HALF = ["open the", "search for the", "can you", "turn the", "play the", "click on the", "go to the", "i want to",
        "set the", "could you please", "hey triton", "um", "switch to the", "what about", "and then", "no wait"]
CHATTER = ("Write 40 different lines that a microphone near a TV might pick up that are NOT meant for a voice "
           "assistant: {h}. One line each, as speech recognition would write it, no quotes, no names of real songs "
           "or shows.")
CHATTER_HINTS = ["a TV presenter or news anchor talking", "cricket or football commentary", "characters in a sitcom",
                 "a cooking show host", "a YouTuber talking to the camera", "people on the sofa chatting about dinner",
                 "someone on a phone call", "kids playing", "made-up song lines being sung", "an advert",
                 "a documentary narrator", "a wedding speech", "two friends gossiping", "someone talking to a pet",
                 "a quiz show host", "a reaction video"]
PERSONA = ("You are Triton, a friendly, quick-witted voice assistant on a TV computer in someone's living room. "
           "Spoken replies: one or two short sentences (under 30 words), answer first, warm and casual like a friend "
           "on the sofa, no lists, no markdown, no emojis, never 'sir', no offers of more help.")


def screen_line(title, names):
    return f"[screen: {title}" + (f" | on screen: {', '.join(names)}" if names else "") + "]"


def user(text, screen=None):
    return {"role": "user", "content": (screen_line(*screen) + " " if screen else "") + text}


def said(reply_text):
    return {"role": "assistant", "content": json.dumps({"say": reply_text}, separators=(",", ":"))}


def row(messages, reply, kind):
    return {"messages": messages, "reply": reply, "kind": kind}


# ---------- teacher jobs ----------

def teacher_json(teacher, prompts, schema, max_tokens=300, temperature=0.7):
    params = structured(schema, temperature=temperature, top_p=0.95, max_tokens=max_tokens)
    texts = [teacher.tok.apply_chat_template([{"role": "user", "content": p}], tokenize=False,
                                             add_generation_prompt=True) for p in prompts]
    out = []
    for o in teacher.llm.generate(texts, params):
        try:
            out.append(json.loads(o.outputs[0].text))
        except ValueError:
            out.append(None)
    return out


def cached(name, make):
    path = OUT / f"teacher_{name}.json"
    for p in (path, WORK / "data_v2" / f"teacher_{name}.json"):     # v3 reuses v2's teacher work
        if p.exists():
            return json.loads(p.read_text())
    data = make()
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


def sort_chat(teacher, texts):
    """Small talk (answered on the N95) vs real questions (the laptop answers): [{"kind", "say"}]."""
    schema = {"type": "object", "properties": {"kind": {"enum": ["chat", "knowledge"]},
                                                 "say": {"type": "string", "maxLength": 200}},
              "required": ["kind", "say"]}
    prompts = [f"{PERSONA}\nSomeone just said to you: \"{t}\"\n"
               "kind \"chat\": greetings, thanks, how are you, feelings, jokes, questions about you, compliments, "
               "simple things everyone knows. kind \"knowledge\": anything needing facts, explanations, maths, advice, "
               "translations, stories, news, or more than a quick reply.\n"
               "say: your spoken reply. Return JSON {\"kind\": ..., \"say\": ...}." for t in texts]
    return teacher_json(teacher, prompts, schema)


def chat_followups(teacher, n, rng):
    """Two-turn chats ending in a follow-up that only makes sense with the turn before."""
    schema = {"type": "object", "properties": {
        "q1": {"type": "string"}, "a1": {"type": "string"}, "q2": {"type": "string"}, "a2": {"type": "string"},
        "kind2": {"enum": ["chat", "knowledge"]}}, "required": ["q1", "a1", "q2", "a2", "kind2"]}
    topics = ["capitals", "the assistant itself", "feelings", "weather chat", "sports", "food", "animals", "movies",
              "numbers", "planets", "jokes", "weekend plans", "music", "family", "work", "sleep"]
    prompts = [f"{PERSONA}\nWrite a two-turn spoken exchange about {rng.choice(topics)}: q1 (the person), a1 (you), "
               "q2 (a short follow-up that only makes sense after q1, like \"and germany?\", \"why?\", \"another one\", "
               "\"what about you\"), a2 (you). kind2: \"chat\" if a2 is small talk, \"knowledge\" if it needs facts. "
               "Return JSON." for _ in range(n)]
    return [x for x in teacher_json(teacher, prompts, schema, temperature=0.9) if x]


def label_real(teacher, seeds):
    """Real things said to Triton, labelled with the laptop's full rules (no web search on the N95: escalate)."""
    from brain.prompt import converse_prompt
    from tools.schema import json_schema
    schema = {"anyOf": [json_schema(),
                        {"type": "object", "properties": {"say": {"type": "string", "maxLength": 200}},
                         "required": ["say"]},
                        {"type": "object", "properties": {"repeat": {"const": True}}, "required": ["repeat"]}]}
    rules = converse_prompt() + ("\n\nHere there is no web search: for questions needing facts use "
                                 "{\"calls\": [{\"tool\": \"escalate\", \"args\": {}}]}.")
    prompts = [f"{rules}\n\nUser: {(screen_line(*s['screen']) + ' ') if s.get('screen') else ''}{s['text']}\n"
               "Reply with the JSON only." for s in seeds]
    return teacher_json(teacher, prompts, schema, temperature=0.2)


SAME = {"youtube_search": "web_search", "switch_to": "open"}      # near enough: either does the job


def tool_names(reply):
    if not reply:
        return None
    if reply.get("calls"):
        return [SAME.get(c.get("tool"), c.get("tool")) for c in reply["calls"]]
    return ["clarify"] if reply.get("say") else ["repeat"]


def verify_commands(teacher, rows):
    """v3: the teacher labels each round-2 command again (laptop rules); keep the row only if both agree on the tools.
    Round-2 labels come from code, but its sentences are teacher paraphrases that sometimes drifted from them."""
    second = cached("verify", lambda: label_real(teacher, [{"text": r["messages"][-1]["content"]} for r in rows]))
    keep, dropped = [], []
    for r, t in zip(rows, second):
        (keep if t is None or tool_names(t) == tool_names(r["reply"]) else dropped).append((r, t))
    print(f"  checked {len(rows)} commands: kept {len(keep)}, dropped {len(dropped)} "
          f"({100 * len(dropped) / max(1, len(rows)):.0f}%)", flush=True)
    for r, t in dropped[:12]:
        print(f"    drop {r['messages'][-1]['content'][:60]!r}: {tool_names(r['reply'])} vs teacher {tool_names(t)}")
    return [r for r, _ in keep]


def tv_chatter(teacher, rng, keep=3000):
    lists = teacher.lists([CHATTER.format(h=CHATTER_HINTS[i % len(CHATTER_HINTS)]) for i in range(keep * 3 // 80)], 40)
    seen, items = set(), []
    for lst in lists:
        for s in lst or []:
            if norm(s) and norm(s) not in seen:
                seen.add(norm(s))
                items.append(s)
    rng.shuffle(items)
    return items[:keep]


# ---------- code-written rows ----------

def screen_rows(rng, pools):
    rows = []
    for _ in range(2500):                      # click a name that's on screen, said a bit wrong or in lower case
        title, names = rng.choice(SCREENS)
        if "Who's watching?" in names:
            names = ["Who's watching?"] + rng.sample(PROFILE_NAMES, rng.randint(2, 5)) + ["Manage Profiles"]
        target = rng.choice([n for n in names if not n.endswith("?")])
        heard = target.lower()
        if rng.random() < 0.35 and len(heard) > 4:   # a speech-recognition slip in the name
            i = rng.randrange(1, len(heard) - 1)
            heard = heard[:i] + rng.choice("aeiounstrk") + heard[i + 1:]
        text = rng.choice(CLICK_FORMS).format(t=heard)
        rows.append(row([user(stt_style(text, rng), (title, names))], {"calls": [call("click", target=target)]},
                        "screen-click"))
    for _ in range(800):                       # "the first profile", "the last one"
        names = rng.sample(PROFILE_NAMES, rng.randint(2, 5))
        word, i = rng.choice(ORDINAL_WORDS)
        if i >= len(names):
            continue
        text = rng.choice(["select the {w} profile", "pick the {w} one", "click the {w} profile", "choose the {w} one",
                           "can you select the {w} profile here", "open the {w} profile"]).format(w=word)
        rows.append(row([user(text, ("Netflix - Google Chrome", ["Who's watching?"] + names + ["Manage Profiles"]))],
                        {"calls": [call("click", target=names[i])]}, "screen-ordinal"))
    for _ in range(1500):                      # "search X here": the site in front, never a YouTube search elsewhere
        title, names = rng.choice([s for s in SCREENS if s[0] not in ("Settings", "Triton Launcher")])
        q = rng.choice(pools["youtube_queries"] if "YouTube" in title else pools["web_queries"])
        rows.append(row([user(rng.choice(HERE_SEARCH).format(q=q), (title, names))],
                        {"calls": [call("web_search", query=q)]}, "screen-search"))
    return rows


def junk_rows(rng, pools):
    rows = []
    misheard = [w for ws in MISHEARD.values() for w in ws]
    for _ in range(1500):                      # word salad and stuck words
        words = rng.sample(misheard + pools["click_targets"][:200], rng.randint(1, 4))
        if rng.random() < 0.2:
            words = [rng.choice(words)] * rng.randint(3, 5)
        rows.append(row([user(" ".join(words))], {"repeat": True}, "junk"))
    for h in HALF:                              # half a sentence, trailing off
        for _ in range(15):
            rows.append(row([user(stt_style(rng.choice(["", "um ", "so "]) + h, rng))], {"repeat": True}, "half"))
    return rows


def command_followups(rng, pools, n=2500):
    """A command, Triton's spoken reply, then a follow-up that needs it ("more", "search lofi there")."""
    rows = []
    for _ in range(n):
        q = rng.choice(pools["youtube_queries"])
        k = rng.random()
        if k < 0.4:
            turns = [user("open youtube"), said("Opening YouTube.")]
            text = rng.choice(["search for {q} there", "now find {q}", "look up {q}", "and search {q}"]).format(q=q)
            reply, screen = {"calls": [call("web_search", query=q)]}, SCREENS[2]
        elif k < 0.7:
            turns = [user(rng.choice(["turn it up", "louder", "volume up"])), said("Turning it up.")]
            text, reply, screen = rng.choice(["more", "a bit more", "louder", "again", "even more"]), \
                {"calls": [call("change_volume", direction="up")]}, None
        else:
            turns = [user(f"search {q}"), said(f"Searching for {q}.")]
            title = q.title()
            text = rng.choice(["play the first one", "click the first result", "open the first video"])
            reply, screen = {"calls": [call("click", target=title)]}, \
                ("YouTube - Google Chrome", ["Home", "Search", "Filters", title, "Shorts"])
        rows.append(row(turns + [user(stt_style(text, rng), screen)], reply, "followup"))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teacher", default="Qwen3-30B-A3B-Instruct-2507-FP8")
    ap.add_argument("--val", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--v3", action="store_true", help="Triton-Mini v3 data (data_v3/): see the top of this file")
    a = ap.parse_args()
    global OUT
    if a.v3:
        OUT = WORK / "data_v3"
    rng = random.Random(a.seed)
    OUT.mkdir(parents=True, exist_ok=True)
    v1 = read_jsonl(V1 / "train.jsonl") + read_jsonl(V1 / "val.jsonl")
    pools = json.loads((V1 / "pools.json").read_text())
    eval_texts = {norm(r["text"]) for r in read_jsonl(CODE / "bench" / "eval" / "real.jsonl")} | \
                 {norm(r["text"]) for r in read_jsonl(CODE / "bench" / "eval" / "eval.jsonl")}
    if (CODE / "bench" / "eval" / "n95.jsonl").exists():
        eval_texts |= {norm(r["text"]) for r in read_jsonl(CODE / "bench" / "eval" / "n95.jsonl")}
    seeds_path = CODE / "training" / "real_seeds.jsonl"
    real = [s for s in read_jsonl(seeds_path) if norm(s["text"]) not in eval_texts] if seeds_path.exists() else []
    print(f"== round-2 rows {len(v1)}, real seeds {len(real)}", flush=True)

    print("== loading teacher", a.teacher, flush=True)
    teacher = Teacher(model_path(a.teacher))
    commands = [r for r in v1 if r["calls"][0]["tool"] != "escalate"]
    chats = [r["text"] for r in v1 if r["calls"][0]["tool"] == "escalate"]
    print(f"== 1/4 sorting {len(chats)} chat sentences", flush=True)
    sorted_chat = cached("chat", lambda: sort_chat(teacher, chats))
    print("== 2/4 TV chatter", flush=True)
    tv = cached("chatter", lambda: tv_chatter(teacher, rng))
    print("== 3/4 chat follow-ups", flush=True)
    follow = cached("followups", lambda: chat_followups(teacher, 2500, rng))
    print(f"== 4/4 labelling {len(real)} real sentences", flush=True)
    real_labels = cached("real", lambda: label_real(teacher, real)) if real else []

    print("== assembling", flush=True)
    rows = []
    for r in commands:
        c = r["calls"]
        tools = {x["tool"] for x in c}
        screen = rng.choice(SCREENS) if rng.random() < 0.3 and not tools & {"click", "web_search", "clarify"} else None
        reply = {"say": c[0]["args"]["question"]} if c[0]["tool"] == "clarify" else {"calls": c}
        rows.append(row([user(r["text"], screen)], reply, "command"))
    if a.v3:
        print("== v3: checking round-2 commands with the teacher", flush=True)
        rows = verify_commands(teacher, rows)
    command_rows = list(rows)
    fake = 0

    def honest(say):                              # v3: a spoken reply never claims an action
        nonlocal fake
        if a.v3 and says_it_acts(say):
            fake += 1
            return False
        return True

    n_chat = n_know = 0
    for text, lab in zip(chats, sorted_chat):
        if not lab:
            continue
        if lab["kind"] == "chat":
            if honest(lab["say"]):
                screen = rng.choice(SCREENS) if a.v3 and rng.random() < 0.4 else None
                rows.append(row([user(text, screen)], {"say": lab["say"]}, "chat"))
            n_chat += 1
        else:
            rows.append(row([user(text)], {"calls": [call("escalate")]}, "knowledge"))
            n_know += 1
    for text in tv:
        rows.append(row([user(stt_style(text, rng), rng.choice(SCREENS) if rng.random() < 0.5 else None)],
                        {"repeat": True}, "chatter"))
    for f in follow:
        last = {"say": f["a2"]} if f["kind2"] == "chat" else {"calls": [call("escalate")]}
        if "say" not in last or honest(last["say"]):
            rows.append(row([user(f["q1"]), said(f["a1"]), user(stt_style(f["q2"], rng))], last, "chat-followup"))
    rows += screen_rows(rng, pools) + junk_rows(rng, pools) + command_followups(rng, pools)
    if a.v3:
        extra = all_rows(rng, pools, SCREENS, command_rows)
        print(f"== v3: {len(extra)} contrast rows; {fake} chat replies dropped for claiming an action", flush=True)
        rows += extra
    for s, lab in zip(real, real_labels):
        if not lab:
            continue
        lab = {k: v for k, v in lab.items() if k in ("calls", "say", "repeat")} or {"calls": [call("escalate")]}
        if "say" in lab and not honest(lab["say"]):
            continue
        screen = tuple(s["screen"]) if s.get("screen") else None
        for i in range(3):                       # x3: what was actually said matters most
            text = s["text"] if i == 0 else (add_noise(s["text"], [], rng) or stt_style(s["text"], rng))
            rows.append(row([user(text, screen)], lab, "real"))

    seen, out = set(), []
    for r in rows:
        key = json.dumps(r["messages"])
        last = r["messages"][-1]["content"].split("] ", 1)[-1]
        if key not in seen and norm(last) not in eval_texts:
            seen.add(key)
            out.append(r)
    rng.shuffle(out)
    n_val = int(len(out) * a.val)
    write_jsonl(OUT / "val.jsonl", out[:n_val])
    write_jsonl(OUT / "train.jsonl", out[n_val:])
    kinds = {}
    for r in out:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"  chat {n_chat}, knowledge {n_know}; train {len(out) - n_val}, val {n_val}: "
          + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    for r in out[:15]:
        print(f"    {r['messages'][-1]['content'][:90]!r} -> {json.dumps(r['reply'])[:110]}")


if __name__ == "__main__":
    main()
