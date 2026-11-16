#!/bin/bash
# Writes the training data with the teacher model, fully offline. Submit from this folder after setup.sh:
#     sbatch gen_data.slurm
#     SEEDS=1500 sbatch gen_data.slurm      # more data (default 800 seeds per tool -> ~90k examples)
# Watch: tail -f gendata_<jobid>.log
#
# A100 only: vLLM's PyTorch build doesn't run on V100s (and they lack bf16/FP8). The max wall time
# is 24 h; this needs ~2-4 h.
#SBATCH --job-name=triton-data
#SBATCH --partition=a100
#SBATCH --gres=gpu:a100:1
#SBATCH --time=08:00:00
#SBATCH --output=gendata_%j.log

set -euo pipefail
HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
source "$HERE/config.sh"
export WORK_DIR
module load "$PYTHON_MODULE" 2>/dev/null || true
source "$WORK_DIR/venv/bin/activate"
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1

echo "Job $SLURM_JOB_ID on $(hostname), $(date)"
nvidia-smi --query-gpu=name,memory.total --format=csv || true
python -c "import torch; torch.ones(1, device='cuda'); print('  torch', torch.__version__, 'OK on', torch.cuda.get_device_name(0))"
cd "$HERE"
python gen_data.py --teacher "${TEACHER##*/}" --seeds-per-tool "${SEEDS:-800}" --paraphrases "${PARAPHRASES:-6}"
echo "Finished $(date). Data in $WORK_DIR/data"
