# Settings shared by setup.sh and train.slurm. Edit to match your cluster.

# Where everything goes: code, Python env, ~25 GB of training data, caches, the finished model.
# Needs ~40 GB free at peak. Defaults to the cluster's $WORK filesystem (big quota) so the small
# home quota stays untouched; falls back to $HOME only if $WORK isn't set.
WORK_DIR="${WORK_DIR:-${WORK:-$HOME}/hey_nova}"

# Keep every cache and temp file inside WORK_DIR too (by default pip, Hugging Face and others
# write gigabytes to ~/.cache in your home folder)
export PIP_CACHE_DIR="$WORK_DIR/cache/pip"
export HF_HOME="$WORK_DIR/cache/huggingface"
export XDG_CACHE_HOME="$WORK_DIR/cache"
export TMPDIR="$WORK_DIR/tmp"
mkdir -p "$PIP_CACHE_DIR" "$HF_HOME" "$TMPDIR"

# Python 3.9-3.12 module (3.13+ won't work: piper-phonemize has no wheels for it).
# See what exists with:  module avail python
PYTHON_MODULE="python/3.12-conda"
