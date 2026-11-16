#!/bin/bash
# Run ONCE on the cluster LOGIN node (it needs internet; the GPU nodes don't have it):
#     cd cluster_training && bash setup.sh
# Downloads ~25 GB, so run it inside tmux/screen or with nohup. Safe to re-run: every step
# skips what's already done, and the big downloads resume where they stopped.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
export WORK_DIR

echo "== 1/5 Python environment: $WORK_DIR/venv"
module load "$PYTHON_MODULE" 2>/dev/null || echo "  (couldn't load module '$PYTHON_MODULE'; using $(python3 --version 2>&1))"
PYV=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
case "$PYV" in
  3.9|3.10|3.11|3.12) echo "  Python $PYV" ;;
  *) echo "  Python $PYV found, but 3.9-3.12 is required (no piper-phonemize wheels for newer)."
     echo "  Set PYTHON_MODULE in config.sh (see: module avail python)."; exit 1 ;;
esac
GLIBC=$(python3 -c 'import platform; print(platform.libc_ver()[1] or "unknown")')
echo "  glibc $GLIBC (piper-phonemize needs 2.28 or newer)"

mkdir -p "$WORK_DIR" "$WORK_DIR/my_voice"     # my_voice: where your recordings get uploaded
[ -d "$WORK_DIR/venv" ] || python3 -m venv "$WORK_DIR/venv"
source "$WORK_DIR/venv/bin/activate"
pip install -q --upgrade pip
# Order matters (same as the Colab notebook): the Py3.12-compatible phonemizer first,
# torch pinned, everything else, then piper-tts last with --no-deps so nothing clobbers it.
pip install -q piper-phonemize-cross
# CUDA 12.6 build: the default (CUDA 12.8) build of torch 2.8 dropped V100 (Volta) GPUs; this one runs on both V100 and A100
pip install -q torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu126
pip install -q webrtcvad-wheels "mutagen==1.47.0" torchinfo torchmetrics pyyaml tqdm "datasets<4" \
    soundfile audiomentations torch_audiomentations pronouncing onnxruntime onnx speechbrain \
    acoustics "scipy<1.17" scikit-learn requests huggingface_hub imageio-ffmpeg
pip install -q --no-deps piper-tts

echo "== 2/5 Code + voice model"
cd "$WORK_DIR"
if [ ! -f piper-sample-generator/generate_samples.py ]; then
  rm -rf piper-sample-generator
  git clone -q https://github.com/rhasspy/piper-sample-generator
fi
# Last revision with the flat generate_samples.py that openwakeword's train.py imports
(cd piper-sample-generator && git fetch -q --all && git checkout -q "1a8c49bd29b3a132721086ee88f2253f788594a8^")
mkdir -p piper-sample-generator/models
VOICE=piper-sample-generator/models/en_US-libritts_r-medium.pt
if [ "$(stat -c%s "$VOICE" 2>/dev/null || echo 0)" -lt 100000000 ]; then
  wget -q --show-progress --tries=5 -O "$VOICE" \
    https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt
fi
if [ ! -f openwakeword/openwakeword/train.py ]; then
  rm -rf openwakeword
  git clone -q https://github.com/dscripka/openwakeword
fi
# --no-deps: its declared deps include tflite-runtime, which has no wheels for 3.12
pip install -q --no-deps -e openwakeword
RES=openwakeword/openwakeword/resources/models
mkdir -p "$RES"
for f in embedding_model.onnx embedding_model.tflite melspectrogram.onnx melspectrogram.tflite; do
  [ -s "$RES/$f" ] || wget -q --tries=5 -O "$RES/$f" "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$f"
done

echo "== 3/5 Compatibility patches"
python "$HERE/apply_patches.py"

echo "== 4/5 Training data (~25 GB: room acoustics, music, ACAV100M speech features)"
python "$HERE/download_data.py"

echo "== 5/5 Pre-flight check"
python "$HERE/preflight.py"

echo
echo "Setup done. Now submit the training job from this folder:"
echo "    cd $HERE && sbatch train.slurm"
