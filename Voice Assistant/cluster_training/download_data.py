"""
Downloads the training data on the login node (the GPU nodes have no internet).
Same sources as the Colab notebook; big files resume if interrupted.

  mit_rirs/   ~270 room impulse responses as 16 kHz WAVs  (makes clips sound like real rooms)
  fma/        ~8 GB of music MP3s                          (background noise; converted in the GPU job)
  openwakeword_features_ACAV100M_2000_hrs_16bit.npy        (~17 GB of everyday-speech features: "not the wake word")
"""

import os
import time
import zipfile
from pathlib import Path

os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "120")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")

import numpy as np
import requests
import scipy.io.wavfile as wavfile
from tqdm import tqdm

WORK = Path(os.environ["WORK_DIR"])
RIR_DIR = WORK / "mit_rirs"
FMA_DIR = WORK / "fma"
FMA_ZIP = WORK / "fma_small.zip"
ACAV = WORK / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
ACAV_URL = ("https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/"
            "openwakeword_features_ACAV100M_2000_hrs_16bit.npy")
FMA_URL = "https://os.unil.cloud.switch.ch/fma/fma_small.zip"
ACAV_TRAIN = WORK / "acav_train_subset.npy"


def resumable_download(url, dest, label):
    have = dest.stat().st_size if dest.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with requests.get(url, stream=True, headers=headers, timeout=120) as r:
        if r.status_code == 416:        # already complete
            return
        r.raise_for_status()
        if have and r.status_code != 206:
            have = 0                    # server ignored the range: start over
        total = int(r.headers.get("content-length", 0)) + have
        with open(dest, "ab" if have else "wb") as f, tqdm(total=total, initial=have, unit="B",
                                                          unit_scale=True, unit_divisor=1024, desc=label) as bar:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                f.write(chunk)
                bar.update(len(chunk))


# ---- MIT room impulse responses ----
RIR_DIR.mkdir(parents=True, exist_ok=True)
if len(list(RIR_DIR.glob("*.wav"))) >= 250:
    print(f"  cached: {len(list(RIR_DIR.glob('*.wav')))} room impulse responses")
else:
    import datasets
    from huggingface_hub import snapshot_download
    from scipy.signal import resample_poly
    for attempt in range(6):
        try:
            snapshot_download(repo_id="davidscripka/MIT_environmental_impulse_responses",
                              repo_type="dataset", etag_timeout=120, max_workers=4)
            break
        except Exception as e:
            print(f"  snapshot attempt {attempt + 1}/6 failed: {e}; retrying")
            time.sleep(15 * (attempt + 1))
    rirs = datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses", split="train")
    for i, row in enumerate(rirs):
        out = RIR_DIR / f"{i:04d}.wav"
        if out.exists():
            continue
        sr, arr = row["audio"]["sampling_rate"], np.asarray(row["audio"]["array"])
        if sr != 16000:
            arr = resample_poly(arr, 16000, sr)
        wavfile.write(out, 16000, (arr * 32767).clip(-32768, 32767).astype(np.int16))
    print(f"  wrote {len(list(RIR_DIR.glob('*.wav')))} room impulse responses")

# ---- ACAV100M features (skipped once the GPU job has made its subset and deleted the original) ----
if ACAV_TRAIN.exists():
    print("  cached: ACAV subset already prepared")
elif ACAV.exists() and ACAV.stat().st_size > 16_000_000_000:
    print(f"  cached: ACAV {ACAV.stat().st_size / 1e9:.1f} GB")
else:
    resumable_download(ACAV_URL, ACAV, "ACAV100M")

# ---- FMA music (background noise) ----
if FMA_DIR.is_dir() and len(list(FMA_DIR.rglob("*.mp3"))) > 100:
    print(f"  cached: FMA {len(list(FMA_DIR.rglob('*.mp3')))} MP3s")
else:
    resumable_download(FMA_URL, FMA_ZIP, "FMA")
    print("  extracting FMA...")
    with zipfile.ZipFile(FMA_ZIP) as zf:
        zf.extractall(FMA_DIR)
    FMA_ZIP.unlink()
    print(f"  FMA: {len(list(FMA_DIR.rglob('*.mp3')))} MP3s")
