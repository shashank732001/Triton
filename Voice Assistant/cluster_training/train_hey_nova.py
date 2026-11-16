"""
Trains a wake-word model on a GPU node (no internet needed; setup.sh downloaded everything).
A port of steps 8-15 of the 2026 openWakeWord Colab notebook (github.com/alfiedennen/openwakeword-colab-2026),
with Colab paths replaced by WORK_DIR. Every step skips work that's already done, so a re-queued job resumes.

The phrase comes from $WAKE_PHRASE (default "hey echo"):
    sbatch train.slurm                              -> trains "hey echo"   (this PC)
    WAKE_PHRASE="hey triton" sbatch train.slurm     -> trains "hey triton" (the Nova Launcher PC)
    WAKE_PHRASE="..." GENERATE_ONLY=1 sbatch ...    -> only makes the phrase in many computer voices
                                                       (for the laptop's voice check of a built-in wake word)

Output: $WORK_DIR/<model_name>.onnx, e.g. hey_echo.onnx (copy it to "Voice Assistant/models/" on your laptop)
"""

import glob
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import yaml

WORK = Path(os.environ["WORK_DIR"])
TRAIN_PY = WORK / "openwakeword" / "openwakeword" / "train.py"
PHRASE = os.environ.get("WAKE_PHRASE", "hey echo").strip().lower()
TARGET_PHRASE = [PHRASE]
MODEL_NAME = PHRASE.replace(" ", "_")
CONFIG_YAML = WORK / f"{MODEL_NAME}.yaml"
GENERATE_ONLY = os.environ.get("GENERATE_ONLY") == "1"
# Phrases it must NOT fire on (added to openWakeWord's own auto-generated sound-alikes); the laptop's
# record_samples.py asks you to say some of these too
NEGATIVE_PHRASES = {
    "hey echo": ["hey eric", "hey eko", "echo", "hey lets go", "hey becca", "hey ego",
                 "hey gecko", "hey eddie", "a echo", "hey hello"],
    "hey triton": ["hey tristan", "hey brighton", "triton", "hey try it on", "hey titan", "hey tighten",
                   "hey trite one", "hey crayton", "hey brighten", "hey tritium"],
    "hey nova": ["hey nora", "hey noah", "hey now", "casanova", "bossa nova", "hey no way",
                 "nova", "hey novak", "hey over", "a nova"],
}.get(PHRASE, [])
N_SAMPLES = 1000 if GENERATE_ONLY else 12000   # notebook default 2000; an A100 makes more variety cheap
N_SAMPLES_VAL = 2000                            # -> 1500 test clips in many voices
STEPS = 100000           # stage 1; stages 2 and 3 get a tenth each (20k steps took ~3 min on the A100)
HIDDEN, BLOCKS = 256, 2  # model size (the notebook's 128 x 1 plateaued around 60% recall)
# Training pushes towards this many false alarms per hour of everyday speech...
MAX_FP_PER_HOUR = 1.0
# ...but the final model is the snapshot that hears your held-back takes best among those with at most
# this many. Validation counts every 80 ms frame over 0.5, while the assistant needs 2 frames in a row
# AND the voice check (which rejects other people), so live false wakes are far rarer than this number.
PICK_FP_PER_HOUR = 4.0
# Your own recordings (upload voice_samples/<model_name>/ to $WORK_DIR/my_voice/<model_name>): each training
# take is used as recorded plus this many randomly varied copies; a few are held back to measure recall on YOUR voice
MY_VOICE = WORK / "my_voice" / MODEL_NAME
# 120 varied copies per take (other mic, room, distance, background; my_voice.py): with ~150-250 takes your
# voice outnumbers the computer voices ~2-3:1, which is what "wake for ME every time" needs. MY_VOICE_COPIES=40
# was round 1 (hey_triton: 0.89 on your held-back takes).
MY_VOICE_COPIES = int(os.environ.get("MY_VOICE_COPIES", 120))
MY_VOICE_NEG_COPIES = int(os.environ.get("MY_VOICE_NEG_COPIES", 6))   # varied copies of your "don't fire" recordings
MY_VOICE_HOLDOUT = 0.15      # this share of takes (at least 10), spread over close and far ones, measures "your_voice"


def banner(text):
    print(f"\n==== {text} ====", flush=True)


