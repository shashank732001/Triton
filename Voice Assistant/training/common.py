"""
Shared by gen_data.py, sft.py and evaluate.py: where things are, and the exact text a model sees.

Training and running must format requests identically, or the fine-tuned model sees something it
never trained on. Prompt = the chat template with the one-line COMPACT system prompt and thinking
switched off (what llama-server sends with --jinja and enable_thinking=false); completion = the
calls as compact JSON, then the end-of-turn token.
"""

import json
import os
import sys
from pathlib import Path

CODE = Path(os.environ.get("TRITON_CODE", Path(__file__).resolve().parent.parent))
WORK = Path(os.environ.get("WORK_DIR", CODE.parent))
MODELS = WORK / "models"
DATA = WORK / "data"
RUNS = WORK / "runs"

sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "bench"))
from brain.prompt import system_prompt  # noqa: E402
from tools.schema import json_schema  # noqa: E402


def model_path(name):
    """'Qwen/Qwen3-1.7B', 'Qwen3-1.7B' or a full path -> the local folder."""
    p = Path(name)
    return p if p.exists() else MODELS / name.split("/")[-1]


def build_prompt(tokenizer, text, kind="compact"):
    """kind "full" is for scoring a stock model the way bench.py does on the laptop."""
    messages = [{"role": "system", "content": system_prompt(kind)}, {"role": "user", "content": text}]
    return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                         enable_thinking=False)


def completion(calls):
    return json.dumps({"calls": calls}, separators=(",", ":"))


def build_converse_prompt(tokenizer, messages):
    """Triton-Mini v2 (gen_converse.py rows): COMPACT_CONVERSE, earlier turns, then what was just said, exactly as
    brain/llm.py decide() sends it at run time."""
    from brain.prompt import COMPACT_CONVERSE
    return tokenizer.apply_chat_template([{"role": "system", "content": COMPACT_CONVERSE}, *messages], tokenize=False,
                                         add_generation_prompt=True, enable_thinking=False)


def converse_completion(reply):
    return json.dumps(reply, separators=(",", ":"))


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def structured(schema, **kw):
    """vLLM SamplingParams whose output must match `schema` (the API was renamed across versions)."""
    from vllm import SamplingParams
    try:
        from vllm.sampling_params import StructuredOutputsParams
        return SamplingParams(structured_outputs=StructuredOutputsParams(json=schema), **kw)
    except ImportError:
        from vllm.sampling_params import GuidedDecodingParams
        return SamplingParams(guided_decoding=GuidedDecodingParams(json=schema), **kw)
