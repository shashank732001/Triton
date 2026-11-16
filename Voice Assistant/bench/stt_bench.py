r"""
Speech-to-text benchmark: which recogniser should hear the free-form part of a command?

    .venv\Scripts\python.exe bench\stt_bench.py                     # all engines, synthetic speech
    .venv\Scripts\python.exe bench\stt_bench.py --engines vosk moonshine-tiny
    .venv\Scripts\python.exe bench\stt_bench.py --record 20         # record 20 eval sentences in YOUR voice first

Test audio:
  - synthetic: eval sentences (bench/eval/eval.jsonl, minus the deliberately misspelt asr_noise ones)
    spoken by the Piper voices in models/piper, once clean and once with background noise (15 dB SNR).
    Cached in bench/stt_audio/. Easier than real speech, but fair for ranking the engines.
  - real: bench/stt_audio/real/*.wav + same-name .txt (made by --record). These are what really counts.

Engines: vosk (today's free recogniser), Moonshine tiny/base and Whisper tiny/base/small (int8, via
sherpa-onnx, models in %USERPROFILE%\TritonModels\stt). Each runs in its own process so RAM is its own.
Reports WER (word error rate), sentences fully right, median/p90 seconds per sentence, peak RAM.
Results: bench/results/stt_results.csv (one row per engine and audio set).
"""

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
AUDIO = ROOT / "bench" / "stt_audio"
RESULTS = ROOT / "bench" / "results" / "stt_results.csv"
STT_DIR = Path(os.environ["USERPROFILE"]) / "TritonModels" / "stt"
VOSK_DIR = ROOT / "models" / "vosk-model-small-en-us-0.15"
RATE = 16000

ENGINES = {
    "vosk": None,
    "moonshine-tiny": ("moonshine", "sherpa-onnx-moonshine-tiny-en-int8"),
    "moonshine-base": ("moonshine", "sherpa-onnx-moonshine-base-en-int8"),
    "whisper-tiny": ("whisper", "sherpa-onnx-whisper-tiny.en"),
    "whisper-base": ("whisper", "sherpa-onnx-whisper-base.en"),
    "whisper-small": ("whisper", "sherpa-onnx-whisper-small.en"),
}


# ---------- scoring ----------

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen " \
       "sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(n):
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n == 100:
        return "one hundred"
    return " ".join(ONES[int(d)] for d in str(n))


def normalise(text):
    """Lower case, digits as words, no punctuation: so "Volume 40%." and "volume forty percent" match."""
    t = text.lower().replace("%", " percent").replace("&", " and ").replace("+", " plus ")
    t = re.sub(r"(\d+)", lambda m: " " + number_words(int(m.group(1))) + " ", t)
    t = re.sub(r"[^a-z' ]+", " ", t).replace("'", "")
    return t.split()


def edits(ref, hyp):
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1]


# ---------- test audio ----------

def read_wav(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def write_wav(path, audio):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.astype(np.int16).tobytes())


def eval_sentences():
    rows = [json.loads(line) for line in (ROOT / "bench" / "eval" / "eval.jsonl").open(encoding="utf-8")]
    return [r for r in rows if r["category"] != "asr_noise"]


def make_synthetic(n):
    """n eval sentences, each spoken by one of the Piper voices; clean and noisy copies."""
    out = AUDIO / "synthetic"
    if len(list(out.glob("clean/*.wav"))) >= n:
        return
    from piper import PiperVoice
    from scipy.signal import resample_poly
    voices = [PiperVoice.load(str(p)) for p in sorted((ROOT / "models" / "piper").glob("*.onnx"))]
    rng = random.Random(3)
    rows = rng.sample(eval_sentences(), n)
    noise_rng = np.random.default_rng(3)
    for i, row in enumerate(rows):
        voice = voices[i % len(voices)]
        chunks = list(voice.synthesize(row["text"]))
        audio = np.concatenate([c.audio_int16_array for c in chunks]).astype(np.float32)
        audio = resample_poly(audio, RATE, chunks[0].sample_rate)
        pad = np.zeros(int(0.3 * RATE), dtype=np.float32)
        audio = np.concatenate([pad, audio, pad])
        # Room-like noise: a low rumble plus hiss, 15 dB below the speech
        noise = np.cumsum(noise_rng.normal(size=len(audio))) * 0.02 + noise_rng.normal(size=len(audio))
        noise -= np.convolve(noise, np.ones(400) / 400, mode="same")
        speech_power = np.mean(audio[audio != 0] ** 2)
        noise *= np.sqrt(speech_power / 10 ** 1.5 / np.mean(noise ** 2))
        name = f"{i:03d}-{row['id']}"
        for kind, a in (("clean", audio), ("noisy", audio + noise)):
            write_wav(out / kind / f"{name}.wav", np.clip(a, -32768, 32767))
            (out / kind / f"{name}.txt").write_text(row["text"], encoding="utf-8")
    print(f"Made {n} synthetic sentences x 2 (clean, noisy) in {out}")


