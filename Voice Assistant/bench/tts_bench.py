r"""
Voice benchmark: how fast is each voice on this device, and how does it sound?

    .venv\Scripts\python.exe bench\tts_bench.py                    # all candidates below, 2 threads
    .venv\Scripts\python.exe bench\tts_bench.py --threads 4

For each voice: load time, time until the first sentence is ready (what you wait for after a command),
and real-time factor (seconds of work per second of speech; under 1 = faster than it talks).
Writes voice_samples/tts_compare/<voice>_<speaker>.wav so you can listen and pick.
Voices: models/piper (Piper) or %USERPROFILE%\TritonModels\tts (sherpa-onnx folders); see speaker.py.
The N95 is roughly 1.5-2x slower than the laptop's i5-1135G7 per thread.
"""

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from speaker import load_voice  # noqa: E402

CANDIDATES = [   # (voice folder or Piper name, speaker)
    ("en_GB-alan-medium", ""),                       # today's voice (Piper medium)
    ("en_US-ryan-high", ""),                         # Piper "high" (int8 file from vits-piper-*-int8)
    ("en_GB-cori-high", ""),
    ("kitten-nano-en-v0_8-int8", "expr-voice-2-m"),
    ("kitten-mini-en-v0_8", "expr-voice-2-m"),
    ("kitten-mini-en-v0_8", "expr-voice-2-f"),
    ("kokoro-int8-en-v0_19", "bm_george"),
    ("kokoro-int8-en-v0_19", "af_sarah"),
    ("kokoro-multi-lang-v1_0", "af_heart"),
    ("kokoro-multi-lang-v1_0", "bm_george"),
    ("kokoro-multi-lang-v1_0", "am_michael"),
]
TEXT = ("Opening YouTube and searching for lo-fi beats. "
        "It's twenty past four, and your battery is at sixty percent. Anything else?")
OUT = ROOT / "voice_samples" / "tts_compare"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=2)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"{'voice':34s} {'speaker':15s} {'load':>6s} {'first':>6s} {'RTF':>5s}")
    for name, spk in CANDIDATES:
        t0 = time.perf_counter()
        say = load_voice(name, ROOT, spk, 1.0, a.threads)
        if say is None:
            print(f"{name:34s} not installed")
            continue
        list(say("Ready."))                                  # warm-up, as the assistant does
        load = time.perf_counter() - t0
        t1 = time.perf_counter()
        chunks, first = [], None
        for rate, audio in say(TEXT):
            first = first or time.perf_counter() - t1
            chunks.append(audio)
        work = time.perf_counter() - t1
        audio = np.concatenate(chunks)
        with wave.open(str(OUT / f"{name}_{spk or 'default'}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(audio.tobytes())
        print(f"{name:34s} {spk:15s} {load:5.1f}s {first:5.2f}s {work / (len(audio) / rate):5.2f}", flush=True)
    print(f"\nListen: {OUT}")


if __name__ == "__main__":
    main()
