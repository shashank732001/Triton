# Settings shared by setup.sh and the Slurm jobs. Edit to match your cluster.

# Everything (Python env, models, data, checkpoints, caches) goes here: needs ~80 GB at peak.
# Defaults to $WORK (big quota), like the wake-word training; separate folder so the two don't mix.
WORK_DIR="${WORK_DIR:-${WORK:-$HOME}/triton_brain}"

export PIP_CACHE_DIR="$WORK_DIR/cache/pip"
export HF_HOME="$WORK_DIR/cache/huggingface"
export XDG_CACHE_HOME="$WORK_DIR/cache"
export TMPDIR="$WORK_DIR/tmp"
export TRITON_CODE="$WORK_DIR/code"          # the uploaded Voice Assistant code (tools/, brain/, bench/, training/)
mkdir -p "$PIP_CACHE_DIR" "$HF_HOME" "$TMPDIR"

PYTHON_MODULE="python/3.12-conda"

# Models (downloaded once by setup.sh into $WORK_DIR/models/<last part of the name>)
TEACHER="Qwen/Qwen3-30B-A3B-Instruct-2507-FP8"   # writes the training sentences (29 GB, 3B active: fast)
STUDENTS="Qwen/Qwen3-1.7B Qwen/Qwen3-0.6B Qwen/Qwen3-4B-Instruct-2507"
#   1.7B: Triton-Mini for the N95   0.6B: ultra-compact try   4B: the laptop "big brain" (LoRA)
