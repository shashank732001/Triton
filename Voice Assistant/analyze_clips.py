"""Diagnostic: for each saved debug clip, show the wake-word score and what Vosk hears."""

import json
import wave

import numpy as np
from openwakeword.model import Model
from vosk import KaldiRecognizer, Model as VoskModel, SetLogLevel

import assistant

SetLogLevel(-1)
wake = Model(wakeword_models=[assistant.WAKE_MODEL], inference_framework="onnx")
vosk = VoskModel(str(assistant.VOSK_MODEL_DIR))

for path in sorted(assistant.DEBUG_CLIP_DIR.glob("clip_*.wav")):
    with wave.open(str(path)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    wake.reset()
    padded = np.concatenate([np.zeros(16000, np.int16), audio, np.zeros(16000, np.int16)])
    score = max(wake.predict(padded[i:i + 1280])[assistant.WAKE_WORD]
                for i in range(0, len(padded) - 1280, 1280))

    rec = KaldiRecognizer(vosk, assistant.SAMPLE_RATE)   # full vocabulary
    rec.AcceptWaveform(audio.tobytes())
    heard = json.loads(rec.FinalResult()).get("text", "")

    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    print(f"{path.name}  peak={int(np.abs(audio).max()):5d}  rms={rms:6.0f}  wake={score:.2f}  heard={heard!r}")
