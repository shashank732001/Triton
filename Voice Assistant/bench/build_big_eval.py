r"""
Builds bench/eval/big.jsonl: a ~1000-item test for real_bench.py, in real.jsonl's format, each item tagged "part":

    .venv\Scripts\python.exe bench\build_big_eval.py [--val C:\Users\shash\TritonModels\trained\val_v2.jsonl]

    real   bench/eval/real.jsonl, all 49     your own N95 sentences, labelled by hand (most honest)
    v1     bench/eval/eval.jsonl, all 229    commands written for the first eval (never trained on)
    v2     sampled from val_v2.jsonl         the 3% of Triton-Mini v2's data held out of training (gen_converse.py):
                                             every kind except plain commands is kept whole (chat, TV chatter,
                                             garble, screen clicks, follow-ups, knowledge); commands fill up to
                                             --total. Same style as the training data, so it favours fine-tunes.
Forms (what real_bench scores): calls -> act (escalate -> say, clarify -> say or repeat), say -> say,
repeat -> repeat. Never-tools (the dangerous kind of wrong) for items that must not act: power, lock, close.
"""

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "bench" / "eval"
SCREEN = re.compile(r"^\[screen: (.*?)\]\s*(.*)$", re.S)
NEVER = ["power", "lock", "close"]


def norm(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def expectation(calls):
    """(expect, expected calls, also_ok_forms) for a list of expected calls."""
    tool = calls[0]["tool"] if calls else ""
    if tool == "escalate":
        return "say", [], []
    if tool == "clarify":
        return "say", [], ["repeat"]           # asking what they mean, or asking them to say it again
    return "act", calls, []


def from_val(row, i):
    *before, last = row["messages"]
    text, screen = last["content"], ""
    m = SCREEN.match(text)
    if m:
        screen, text = m.group(1), m.group(2)
    history = []
    for u, a in zip(before[0::2], before[1::2]):
        said = json.loads(a["content"]).get("say", "") if a["content"].startswith("{") else a["content"]
        history.append({"user": u["content"], "reply": said})
    reply = row["reply"]
    if "calls" in reply:
        expect, expected, also_forms = expectation(reply["calls"])
    else:
        expect, expected, also_forms = ("say" if "say" in reply else "repeat"), [], []
    item = {"id": f"v2-{row['kind']}-{i:04d}", "part": "v2", "kind": row["kind"], "text": text, "screen": screen,
            "expect": expect, "expected": expected, "history": history}
    if also_forms:
        item["also_ok_forms"] = also_forms
    if expect != "act":
        item["never"] = NEVER
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--val", type=Path, default=Path.home() / "TritonModels" / "trained" / "val_v2.jsonl")
    ap.add_argument("--total", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    real = [json.loads(l) for l in open(EVAL / "real.jsonl", encoding="utf-8") if l.strip()]
    for r in real:
        r["part"] = "real"
    v1 = []
    for r in (json.loads(l) for l in open(EVAL / "eval.jsonl", encoding="utf-8") if l.strip()):
        expect, expected, also_forms = expectation(r["expected"])
        item = {"id": f"v1-{r['id']}", "part": "v1", "kind": r["category"], "text": r["text"], "screen": "",
                "expect": expect, "expected": expected, "also_ok": r.get("also_ok", []) if expect == "act" else []}
        if also_forms:
            item["also_ok_forms"] = also_forms
        if expect != "act":
            item["never"] = NEVER
        v1.append(item)

    seen = {norm(r["text"]) for r in real + v1}
    rows = [r for r in (json.loads(l) for l in open(a.val, encoding="utf-8") if l.strip())
            if norm(r["messages"][-1]["content"]) not in seen]
    rng = random.Random(a.seed)
    rare = [r for r in rows if r["kind"] != "command"]
    commands = [r for r in rows if r["kind"] == "command"]
    n_cmd = max(0, a.total - len(real) - len(v1) - len(rare))
    picked = rare + rng.sample(commands, min(n_cmd, len(commands)))
    v2 = [from_val(r, i) for i, r in enumerate(picked)]

    items = real + v1 + v2
    out = EVAL / "big.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(f"{out}: {len(items)} items")
    print("  parts:", dict(Counter(i["part"] for i in items)))
    print("  forms:", dict(Counter(i["expect"] for i in items)))
    print("  v2 kinds:", dict(Counter(i["kind"] for i in v2)))


if __name__ == "__main__":
    main()
