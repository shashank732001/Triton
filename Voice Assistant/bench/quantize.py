r"""
Compresses a trained model (f16 GGUF from the cluster) into smaller versions with llama.cpp.

    .venv\Scripts\python.exe bench\quantize.py C:\Users\shash\TritonModels\trained\triton-Qwen3-1.7B-f16.gguf
        --types Q8_0 Q5_K_M Q4_K_M Q3_K_M IQ4_XS IQ3_M   (default)
        --val ...\val.jsonl     calibration data for the importance matrix (default: next to the model)

An importance matrix ("imatrix") records which weights matter most on OUR kind of input (requests
and tool calls), so low-bit versions keep those weights more precise. It's computed once from the
f16 model and val.jsonl, then used for every type. Each output is named ...-<TYPE>.gguf, ready for
bench.py --prompt compact.
"""

import argparse
import json
import subprocess
from pathlib import Path

from bench import STORE   # same llama.cpp folder as bench.py

LLAMA = STORE / "llama.cpp"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("f16", type=Path)
    ap.add_argument("--types", nargs="+", default=["Q8_0", "Q5_K_M", "Q4_K_M", "Q3_K_M", "IQ4_XS", "IQ3_M"])
    ap.add_argument("--val", type=Path)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--chunks", type=int, default=100, help="512-token chunks for the imatrix (more = slower, "
                    "barely better; 282 chunks of the chat-format calibration took ~70 min for 1.7B on the laptop)")
    a = ap.parse_args()

    base = a.f16.with_name(a.f16.stem.removesuffix("-f16"))
    val = a.val or a.f16.with_name("val.jsonl")
    # f-strings, not with_suffix: "triton-Qwen3-0.6B" would lose ".6B" as if it were an extension
    imatrix = Path(f"{base}.imatrix")
    if not imatrix.exists():
        calib = Path(f"{base}.calib.txt")
        rows = [json.loads(line) for line in val.open(encoding="utf-8")][:1500]
        if "converse" in a.f16.name:
            # Triton-Mini v2 sees the chat template with COMPACT_CONVERSE (brain/llm.py decide), so calibrate on that
            # shape; v1 val requests (never the eval sets) with their calls as the reply
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from brain.prompt import COMPACT_CONVERSE
            reply = lambda r: json.dumps({"calls": r["calls"]}, separators=(",", ":"))
            text = "\n".join(f"<|im_start|>system\n{COMPACT_CONVERSE}<|im_end|>\n<|im_start|>user\n{r['text']}<|im_end|>\n"
                             f"<|im_start|>assistant\n<think>\n\n</think>\n\n{reply(r)}<|im_end|>" for r in rows)
        else:
            text = "\n".join(f"{r['text']}\n{json.dumps({'calls': r['calls']})}" for r in rows)
        calib.write_text(text, encoding="utf-8")
        print(f"importance matrix from {len(rows)} examples...")
        subprocess.run([str(LLAMA / "llama-imatrix.exe"), "-m", str(a.f16), "-f", str(calib), "-o", str(imatrix),
                        "-t", str(a.threads), "-c", "512", "--chunks", str(a.chunks)], check=True)
    for t in a.types:
        out = Path(f"{base}-{t}.gguf")
        if out.exists():
            print(f"{out.name}: already there")
            continue
        subprocess.run([str(LLAMA / "llama-quantize.exe"), "--imatrix", str(imatrix), str(a.f16), str(out), t,
                        str(a.threads)], check=True, capture_output=True)
        print(f"{out.name}: {out.stat().st_size / 2**20:.0f} MB")
    print(f"\nNext: .venv\\Scripts\\python.exe bench\\bench.py llama \"{base}-*.gguf\" --prompt compact --label finetuned")


if __name__ == "__main__":
    main()