def record(n):
    """Show eval sentences one at a time and record you saying each (Enter to start, 5 s each)."""
    import sounddevice as sd
    out = AUDIO / "real"
    start = len(list(out.glob("*.wav")))
    rows = random.Random(start).sample(eval_sentences(), n)
    print("Speak normally, from where you'd usually sit. Ctrl+C to stop early.")
    for i, row in enumerate(rows):
        input(f"\n[{i + 1}/{n}] Press Enter, then say:   {row['text']}")
        audio = sd.rec(5 * RATE, samplerate=RATE, channels=1, dtype="int16")
        sd.wait()
        name = f"{start + i:03d}-{row['id']}"
        write_wav(out / f"{name}.wav", audio[:, 0])
        (out / f"{name}.txt").write_text(row["text"], encoding="utf-8")
    print(f"Saved to {out}")


# ---------- engines (run inside a worker process) ----------

def load_engine(name, threads):
    if name == "vosk":
        from vosk import KaldiRecognizer, Model, SetLogLevel
        SetLogLevel(-1)
        model = Model(str(VOSK_DIR))

        def run(audio):
            rec = KaldiRecognizer(model, RATE)
            rec.AcceptWaveform(audio.tobytes())
            return json.loads(rec.FinalResult()).get("text", "")
        return run

    sys.path.insert(0, str(ROOT))
    from transcriber import load_recognizer, recognise
    rec = load_recognizer(name, STT_DIR / ENGINES[name][1], threads)
    return lambda audio: recognise(rec, audio)


def worker(name, threads, sets):
    import psutil
    proc = psutil.Process()
    t0 = time.perf_counter()
    run = load_engine(name, threads)
    load_s = time.perf_counter() - t0
    run(np.zeros(RATE, dtype=np.int16))                 # warm-up
    for set_name in sets:
        wavs = sorted((AUDIO / set_name).glob("*.wav"))
        words = errors = right = 0
        times, audio_s, misses = [], 0.0, []
        for wav in wavs:
            audio = read_wav(wav)
            ref = (wav.with_suffix(".txt")).read_text(encoding="utf-8")
            t = time.perf_counter()
            hyp = run(audio)
            times.append(time.perf_counter() - t)
            audio_s += len(audio) / RATE
            r, h = normalise(ref), normalise(hyp)
            e = edits(r, h)
            words, errors, right = words + len(r), errors + e, right + (e == 0)
            if e:
                misses.append(f"{ref!r} -> {hyp!r}")
        print(json.dumps({
            "engine": name, "set": set_name, "n": len(wavs),
            "wer": round(100 * errors / max(words, 1), 1), "sentences_right": round(100 * right / max(len(wavs), 1), 1),
            "median_s": round(float(np.median(times)), 3), "p90_s": round(float(np.percentile(times, 90)), 3),
            "rtf": round(sum(times) / audio_s, 3), "load_s": round(load_s, 1),
            "peak_ram_mb": round(proc.memory_info().peak_wset / 2 ** 20),
            "misses": misses[:8],
        }), flush=True)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", nargs="+", default=list(ENGINES))
    ap.add_argument("--sentences", type=int, default=60)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--record", type=int, metavar="N", help="record N sentences in your voice first")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--sets", nargs="+", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        return worker(a.worker, a.threads, a.sets)

    if a.record:
        record(a.record)
    make_synthetic(a.sentences)
    sets = ["synthetic/clean", "synthetic/noisy"] + (["real"] if list((AUDIO / "real").glob("*.wav")) else [])
    RESULTS.parent.mkdir(exist_ok=True)
    new = not RESULTS.exists()
    with RESULTS.open("a", newline="", encoding="utf-8") as f:
        out = csv.writer(f)
        if new:
            out.writerow(["when", "engine", "set", "n", "wer", "sentences_right", "median_s", "p90_s",
                          "rtf", "load_s", "peak_ram_mb", "threads"])
        print(f"{'engine':16s} {'set':16s} {'WER':>6s} {'right':>6s} {'median':>7s} {'p90':>6s} {'RAM':>7s}")
        for name in a.engines:
            if ENGINES[name] and not (STT_DIR / ENGINES[name][1]).exists():
                print(f"{name}: model not downloaded, skipped")
                continue
            p = subprocess.run([sys.executable, __file__, "--worker", name, "--threads", str(a.threads),
                                "--sets", *sets], capture_output=True, text=True, encoding="utf-8")
            if p.returncode:
                print(f"{name}: FAILED\n{p.stderr[-1500:]}")
                continue
            for line in p.stdout.splitlines():
                if not line.startswith("{"):
                    continue
                r = json.loads(line)
                print(f"{r['engine']:16s} {r['set']:16s} {r['wer']:5.1f}% {r['sentences_right']:5.0f}% "
                      f"{r['median_s']:6.2f}s {r['p90_s']:5.2f}s {r['peak_ram_mb']:5d}MB", flush=True)
                for m in r["misses"][:3]:
                    print(f"      miss: {m}")
                out.writerow([time.strftime("%Y-%m-%d %H:%M"), r["engine"], r["set"], r["n"], r["wer"],
                              r["sentences_right"], r["median_s"], r["p90_s"], r["rtf"], r["load_s"],
                              r["peak_ram_mb"], a.threads])
                f.flush()


if __name__ == "__main__":
    main()