# ---------- 8. music MP3s -> 16 kHz mono WAVs (background noise) ----------
banner("8. music -> 16 kHz WAV")
fma_wav = WORK / "fma_wav"
fma_wav.mkdir(exist_ok=True)
if len(list(fma_wav.glob("*.wav"))) >= 1500:
    print(f"  cached: {len(list(fma_wav.glob('*.wav')))} WAVs")
else:
    import imageio_ffmpeg
    from concurrent.futures import ThreadPoolExecutor
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    mp3s = sorted(glob.glob(str(WORK / "fma" / "**" / "*.mp3"), recursive=True))
    if len(mp3s) < 100:
        sys.exit(f"Only {len(mp3s)} MP3s in {WORK / 'fma'}; re-run setup.sh on the login node")
    jobs = [(i, m) for i, m in enumerate(mp3s[:1500]) if not (fma_wav / f"{i:05d}.wav").exists()]

    def convert(job):
        i, mp3 = job
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", mp3, "-ac", "1", "-ar", "16000",
                        str(fma_wav / f"{i:05d}.wav")], check=False)

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        list(pool.map(convert, jobs))
    print(f"  {len(list(fma_wav.glob('*.wav')))} WAVs ready")

# ---------- 9. ACAV subsample: 10% train, 1% validation ----------
banner("9. ACAV100M subsample")
ACAV_SRC = WORK / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
ACAV_TRAIN = WORK / "acav_train_subset.npy"
ACAV_VAL = WORK / "acav_val_subset.npy"
if ACAV_TRAIN.exists() and ACAV_VAL.exists():
    print("  cached")
else:
    arr = np.load(ACAV_SRC, mmap_mode="r")
    n_train, n_val = len(arr) // 10, len(arr) // 100
    np.save(ACAV_TRAIN, np.array(arr[:n_train]))
    val = np.array(arr[n_train:n_train + n_val])
    np.save(ACAV_VAL, val.reshape(-1, val.shape[-1]))      # validation stays a flat (M, 96) stream
    del arr, val
    ACAV_SRC.unlink()                                       # frees ~17 GB
    print("  train/val subsets saved; removed the 17 GB original")

# ---------- 10. training config ----------
banner("10. config")
out_dir = WORK / f"{MODEL_NAME}_output"
feat_dir = out_dir / MODEL_NAME
config = {
    "target_phrase": TARGET_PHRASE,
    "model_name": MODEL_NAME,
    "custom_negative_phrases": list(NEGATIVE_PHRASES),
    "n_samples": N_SAMPLES,
    "n_samples_val": N_SAMPLES_VAL,
    "tts_batch_size": 100,
    "piper_sample_generator_path": str(WORK / "piper-sample-generator"),
    "augmentation_rounds": 1,
    "augmentation_batch_size": 16,
    "steps": STEPS,
    "max_negative_weight": 1500,
    "target_accuracy": 0.7,
    "target_recall": 0.5,
    "target_false_positives_per_hour": MAX_FP_PER_HOUR,
    "batch_size": 128,
    "learning_rate": 1e-4,
    "model_type": "dnn",
    "layer_dim": 128,
    "layer_size": 128,
    "n_blocks": 1,
    "model_input_shape": [16, 96],
    "n_classes": 1,
    "batch_n_per_class": {"ACAV100M_sample": 1024, "adversarial_negative": 50, "positive": 50},
    "background_paths": [str(fma_wav)],
    "background_paths_duplication_rate": [1],
    "rir_paths": [str(WORK / "mit_rirs")],
    "false_positive_validation_data_path": str(ACAV_VAL),
    "feature_data_files": {"ACAV100M_sample": str(ACAV_TRAIN)},
    "output_dir": str(out_dir),
    "tflite_export": False,
    "onnx_export": True,
    "positive_clips_train_dir": str(feat_dir / "positive_train"),
    "positive_clips_test_dir": str(feat_dir / "positive_test"),
    "negative_clips_train_dir": str(feat_dir / "negative_train"),
    "negative_clips_test_dir": str(feat_dir / "negative_test"),
    "feature_save_dir": str(feat_dir),
}
out_dir.mkdir(parents=True, exist_ok=True)
CONFIG_YAML.write_text(yaml.dump(config, sort_keys=False))
print(f"  phrase {TARGET_PHRASE}, {N_SAMPLES} samples, {STEPS} steps")

