#!/bin/bash
# Triton-Mini v2 training data (gen_converse.py): act / talk / ask again, with the screen line and follow-ups.
# Needs round 2's data ($WORK_DIR/data from gen_data.slurm). Submit from this folder:
#     sbatch gen_converse.slurm
#     V3=1 sbatch gen_converse.slurm      Triton-Mini v3 (data_v3/): reuses data_v2's teacher work, adds the
#                                         contrast rows (gen_v3.py) and a teacher re-check of every command
# Watch: tail -f genconv_<jobid>.log      (~1-2 h on one A100; teacher steps are cached in data_v2/)
#SBATCH --job-name=triton-conv
#SBATCH --partition=a100
#SBATCH --gres=gpu:a100:1
#SBATCH --time=06:00:00
#SBATCH --output=genconv_%j.log

set -euo pipefail
HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
source "$HERE/config.sh"
export WORK_DIR
module load "$PYTHON_MODULE" 2>/dev/null || true
source "$WORK_DIR/venv/bin/activate"
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1

echo "Job $SLURM_JOB_ID on $(hostname), $(date)"
test -f "$WORK_DIR/data/train.jsonl" || { echo "No round-2 data in $WORK_DIR/data: run gen_data.slurm first"; exit 1; }
cd "$HERE"
python gen_converse.py --teacher "${TEACHER##*/}" ${V3:+--v3}
echo "Finished $(date). Data in $WORK_DIR/$([ -n "${V3:-}" ] && echo data_v3 || echo data_v2)"
