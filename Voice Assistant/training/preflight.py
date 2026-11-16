"""Checks, on the login node, everything the GPU jobs need, so they don't fail hours in."""

import inspect
import os
import sys

from common import CODE, MODELS, build_prompt, completion, json_schema, structured

ok = True


def check(what, fn):
    global ok
    try:
        note = fn()
        print(f"  OK   {what}" + (f": {note}" if note else ""))
    except Exception as e:  # noqa: BLE001 - report everything, then fail once at the end
        ok = False
        print(f"  FAIL {what}: {type(e).__name__}: {e}")


def versions():
    import torch, transformers, trl, vllm  # noqa: E401
    return (f"torch {torch.__version__}, transformers {transformers.__version__}, "
            f"trl {trl.__version__}, vllm {vllm.__version__}")


def sft_config():
    from trl import SFTConfig
    fields = inspect.signature(SFTConfig).parameters
    missing = [f for f in ("max_length", "completion_only_loss") if f not in fields]
    assert not missing, f"SFTConfig lacks {missing}; sft.py needs updating for this TRL"


def structured_outputs():
    structured(json_schema(), temperature=0, max_tokens=8)


def models():
    names = os.environ.get("MODEL_NAMES", "").split()
    missing = [n for n in names if not (MODELS / n.split("/")[-1] / "config.json").exists()]
    assert not missing, f"not downloaded: {missing}"
    return ", ".join(n.split("/")[-1] for n in names)


def prompt_format():
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(MODELS / "Qwen3-1.7B")
    p = build_prompt(tok, "volume up")
    assert p.rstrip().endswith("</think>"), "thinking isn't switched off in the prompt"
    n = len(tok(p + completion([{"tool": "mute", "args": {}}]))["input_ids"])
    return f"{n} tokens for a short example"


def eval_set():
    n = sum(1 for _ in open(CODE / "bench" / "eval" / "eval.jsonl", encoding="utf-8"))
    return f"{n} items"


check("libraries", versions)
check("TRL SFTConfig fields", sft_config)
check("vLLM structured outputs", structured_outputs)
check("models downloaded", models)
check("prompt format", prompt_format)
check("eval set uploaded", eval_set)
if not ok:
    sys.exit("Pre-flight FAILED (see above)")
print("Pre-flight PASSED")
