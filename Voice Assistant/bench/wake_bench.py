r"""
Wake-word benchmark: how many of your wake words does the model catch, and how often does it wake
on something else, for each threshold and patience (frames in a row) setting?

    .venv\Scripts\python.exe bench\wake_bench.py                  # hey_echo
    .venv\Scripts\python.exe bench\wake_bench.py --wake hey_triton --live

Clips:
  should wake      voice_samples/<wake>/positive/*.wav   you saying the wake word (record_samples.py)
  should not wake  voice_samples/<wake>/negative/*.wav   room noise, your other speech, similar words
                   bench/stt_audio/synthetic/*/*.wav      60 command sentences x clean/noisy (TV-like)
  --live           also voice_samples/<wake>/live/*.wav   saved by the assistant: wake_*_cmd count as
                   should-wake, wake_*_nocmd as should-not (usually false wake-ups), near_* as should-wake

Finding (2026-10-28, hey_echo): other speech never scored above 0.04, but only 21/50 of your takes
reached 0.5 (13 stay near 0 at any threshold). Hence threshold 0.2 in assistant.py, and retraining
with your recordings for the rest. Speech-to-text second checks (Whisper, Moonshine, Vosk grammar)
were tried and dropped: they hear "Hey Echo" as "here you go" / "very good".
"""

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))

from stt_bench import read_wav  # noqa: E402

FRAME = 1280
RATE = 16000


def wake_scores(model, audio, wake):
    """Per-frame scores of the wake model over a clip (with 1 s of silence around it, as if live)."""
    pad = np.zeros(RATE, dtype=np.int16)
    audio = np.concatenate([pad, audio, pad])
    model.reset()
    return [model.predict(audio[i:i + FRAME]).get(wake, 0.0) for i in range(0, len(audio) - FRAME + 1, FRAME)]


def held(scores, patience):
    """Best score kept for `patience` frames in a row: what the assistant compares to its threshold."""
    return max(min(scores[i:i + patience]) for i in range(len(scores) - patience + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wake", default="hey_echo")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="should-wake = voice_samples/<wake>/test/ (record_samples.py --test; never uploaded for training)")
    a = ap.parse_args()

    from openwakeword.model import Model
    model = Model(wakeword_models=[str(ROOT / "models" / f"{a.wake}.onnx")], inference_framework="onnx")
    samples = ROOT / "voice_samples" / a.wake
    pos = sorted((samples / ("test" if a.test else "positive")).glob("*.wav"))
    neg = sorted((samples / "negative").glob("*.wav")) + sorted((ROOT / "bench" / "stt_audio" / "synthetic").glob("*/*.wav"))
    if a.live:
        live = sorted((samples / "live").glob("*.wav"))
        pos += [p for p in live if p.stem.endswith("_cmd") or "_near_" in p.stem]
        neg += [p for p in live if p.stem.endswith("_nocmd")]
    pos_scores = {p: wake_scores(model, read_wav(p), a.wake) for p in pos}
    neg_scores = {p: wake_scores(model, read_wav(p), a.wake) for p in neg}

    print(f"{len(pos)} should wake, {len(neg)} should not; model {a.wake}.onnx\n")
    print("threshold  patience  caught      false wakes")
    for th in (0.5, 0.3, 0.2, 0.1, 0.05):
        for k in (1, 2):
            c = sum(held(s, k) >= th for s in pos_scores.values())
            f = sum(held(s, k) >= th for s in neg_scores.values())
            print(f"{th:9.2f} {k:9d}  {c:4d}/{len(pos):<4d}  {f:4d}/{len(neg)}")
    print("\nnever above 0.05 (only retraining helps these):",
          ", ".join(p.name for p, s in pos_scores.items() if held(s, 2) < 0.05) or "none")
    top = sorted(neg_scores.items(), key=lambda kv: -held(kv[1], 2))[:5]
    print("highest-scoring should-not clips:", ", ".join(f"{p.name} {held(s, 2):.2f}" for p, s in top))


if __name__ == "__main__":
    main()