# ---------- 11. synthesise clips with Piper TTS ----------
banner("11. generate synthetic clips")
clip_dirs = {
    "positive_train": (config["positive_clips_train_dir"], int(N_SAMPLES * 0.75)),
    "positive_test": (config["positive_clips_test_dir"], int(N_SAMPLES_VAL * 0.75)),
    "negative_train": (config["negative_clips_train_dir"], int(N_SAMPLES * 0.75)),
    "negative_test": (config["negative_clips_test_dir"], int(N_SAMPLES_VAL * 0.75)),
}


def clip_count(path):
    return len(os.listdir(path)) if os.path.isdir(path) else 0


if all(clip_count(p) >= n for p, n in clip_dirs.values()):
    print("  cached")
else:
    subprocess.run([sys.executable, str(TRAIN_PY), "--training_config", str(CONFIG_YAML), "--generate_clips"],
                   check=True)
for name, (p, n) in clip_dirs.items():
    print(f"  {name}: {clip_count(p)} clips")
    if clip_count(p) < n:
        sys.exit(f"generate_clips left {name} with {clip_count(p)} clips (expected >= {n})")

# ---------- 12. resample Piper's 22.05 kHz output to 16 kHz ----------
banner("12. resample clips to 16 kHz")
import soundfile as sf
from scipy.signal import resample_poly
from tqdm import tqdm

for d in sorted({os.path.dirname(f) for f in glob.glob(str(out_dir / "**" / "*.wav"), recursive=True)}):
    files = [f for f in os.listdir(d) if f.endswith(".wav")]
    if all(sf.info(os.path.join(d, f)).samplerate == 16000 for f in files[:20]):
        continue
    for f in tqdm(files, desc=os.path.basename(d), mininterval=10):
        p = os.path.join(d, f)
        data, sr = sf.read(p)
        if sr != 16000:
            sf.write(p, resample_poly(data.astype("float32"), 16000, sr), 16000)
if GENERATE_ONLY:
    many = Path(config["positive_clips_test_dir"])
    print(f"\nDONE (clips only): {clip_count(many)} '{PHRASE}' clips in many voices in {many}")
    print(f'Copy that folder to "Voice Assistant/models/{MODEL_NAME}_many_voices" on your laptop.')
    sys.exit(0)
needed = ["positive_features_train.npy", "negative_features_train.npy",
          "positive_features_test.npy", "negative_features_test.npy"]
if not all((feat_dir / n).exists() for n in needed):
    for f in glob.glob(str(out_dir / "**" / "*.npy"), recursive=True):
        os.remove(f)        # stale features from a half-finished run

# ---------- 13. augment (noise, rooms) + extract features ----------
banner("13. augment + featurise")
if all((feat_dir / n).exists() for n in needed):
    print("  cached")
else:
    subprocess.run([sys.executable, str(TRAIN_PY), "--training_config", str(CONFIG_YAML), "--augment_clips"],
                   check=True)
for n in needed:
    if not (feat_dir / n).exists():
        sys.exit(f"augment didn't produce {n}")
    print(f"  {n}: {(feat_dir / n).stat().st_size / 1e6:.1f} MB")

# ---------- 13b. your own recordings (optional): $WORK_DIR/my_voice/{positive,negative}/*.wav ----------
banner("13b. your voice")
# positive/: record_samples.py takes. live/wake_*_cmd.wav: real wake-ups the assistant saved where a command
# followed, so they really were the wake word, heard by the real mic in the real room.
my_takes = sorted((MY_VOICE / "positive").glob("*.wav")) + sorted((MY_VOICE / "live").glob("wake_*_cmd.wav"))
my_pos_train = my_pos_test = my_neg = None
if not my_takes:
    print(f"  no recordings in {MY_VOICE / 'positive'}; training on computer voices only")
