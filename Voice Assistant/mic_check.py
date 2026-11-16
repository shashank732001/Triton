"""Diagnostic: list input devices and show live mic level + wake-word score."""

import sys
import time

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 5
DEVICE = int(sys.argv[2]) if len(sys.argv) > 2 else None

print("Default input:", sd.query_devices(kind="input")["name"])
for i, dev in enumerate(sd.query_devices()):
    if dev["max_input_channels"] > 0:
        api = sd.query_hostapis(dev["hostapi"])["name"]
        print(f"  [{i}] {dev['name']}  ({api})")

model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
peak_score, peak_level = 0.0, 0
start = time.monotonic()
with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280, device=DEVICE) as stream:
    while time.monotonic() - start < SECONDS:
        frame, _ = stream.read(1280)
        frame = frame[:, 0]
        level = int(np.abs(frame).max())
        score = model.predict(frame)["hey_jarvis"]
        peak_level, peak_score = max(peak_level, level), max(peak_score, score)
        if score > 0.05:
            print(f"  t={time.monotonic() - start:5.1f}s  level={level:5d}  score={score:.2f}")

print(f"Peak level: {peak_level} / 32767   Peak wake score: {peak_score:.2f}")
