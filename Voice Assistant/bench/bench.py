r"""
Benchmark: how well and how fast does a model turn requests into tool calls, on this device?

    .venv\Scripts\python.exe bench\bench.py rules
    .venv\Scripts\python.exe bench\bench.py llama C:\Users\shash\TritonModels\models\Qwen3-0.6B-Q4_K_M.gguf [more.gguf ...]
        --threads 4      CPU threads (default: physical cores; the N95 has 4)
        --limit 50       only the first N items (quick check)
        --prompt compact one-line prompt, for fine-tuned models (default "full": tools + examples)

"rules" scores today's commands.py (tier 1 of the router): what it can't place goes to the model.
"llama" starts llama.cpp's llama-server for each model and forces every answer into the tool-call
grammar (tools/schema.py), so a reply is always valid JSON; what's measured is whether it's right.

Per run: bench/results/<time>_<device>_<model>.jsonl (every item) and one row in results.csv.
Latency is the whole request; prefill = reading the prompt, decode = writing the answer. The first
(unscored) request reads the full prompt cold; after that llama.cpp reuses the cached system prompt,
as the assistant would, so each request only reads the user's words.
"""

import argparse
import csv
import datetime
import glob
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import time
import winreg
from pathlib import Path

import psutil
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scoring import score  # noqa: E402  (bench/ is on the path as the script's folder)
from brain.prompt import system_prompt  # noqa: E402
from tools.schema import json_schema  # noqa: E402

BENCH = Path(__file__).resolve().parent
EVAL = BENCH / "eval" / "eval.jsonl"
RESULTS = BENCH / "results"
# Models and llama.cpp live outside OneDrive (gigabytes that shouldn't sync)
STORE = Path(os.environ.get("TRITON_MODELS", Path.home() / "TritonModels"))
LLAMA_SERVER = STORE / "llama.cpp" / "llama-server.exe"


# ---------- runners ----------

class Rules:
    """Today's commands.py, mapped onto the new tools."""

    def __init__(self):
        from commands import Commands
        self.commands = Commands(catalog=None, monitor=None)

    name, quant, size_mb, peak_ram_mb, cold = "rules (commands.py)", "-", 0, 0, {}

    def ask(self, text):
        from brain.rules import to_calls
        t0 = time.perf_counter()
        cmd, req = self.commands.understand(text)
        calls = to_calls(cmd, req) if cmd else []
        return calls, {"wall_ms": (time.perf_counter() - t0) * 1000}

    def close(self):
        pass


class LlamaServer:
    """One GGUF model in llama.cpp's llama-server, answers forced into the tool-call grammar."""

    def __init__(self, model, threads, prompt="full", ctx=4096):
        self.model = Path(model)
        self.name = self.model.stem
        m = re.search(r"(I?Q\d\w*|MXFP4|F16|BF16|F32)$", self.name, re.I)
        self.quant = m.group(1).upper() if m else "?"
        self.size_mb = round(self.model.stat().st_size / 2**20)
        self.system = system_prompt(prompt)
        self.schema = json_schema()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        log = RESULTS / "llama-server.log"
        self.proc = subprocess.Popen(
            [str(LLAMA_SERVER), "-m", str(self.model), "-t", str(threads), "-c", str(ctx), "-np", "1",
             "--port", str(self.port), "--jinja", "--no-webui"],
            stdout=log.open("w"), stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        self._wait_ready()
        _, self.cold = self.ask("hello")          # warm-up: reads the whole prompt once (cold prefill)

    def _wait_ready(self, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited; see {RESULTS / 'llama-server.log'}")
            try:
                if requests.get(self.url + "/health", timeout=2).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.5)
        raise RuntimeError("llama-server didn't start in time")

    def ask(self, text):
        body = {
            "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": text}],
            "temperature": 0, "max_tokens": 200, "cache_prompt": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "calls", "schema": self.schema}},
            # Qwen3 reads enable_thinking; gpt-oss always thinks first, reasoning_effort keeps it short
            "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "low"},
        }
        t0 = time.perf_counter()
        r = requests.post(self.url + "/v1/chat/completions", json=body, timeout=600)
        wall = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        data = r.json()
        t = data.get("timings", {})
        stats = {"wall_ms": wall, "prompt_n": t.get("prompt_n"), "prompt_ms": t.get("prompt_ms"),
                 "predicted_n": t.get("predicted_n"), "predicted_ms": t.get("predicted_ms")}
        content = data["choices"][0]["message"]["content"]
        try:
            calls = json.loads(content)["calls"]
        except (ValueError, KeyError, TypeError):
            calls = None
        stats["raw"] = content
        return calls, stats

    @property
    def peak_ram_mb(self):
        mem = psutil.Process(self.proc.pid).memory_info()
        return round(getattr(mem, "peak_wset", mem.rss) / 2**20)

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---------- running ----------

