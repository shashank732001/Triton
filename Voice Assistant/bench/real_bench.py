r"""
The N95's own decisions on real commands (bench/eval/real.jsonl: what you actually said, mishearings and TV
chatter included), without the laptop. Runs the assistant's real _understand(): rules, part-by-part placing,
then the small model.

    .venv\Scripts\python.exe bench\real_bench.py [--model path\to.gguf]

Each item expects one form:
    act     do these calls (expected / also_ok)
    say     talk back (here: the small model handed it on with "escalate"; the N95 alone can't talk yet)
    repeat  ask again / do nothing (mishearings, TV speech)
A "never" tool firing is counted separately: the dangerous kind of wrong.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))

import assistant as A                       # noqa: E402  (the real decision code)
from apps import Catalog                    # noqa: E402
from brain.llm import LocalLLM              # noqa: E402
from brain.router import Router             # noqa: E402
from commands import Commands               # noqa: E402
from scoring import score                   # noqa: E402
import screen as S                          # noqa: E402


class _Quiet:
    def __getattr__(self, name):
        return lambda *a, **k: None


def form_of(result):
    kind, payload, _ = result
    if kind == "act":
        tools = [c["tool"] for c in payload]
        if tools and tools[0] == "escalate":
            return "say", []
        if tools and tools[0] == "clarify":
            return "repeat", []
        return "act", payload
    if kind == "partial":
        return "act", payload[0]
    if kind == "say":
        return "say", []
    if kind == "snooze":
        return "snooze", []
    return "repeat", []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=A.SETTINGS.get("brain", {}).get("model"))
    ap.add_argument("--eval", default=str(ROOT / "bench" / "eval" / "real.jsonl"))
    ap.add_argument("--threads", type=int, help="llama-server threads (the N95 runs 4)")
    ap.add_argument("--prompt", help="override, e.g. converse-full for an untrained base model")
    ap.add_argument("--checkpoints", action="store_true", help="hybrid models (Qwen3.5, LFM3): keep the prompt cached")
    ap.add_argument("--quiet", action="store_true", help="print only misses")
    ap.add_argument("--out", help="write every item's result here (JSON lines)")
    ap.add_argument("--no-doubt", action="store_true", help="trust the model: skip assistant._doubtful (experiment)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    catalog = Catalog(A.SHORTCUTS_FILE)
    catalog.ready.wait(60)
    me = A.Assistant.__new__(A.Assistant)
    me.overlay, me.remote, me.only_wake, me.pending_click = _Quiet(), None, False, None
    me.commands = Commands(catalog, A.monitor)
    if a.no_doubt:
        me._doubtful = lambda calls, text: False
    cfg ={**A.SETTINGS.get("brain", {}), "model": a.model}
    cfg.pop("prompt", None)                 # chosen by the model's name (converse = Triton-Mini v2)
    if a.threads:
        cfg["threads"] = a.threads
    if a.prompt:
        cfg["prompt"] = a.prompt
    cfg["checkpoints"] = a.checkpoints
    me.llm = LocalLLM.from_settings(cfg, A.BASE_DIR)
    me.llm.ready.wait(300)
    model_times = []                        # the model's own time per question it was asked (rules cost ~0)
    for name in ("_ask", "_decide"):
        def timed(*args, _f=getattr(me.llm, name), **kw):
            t = time.perf_counter()
            try:
                return _f(*args, **kw)
            finally:
                model_times.append(time.perf_counter() - t)
        setattr(me.llm, name, timed)
    me.router = Router(me.commands, catalog, me.llm, backup=False)

    items = [json.loads(line) for line in open(a.eval, encoding="utf-8") if line.strip()]
    right = danger = 0
    times = []
    parts = {}                              # part -> [right, n]  ("real", "v1", "v2"; bench/eval/big.jsonl)
    by_model = [0, 0]                       # right, n: items the model had to decide (the rules didn't place them)
    results = []
    for it in items:
        title, _, names = (it.get("screen") or "").partition(" | on screen: ")
        me.screen = S.Screen(title, [n.strip() for n in names.split(",") if n.strip()])
        me._local_history().clear()
        me._local_history().extend(it.get("history", []))   # earlier turns, for follow-ups ("and germany?")
        t0 = time.perf_counter()
        asked_before = len(model_times)
        form, calls = form_of(me._understand(it["text"], it["text"]))
        times.append(time.perf_counter() - t0)
        ok_forms = [it["expect"], *it.get("also_ok_forms", [])]
        if form == "act" and it["expect"] == "act":
            # App names count as the app they open ("crome" opens Chrome, as at runtime through catalog.find)
            def canon(cs):
                out = []
                for c in cs:
                    c = {"tool": c["tool"], "args": dict(c.get("args") or {})}
                    if c["tool"] in ("open", "switch_to", "close") and c["args"].get("target"):
                        t = catalog.find(str(c["args"]["target"]))
                        c["args"]["target"] = t.name.lower() if t else c["args"]["target"]
                    out.append(c)
                return out
            item = {**it, "expected": canon(it["expected"]), "also_ok": [canon(o) for o in it.get("also_ok", [])]}
            ok = score(item, canon(calls))[0]
        else:
            ok = form in ok_forms or (form == "act" and any(f == "act:" + calls[0]["tool"] for f in ok_forms if calls))
        bad = form == "act" and any(c["tool"] in it.get("never", []) for c in calls)
        right += ok
        danger += bad
        asked = len(model_times) > asked_before
        p = parts.setdefault(it.get("part", "real"), [0, 0])
        p[0] += ok
        p[1] += 1
        if asked:
            by_model[0] += ok
            by_model[1] += 1
        results.append({"id": it["id"], "ok": bool(ok), "bad": bool(bad), "form": form, "calls": calls,
                        "model": asked, "s": round(times[-1], 3)})
        if a.quiet and ok and not bad:
            continue
        print(f"{'ok ' if ok else 'MISS'}{' DANGER' if bad else ''} {it['id']} {it['text'][:50]!r:52} -> {form} "
              f"{json.dumps(calls)[:110] if calls else ''}")
    n = len(items)
    times.sort()
    print(f"\n{Path(a.model).name}: {right}/{n} right ({100 * right / n:.0f}%), {danger} dangerous, "
          f"decision p50 {times[n // 2]:.2f}s p90 {times[int(n * 0.9)]:.2f}s")
    if len(parts) > 1:
        print("  " + ", ".join(f"{k} {r}/{t} ({100 * r / t:.0f}%)" for k, (r, t) in parts.items()))
    if by_model[1]:
        print(f"  decided by the model: {by_model[0]}/{by_model[1]} ({100 * by_model[0] / by_model[1]:.0f}%)")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
    if model_times:
        m = sorted(model_times)
        print(f"  model asked {len(m)}x: p50 {m[len(m) // 2]:.2f}s p90 {m[int(len(m) * 0.9)]:.2f}s max {m[-1]:.2f}s")
    me.llm.close()


if __name__ == "__main__":
    main()
