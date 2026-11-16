r"""
Would a second check on the words (Max's way of confirming a wake-up) cut false wake-ups without losing real ones?

For every clip: run the wake-word model as the assistant does (threshold, patience 2, personal voice check), and where
it fires, transcribe the WAKE_AUDIO_SEC before the hit (+ TAIL_SEC) with a small speech-to-text model and ask
confirm() whether it contains "Hey Triton".

    .venv\Scripts\python.exe bench\wake_confirm_bench.py [--ears moonshine-tiny] [--threshold 0.5]

Result (2026-11-16, moonshine-tiny, 82 ms per check): of the recorded takes that woke the model, 22/237 positive,
3/32 test and 0/5 live were confirmed: your "Hey Triton" comes out as "Hate item", "He tried", "Page item" or nothing.
Not usable; wake_check.speech_check() is used instead. Kept to re-test after retraining or with other ears.
"""

import argparse
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from transcriber import Transcriber  # noqa: E402

FRAME = 1280
RATE = 16000
WAKE_AUDIO_SEC = 2.0     # audio before the hit that is transcribed
TAIL_SEC = 0.24          # and after it
_HEY = {"hey", "hay", "hi", "hair", "head", "hit", "here", "he", "hate", "heat", "eh", "a", "hello", "okay", "ok"}


def confirm(text):
    """(True, why) if the words contain the wake phrase anywhere ("...so hey triton open youtube")."""
    from wake_check import tritonish
    w = (text or "").split()
    if not w:
        return False, "nothing heard"
    for i, word in enumerate(w):
        if re.sub(r"[^a-z]", "", word.lower()) in _HEY and any(
                i + 1 + k <= len(w) and tritonish(w[i + 1:i + 1 + k]) for k in (1, 2)):
            return True, "name heard"
        if len(word) > 4 and tritonish([word]) and (i == 0 or word.endswith(",") or i == len(w) - 1):
            return True, "name heard"
    return False, "no name in %d words" % len(w)


def read(path):
    with wave.open(str(path)) as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            return None
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return a[::w.getnchannels()] if w.getnchannels() > 1 else a


def groups():
    root = BASE / "voice_samples" / "hey_triton"
    live = root / "live"
    return [   # (name, files, should it wake: True / False / None = unknown)
        ("positive", sorted((root / "positive").glob("*.wav")), True),
        ("test", sorted((root / "test").glob("*.wav")), True),
        ("other_voices", sorted((root / "other_voices").glob("*.wav")), None),
        ("live wake+cmd", sorted(live.glob("*wake*_cmd.wav")), True),
        ("live wake, no cmd", sorted(live.glob("*_nocmd.wav")), None),
        ("live near miss", sorted(live.glob("*near*.wav")), None),
        ("negative", sorted((root / "negative").glob("*.wav")), False),
        ("sentences (TV-like)", sorted((BASE / "bench" / "stt_audio" / "synthetic").glob("*/*.wav")), False),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ears", default="moonshine-tiny")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    from openwakeword.model import Model
    verifier = BASE / "models" / "hey_triton_verifier.pkl"
    model = Model(wakeword_models=[str(BASE / "models" / "hey_triton.onnx")], inference_framework="onnx",
                  custom_verifier_models={"hey_triton": str(verifier)} if verifier.exists() else {},
                  custom_verifier_threshold=0.3)
    ears = Transcriber.from_settings({"model": args.ears, "threads": 4}, BASE)
    ears.ready.wait(120)
    print(f"ears {args.ears}, threshold {args.threshold}, verifier {'on' if verifier.exists() else 'off'}\n")

    times = []
    for name, files, should in groups():
        fired = confirmed = 0
        rejected = []
        for f in files:
            audio = read(f)
            if audio is None:
                continue
            model.reset()
            pad = np.zeros(RATE, np.int16)
            audio = np.concatenate([pad, audio, pad])
            hits, hit_at = 0, None
            for i in range(0, len(audio) - FRAME + 1, FRAME):
                hits = hits + 1 if model.predict(audio[i:i + FRAME]).get("hey_triton", 0) >= args.threshold else 0
                if hits >= 2:
                    hit_at = i + FRAME
                    break
            if hit_at is None:
                continue
            fired += 1
            clip = audio[max(0, hit_at - int(WAKE_AUDIO_SEC * RATE)):hit_at + int(TAIL_SEC * RATE)]
            t0 = time.monotonic()
            text = ears.transcribe(clip)
            times.append(time.monotonic() - t0)
            ok, why = confirm(text)
            confirmed += ok
            if not ok:
                rejected.append(f"{f.name}: {text!r} ({why})")
        label = {True: "should wake", False: "should NOT wake", None: "unknown"}[should]
        print(f"{name:20s} clips {len(files):4d}  model fired {fired:4d}  confirmed {confirmed:4d}   ({label})")
        for r in rejected[:8]:
            print("      rejected", r)
    if times:
        print(f"\ncheck time: median {np.median(times) * 1000:.0f} ms, p90 {np.percentile(times, 90) * 1000:.0f} ms")


if __name__ == "__main__":
    main()