else:
    import my_voice
    from openwakeword.utils import AudioFeatures

    audio_features = AudioFeatures(inference_framework="onnx", device="cpu")
    takes = []
    for p in my_takes:
        phrase = my_voice.trim(my_voice.load(p))
        if my_voice.PHRASE_SEC[0] <= len(phrase) / my_voice.SR <= my_voice.PHRASE_SEC[1]:
            takes.append(phrase)
        else:
            print(f"  skipped {p.name}: couldn't find a clean phrase in it ({len(phrase) / my_voice.SR:.2f} s)")
    # Held-back takes evenly spaced through the recordings, so later rounds (--more, --far) get tested too
    n_held = max(10, round(len(takes) * MY_VOICE_HOLDOUT))
    held = set(np.linspace(0, len(takes) - 1, n_held).round().astype(int).tolist())
    test_takes = [t for i, t in enumerate(takes) if i in held]
    train_takes = [t for i, t in enumerate(takes) if i not in held]
    rirs = [my_voice.load(p) for p in sorted((WORK / "mit_rirs").glob("*.wav"))]
    backgrounds = sorted(fma_wav.glob("*.wav"))
    negatives = sorted((MY_VOICE / "negative").glob("*.wav"))
    neg_audio = [my_voice.load(p) for p in negatives]
    babble = [a for p, a in zip(negatives, neg_audio) if p.name.startswith("speech")]   # your chatter as background
    print(f"  augmenting {len(train_takes)} takes x {MY_VOICE_COPIES} copies (a few minutes)...", flush=True)
    my_pos_train = my_voice.positive_features(train_takes, MY_VOICE_COPIES, audio_features, rirs, backgrounds,
                                              seed=1, babble=babble)
    my_pos_test = my_voice.positive_features(test_takes, 10, audio_features, rirs, backgrounds, seed=2, babble=babble)
    if negatives:
        my_neg = my_voice.negative_features(neg_audio, audio_features, copies=MY_VOICE_NEG_COPIES, rirs=rirs,
                                            backgrounds=backgrounds)
    print(f"  {len(train_takes)} takes for training -> {len(my_pos_train)} windows; "
          f"{len(test_takes)} held back for testing -> {len(my_pos_test)} windows; "
          f"{0 if my_neg is None else len(my_neg)} 'don't fire' windows from {len(negatives)} other recordings")

# ---------- 14. train (3-stage curriculum, as openWakeWord's auto_train) ----------
banner("14. train")
import torch
import torch.nn as nn
import torch.optim as optim

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"  device: {DEVICE} {torch.cuda.get_device_name(0) if DEVICE == 'cuda' else ''}")


def load_feat(name):
    return torch.from_numpy(np.load(feat_dir / name).astype(np.float32)).to(DEVICE)


pos_train, neg_train = load_feat("positive_features_train.npy"), load_feat("negative_features_train.npy")
pos_test, neg_test = load_feat("positive_features_test.npy"), load_feat("negative_features_test.npy")
if my_pos_train is not None:
    pos_train = torch.cat([pos_train, torch.from_numpy(my_pos_train).to(DEVICE)])
    my_pos_test_t = torch.from_numpy(my_pos_test).to(DEVICE)
if my_neg is not None:
    neg_train = torch.cat([neg_train, torch.from_numpy(my_neg).to(DEVICE)])
acav_train_np = np.load(ACAV_TRAIN, mmap_mode="r")
acav_val_np = np.load(ACAV_VAL)
val_listen_hours = acav_val_np.shape[0] * 0.08 / 3600.0
n_win_val = acav_val_np.shape[0] - 16
acav_val_windows = np.ascontiguousarray(
    np.lib.stride_tricks.sliding_window_view(acav_val_np, (16, 96))[:, 0, :, :].astype(np.float32))
print(f"  pos_train {tuple(pos_train.shape)}, neg_train {tuple(neg_train.shape)}, "
      f"validation stream {val_listen_hours:.1f} h")
VAL_BATCH = 4096


class WakewordModel(nn.Module):
    def __init__(self, hidden=HIDDEN, blocks=BLOCKS):
        super().__init__()
        layers, width = [nn.Flatten()], 16 * 96
        for _ in range(blocks):
            layers += [nn.Linear(width, hidden), nn.LayerNorm(hidden), nn.ReLU()]
            width = hidden
        self.body = nn.Sequential(*layers)
        self.head = nn.Linear(hidden, 1)

    def forward(self, x):
        return self.head(self.body(x))


model = WakewordModel().to(DEVICE)
loss_fn = nn.BCEWithLogitsLoss(reduction="none")
THRESH = 0.5
history = {"val_recall": [], "val_accuracy": [], "val_fp_per_hour": [], "val_n_fp": [], "loss": []}
snapshots = []          # (weights, scores) at every validation; step 15 picks one
B_POS, B_ANEG, B_ACAV = 32, 32, 64


