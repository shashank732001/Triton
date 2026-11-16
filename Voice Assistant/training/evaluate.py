r"""
Scores a model on the eval set (and a slice of val.jsonl) on the GPU, with the same scoring as the
laptop's bench.py, so you know a run is good before downloading it.

    python evaluate.py $WORK_DIR/runs/Qwen3-1.7B/final           # a fine-tuned model (compact prompt)
    python evaluate.py Qwen/Qwen3-1.7B --prompt full              # the stock model, for comparison

GPU accuracy should match the laptop's within a point or two; quantizing on the laptop then costs a
little more, which bench.py measures.
"""

import argparse
import json
from pathlib import Path

from transformers import AutoTokenizer
from vllm import LLM

from common import CODE, DATA, RUNS, build_prompt, json_schema, model_path, read_jsonl, structured, write_jsonl
from scoring import score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--prompt", choices=["compact", "full"], default="compact")
    ap.add_argument("--val-items", type=int, default=1000)
    a = ap.parse_args()

    path = model_path(a.model)
    tok = AutoTokenizer.from_pretrained(path)
    llm = LLM(model=str(path), max_model_len=4096, gpu_memory_utilization=0.85)
    params = structured(json_schema(), temperature=0, max_tokens=200)

    sets = {"eval": read_jsonl(CODE / "bench" / "eval" / "eval.jsonl")}
    if (DATA / "val.jsonl").exists():
        sets["val"] = [{"id": f"val-{i}", "category": r["kind"], "text": r["text"], "expected": r["calls"],
                        "also_ok": []} for i, r in enumerate(read_jsonl(DATA / "val.jsonl")[:a.val_items])]

    name = Path(path).name if Path(path).name != "final" else Path(path).parent.name
    for set_name, items in sets.items():
        outs = llm.generate([build_prompt(tok, it["text"], a.prompt) for it in items], params)
        rows, cats = [], {}
        for it, o in zip(items, outs):
            try:
                calls = json.loads(o.outputs[0].text)["calls"]
                ok, tool_ok = score(it, calls)
            except (ValueError, KeyError, TypeError):
                calls, ok, tool_ok = None, False, False
            rows.append({"id": it["id"], "category": it["category"], "text": it["text"], "ok": ok,
                         "tool_ok": tool_ok, "got": calls, "expected": it["expected"]})
            cats.setdefault(it["category"], []).append(ok)
        acc = sum(r["ok"] for r in rows) / len(rows)
        tools = sum(r["tool_ok"] for r in rows) / len(rows)
        print(f"\n== {name} on {set_name} ({len(rows)} items, {a.prompt} prompt): "
              f"accuracy {acc:.1%}, right tools {tools:.1%}")
        print("  " + ", ".join(f"{c} {sum(v) / len(v):.0%}" for c, v in sorted(cats.items())))
        for r in [r for r in rows if not r["ok"]][:10]:
            print(f"    miss {r['id']}: {r['text']!r} -> {json.dumps(r['got'])}")
        write_jsonl(RUNS / f"eval_{name}_{set_name}_{a.prompt}.jsonl", rows)


if __name__ == "__main__":
    main()
