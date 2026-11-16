"""
Checks everything the offline GPU job needs before you queue it: packages import, files and
data are in place, and the voice generator can actually speak "hey nova".
Run by setup.sh on the login node; exits non-zero if anything is missing.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

WORK = Path(os.environ["WORK_DIR"])
problems = []

for mod in ["torch", "torchinfo", "torchmetrics", "scipy", "numpy", "tqdm", "yaml", "mutagen", "pronouncing",
            "torchaudio", "audiomentations", "torch_audiomentations", "speechbrain", "acoustics", "onnx",
            "onnxruntime", "soundfile", "sklearn", "webrtcvad", "imageio_ffmpeg", "piper_phonemize", "openwakeword"]:
    try:
        importlib.import_module(mod)
    except Exception as e:
        problems.append(f"import {mod}: {type(e).__name__}: {e}")

files = {
    "voice model": WORK / "piper-sample-generator/models/en_US-libritts_r-medium.pt",
    "generate_samples.py": WORK / "openwakeword/openwakeword/generate_samples.py",
    "openwakeword train.py": WORK / "openwakeword/openwakeword/train.py",
    "embedding model": WORK / "openwakeword/openwakeword/resources/models/embedding_model.onnx",
    "melspectrogram model": WORK / "openwakeword/openwakeword/resources/models/melspectrogram.onnx",
}
for name, p in files.items():
    if not p.exists():
        problems.append(f"missing {name}: {p}")

n_rirs = len(list((WORK / "mit_rirs").glob("*.wav")))
n_mp3 = len(list((WORK / "fma").rglob("*.mp3")))
acav_ready = (WORK / "acav_train_subset.npy").exists() or (
    (WORK / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy").exists()
    and (WORK / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy").stat().st_size > 16_000_000_000)
print(f"  room impulse responses: {n_rirs}, music MP3s: {n_mp3}, ACAV features: {'ok' if acav_ready else 'MISSING'}")
if n_rirs < 250:
    problems.append(f"only {n_rirs} room impulse responses (expected 250+)")
if n_mp3 < 100 and not (WORK / "fma_wav").is_dir():
    problems.append(f"only {n_mp3} music MP3s (expected thousands)")
if not acav_ready:
    problems.append("ACAV100M features missing or incomplete (re-run setup.sh; it resumes)")

try:
    from piper_phonemize import phonemize_espeak
    print(f"  phonemes for 'hey nova': {phonemize_espeak('hey nova', 'en-us')}")
except Exception as e:
    problems.append(f"phonemizer can't run: {e}")

if not problems:
    # The real thing, small: synthesise two clips on the CPU, fully offline
    # Same import paths train.py sets up: piper_train lives in piper-sample-generator,
    # while the patched generate_samples.py copy in openwakeword must win
    sys.path.insert(0, str(WORK / "piper-sample-generator"))
    sys.path.insert(0, str(WORK / "openwakeword" / "openwakeword"))
    try:
        from generate_samples import generate_samples
        with tempfile.TemporaryDirectory() as tmp:
            generate_samples(text=["hey nova"], output_dir=tmp, max_samples=2, batch_size=1)
            made = list(Path(tmp).glob("*.wav"))
            print(f"  test synthesis: {len(made)} clip(s) of 'hey nova'")
            if not made:
                problems.append("voice generator ran but produced no clips")
    except Exception as e:
        problems.append(f"voice generator failed: {type(e).__name__}: {e}")

if problems:
    print("\nPRE-FLIGHT FAILED:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("\n  Pre-flight PASSED: ready for the GPU job.")