def random_acav_window_batch(k):
    n, t, f = acav_train_np.shape
    rows, starts = np.random.randint(0, n, size=k), np.random.randint(0, t - 16 + 1, size=k)
    out = np.empty((k, 16, f), dtype=np.float32)
    for i, (r, s) in enumerate(zip(rows, starts)):
        out[i] = acav_train_np[r, s:s + 16, :].astype(np.float32)
    return out


def build_batch():
    p = pos_train[torch.randint(0, pos_train.shape[0], (B_POS,), device=DEVICE)]
    an = neg_train[torch.randint(0, neg_train.shape[0], (B_ANEG,), device=DEVICE)]
    acav = torch.from_numpy(random_acav_window_batch(B_ACAV)).to(DEVICE)
    x = torch.cat([p, an, acav], dim=0)
    y = torch.cat([torch.ones(B_POS, device=DEVICE), torch.zeros(B_ANEG + B_ACAV, device=DEVICE)])
    return x, y


@torch.no_grad()
def validate(label):
    model.eval()
    p_preds = torch.sigmoid(model(pos_test)).squeeze(-1)
    n_preds = torch.sigmoid(model(neg_test)).squeeze(-1)
    recall = (p_preds >= THRESH).float().mean().item()
    accuracy = (((p_preds >= THRESH).sum() + (n_preds < THRESH).sum()).item()
                / (pos_test.shape[0] + neg_test.shape[0]))
    n_fp = 0
    for i in range(0, n_win_val, VAL_BATCH):
        chunk = torch.from_numpy(acav_val_windows[i:i + VAL_BATCH]).to(DEVICE)
        n_fp += (torch.sigmoid(model(chunk)).squeeze(-1) >= THRESH).sum().item()
    fp_per_hour = n_fp / max(val_listen_hours, 1e-6)
    for k, v in (("val_recall", recall), ("val_accuracy", accuracy),
                 ("val_fp_per_hour", fp_per_hour), ("val_n_fp", n_fp)):
        history[k].append(v)
    your_voice = None
    if my_pos_train is not None:
        your_voice = (torch.sigmoid(model(my_pos_test_t)).squeeze(-1) >= THRESH).float().mean().item()
    snapshots.append(({k: v.detach().clone() for k, v in model.state_dict().items()},
                      {"label": label, "val_recall": recall, "your_voice": your_voice,
                       "val_accuracy": accuracy, "val_fp_per_hour": fp_per_hour}))
    mine = "" if your_voice is None else f" your_voice={your_voice:.3f}"
    print(f"  [{label}] recall={recall:.3f}{mine} acc={accuracy:.3f} fp/hr={fp_per_hour:.2f}", flush=True)
    model.train()


