#!/bin/bash
# Run ONCE on the cluster LOGIN node (needs internet; GPU nodes have none), inside tmux:
#     cd $WORK/triton_brain/code/training && bash setup.sh
# Downloads ~45 GB (teacher + students). Safe to re-run: every step skips what's already done.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"

echo "== 1/4 Python environment: $WORK_DIR/venv"
module load "$PYTHON_MODULE" 2>/dev/null || echo "  (couldn't load '$PYTHON_MODULE'; using $(python3 --version 2>&1))"
[ -d "$WORK_DIR/venv" ] || python3 -m venv "$WORK_DIR/venv"
source "$WORK_DIR/venv/bin/activate"
pip install -q --upgrade pip
# vLLM first: it pins the torch build it needs (CUDA 12.8+: runs on the A100s, not the V100s).
# Training libraries after, so they reuse that torch instead of replacing it.
pip install -q vllm
pip install -q trl peft accelerate datasets sentencepiece protobuf huggingface_hub

echo "== 2/4 llama.cpp's converter (Hugging Face model -> GGUF, CPU only; quantizing happens on the laptop)"
cd "$WORK_DIR"
[ -d llama.cpp ] || git clone -q --depth 1 https://github.com/ggml-org/llama.cpp
pip install -q -e llama.cpp/gguf-py

echo "== 3/4 Models -> $WORK_DIR/models"
mkdir -p "$WORK_DIR/models"
for repo in $TEACHER $STUDENTS; do
  echo "  $repo"
  hf download "$repo" --local-dir "$WORK_DIR/models/${repo##*/}" --quiet >/dev/null
done

echo "== 4/4 Pre-flight"
cd "$HERE"
WORK_DIR="$WORK_DIR" MODEL_NAMES="$TEACHER $STUDENTS" python preflight.py

echo
echo "Setup done. Next (from $HERE):"
echo "    sbatch gen_data.slurm          # ~2-4 h: writes the training sentences"
echo "then the training jobs (see README.md)."