def device_name():
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0]
    except OSError:
        cpu = platform.processor()
    cpu = re.sub(r"\(R\)|\(TM\)|CPU|@.*|\d+th Gen|Intel|Core", "", cpu)
    return f"{platform.node()} ({' '.join(cpu.split())})"


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def run(runner, items, threads, label):
    started = datetime.datetime.now()
    rows, by_cat = [], {}
    print(f"\n== {runner.name} [{runner.quant}] on {device_name()}, {threads} threads, {len(items)} items")
    for i, item in enumerate(items, 1):
        calls, stats = runner.ask(item["text"])
        valid = isinstance(calls, list)
        ok, tool_ok = score(item, calls) if valid else (False, False)
        rows.append({"id": item["id"], "category": item["category"], "text": item["text"], "ok": ok,
                     "tool_ok": tool_ok, "valid": valid, "got": calls, "expected": item["expected"], **stats})
        by_cat.setdefault(item["category"], []).append(ok)
        if i % 25 == 0:
            print(f"  {i}/{len(items)}  accuracy so far {sum(r['ok'] for r in rows) / i:.0%}", flush=True)

    def rate(key):
        return sum(r[key] for r in rows) / len(rows)

    decode = [r["predicted_n"] / r["predicted_ms"] * 1000 for r in rows if r.get("predicted_ms")]
    cold = runner.cold or {}
    summary = {
        "timestamp": started.isoformat(timespec="seconds"), "device": device_name(), "label": label,
        "model": runner.name, "quant": runner.quant, "size_mb": runner.size_mb, "threads": threads,
        "items": len(rows), "accuracy": round(rate("ok"), 3), "tool_accuracy": round(rate("tool_ok"), 3),
        "valid_rate": round(rate("valid"), 3),
        "latency_p50_ms": round(pct([r["wall_ms"] for r in rows], 0.5)),
        "latency_p90_ms": round(pct([r["wall_ms"] for r in rows], 0.9)),
        "cold_prompt_tokens": cold.get("prompt_n"),
        "cold_prefill_ms": round(cold["prompt_ms"]) if cold.get("prompt_ms") else None,
        "prefill_tok_s": round(cold["prompt_n"] / cold["prompt_ms"] * 1000, 1) if cold.get("prompt_ms") else None,
        "warm_prompt_tokens_p50": pct([r.get("prompt_n") for r in rows], 0.5),
        "decode_tok_s": round(statistics.median(decode), 1) if decode else None,
        "peak_ram_mb": runner.peak_ram_mb,
        **{f"acc_{c}": round(sum(v) / len(v), 2) for c, v in sorted(by_cat.items())},
    }

    RESULTS.mkdir(exist_ok=True)
    slug = re.sub(r"\W+", "-", f"{started:%Y%m%d-%H%M%S}_{platform.node()}_{runner.name}").strip("-")
    with (RESULTS / f"{slug}.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"summary": summary}) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")
    table = RESULTS / "results.csv"
    old = list(csv.DictReader(table.open(encoding="utf-8"))) if table.exists() else []
    fields = list(dict.fromkeys([*(old[0].keys() if old else []), *summary]))
    with table.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows([*old, summary])

    print(f"  accuracy {summary['accuracy']:.1%} (right tools {summary['tool_accuracy']:.1%}), "
          f"latency p50 {summary['latency_p50_ms']} ms / p90 {summary['latency_p90_ms']} ms, "
          f"peak RAM {summary['peak_ram_mb']} MB")
    if summary["prefill_tok_s"]:
        print(f"  cold prompt {summary['cold_prompt_tokens']} tokens in {summary['cold_prefill_ms']} ms "
              f"({summary['prefill_tok_s']} tok/s), decode {summary['decode_tok_s']} tok/s")
    print("  by category: " + ", ".join(f"{c} {sum(v) / len(v):.0%}" for c, v in sorted(by_cat.items())))
    misses = [r for r in rows if not r["ok"]][:8]
    for r in misses:
        print(f"    miss {r['id']}: {r['text']!r} -> {json.dumps(r['got'])}")
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runtime", choices=["rules", "llama"])
    ap.add_argument("models", nargs="*", help="GGUF files (llama)")
    ap.add_argument("--threads", type=int, default=psutil.cpu_count(logical=False))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--prompt", choices=["full", "noshot", "compact"], default="full",
                    help="full: tools + examples; noshot: tools only; compact: one line (fine-tuned models)")
    ap.add_argument("--label", default="baseline")
    a = ap.parse_args()

    items = [json.loads(line) for line in EVAL.open(encoding="utf-8")]
    if a.limit:
        items = items[:a.limit]
    RESULTS.mkdir(exist_ok=True)
    if a.runtime == "rules":
        run(Rules(), items, a.threads, a.label)
        return
    if not LLAMA_SERVER.exists():
        sys.exit(f"llama-server not found at {LLAMA_SERVER}; run bench\\fetch_models.py first")
    models = [p for m in a.models for p in (sorted(glob.glob(m)) or [m])]   # PowerShell doesn't expand *.gguf
    for m in models:
        runner = LlamaServer(m, a.threads, prompt=a.prompt)
        try:
            run(runner, items, a.threads, a.label if a.prompt == "full" else f"{a.label}-{a.prompt}")
        finally:
            runner.close()


if __name__ == "__main__":
    main()