def run_stage(stage, n_steps, lr, max_neg_w, val_window_frac=1.0):
    optimizer = optim.Adam(model.parameters(), lr=lr)
    weight_schedule = np.linspace(1.0, max_neg_w, n_steps)
    val_steps = set(np.linspace(int(n_steps * (1.0 - val_window_frac)), n_steps - 1, 20).astype(int))
    warmup, hold = max(1, n_steps // 5), n_steps // 3
    accumulated = []
    print(f"\n  -- stage {stage}: {n_steps} steps, lr={lr}, max_neg_w={max_neg_w}", flush=True)
    t0 = time.time()
    for step in range(n_steps):
        if step < warmup:
            lr_now = lr * (step + 1) / warmup
        elif step < warmup + hold:
            lr_now = lr
        else:
            decay_t = (step - warmup - hold) / max(1, n_steps - warmup - hold)
            lr_now = lr * 0.5 * (1.0 + math.cos(math.pi * min(1.0, decay_t)))
        for pg in optimizer.param_groups:
            pg["lr"] = lr_now
        x, y = build_batch()
        logits = model(x).squeeze(-1)
        preds = torch.sigmoid(logits)
        keep = ((y == 0) & (preds >= 0.001)) | ((y == 1) & (preds < 0.999))     # hard-example mining
        if keep.sum() > 0:
            neg_w = weight_schedule[step]
            kept_y = y[keep]
            w = torch.where(kept_y > 0.5, torch.tensor(1.0, device=DEVICE),
                            torch.tensor(neg_w, device=DEVICE, dtype=torch.float32))
            accumulated.append((logits[keep], kept_y, w))
            if sum(t[0].shape[0] for t in accumulated) >= 128:
                loss = (loss_fn(torch.cat([t[0] for t in accumulated]), torch.cat([t[1] for t in accumulated]))
                        * torch.cat([t[2] for t in accumulated])).mean()
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                history["loss"].append(loss.item())
                accumulated.clear()
        if step in val_steps:
            validate(f"stage{stage} {step}/{n_steps} {(time.time() - t0) / 60:.1f}m")
    print(f"  stage {stage} done in {(time.time() - t0) / 60:.1f} min", flush=True)


target_fp = config["target_false_positives_per_hour"]
max_neg_w = config["max_negative_weight"]
run_stage(1, STEPS, lr=1e-4, max_neg_w=max_neg_w, val_window_frac=0.25)
if history["val_fp_per_hour"] and min(history["val_fp_per_hour"]) > target_fp:
    max_neg_w *= 2
    print(f"  [adapt] false positives above target: negative weight doubled to {max_neg_w}")
run_stage(2, max(2000, STEPS // 10), lr=1e-5, max_neg_w=max_neg_w)
if history["val_fp_per_hour"] and min(history["val_fp_per_hour"]) > target_fp:
    max_neg_w *= 2
    print(f"  [adapt] false positives above target: negative weight doubled to {max_neg_w}")
run_stage(3, max(2000, STEPS // 10), lr=1e-6, max_neg_w=max_neg_w)
print(f"  {len(snapshots)} snapshots; best fp/hr {min(history['val_fp_per_hour']):.2f}, "
      f"best recall {max(history['val_recall']):.3f}")

# ---------- 15. pick the best snapshot, export ONNX ----------
banner("15. pick + export")
allowed = [s for s in snapshots if s[1]["val_fp_per_hour"] <= PICK_FP_PER_HOUR]
if allowed:
    # hears you best (computer voices when there are no recordings), then fewer false alarms
    final_state, chosen = max(allowed, key=lambda s: (s[1]["your_voice"] if s[1]["your_voice"] is not None
                                                      else s[1]["val_recall"], s[1]["val_recall"],
                                                      -s[1]["val_fp_per_hour"]))
    print(f"  {len(allowed)}/{len(snapshots)} snapshots within {PICK_FP_PER_HOUR} false alarms/hour")
else:
    final_state, chosen = min(snapshots, key=lambda s: s[1]["val_fp_per_hour"])
    print(f"  no snapshot within {PICK_FP_PER_HOUR} false alarms/hour; using the one with the fewest")
mine = "" if chosen["your_voice"] is None else f", your voice {chosen['your_voice']:.3f}"
print(f"  chose [{chosen['label']}]: recall {chosen['val_recall']:.3f}{mine}, fp/hr {chosen['val_fp_per_hour']:.2f}")
model.load_state_dict(final_state)
model.eval()


class WakewordExportable(nn.Module):
    """Sigmoid baked in, so the output is a 0-1 score like openWakeWord's own models."""

    def __init__(self, base):
        super().__init__()
        self.base = base

    def forward(self, x):
        return torch.sigmoid(self.base(x))


onnx_path = feat_dir / f"{MODEL_NAME}.onnx"
torch.onnx.export(WakewordExportable(model).to(DEVICE).eval(), torch.randn(1, 16, 96, device=DEVICE), str(onnx_path),
                  input_names=["onnx::Flatten_0"], output_names=["output"],
                  dynamic_axes={"onnx::Flatten_0": {0: "batch"}, "output": {0: "batch"}},
                  opset_version=14, dynamo=False)

import onnxruntime as ort

sess = ort.InferenceSession(str(onnx_path))
scores = sess.run(None, {sess.get_inputs()[0].name: pos_test.cpu().numpy()})[0].flatten()
print(f"  ONNX check on held-out synthetic '{PHRASE}': recall@0.5 = {(scores >= 0.5).mean():.3f}")
if my_pos_test is not None:
    mine = sess.run(None, {sess.get_inputs()[0].name: my_pos_test})[0].flatten()
    print(f"  ONNX check on your held-back takes:         recall@0.5 = {(mine >= 0.5).mean():.3f}")

final = WORK / f"{MODEL_NAME}.onnx"
shutil.copy(onnx_path, final)
print(f"\nDONE: {final} ({final.stat().st_size / 1e3:.0f} KB)")
print(f'Copy it to "Voice Assistant/models/{MODEL_NAME}.onnx" on your laptop, and the folder '
      f'{config["positive_clips_test_dir"]} to "Voice Assistant/models/{MODEL_NAME}_many_voices".')
