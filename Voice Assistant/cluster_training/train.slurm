#!/bin/bash
# Trains a wake word on one GPU, fully offline. Submit from this folder AFTER setup.sh succeeded:
#     sbatch train.slurm                              ("hey echo", this PC)
#     WAKE_PHRASE="hey triton" sbatch train.slurm     ("hey triton", the Nova Launcher PC)
# Watch progress with:   tail -f wakeword_<jobid>.log
#
# Adjust the GPU request lines to your cluster's conventions (ask your HPC docs / `sinfo -o "%P %G"`).
#SBATCH --job-name=wakeword
#SBATCH --partition=a100
#SBATCH --gres=gpu:a100:1
# No --cpus-per-task / --mem: TinyGPU hands out a fixed share of CPU cores and RAM per GPU
#SBATCH --time=06:00:00
#SBATCH --output=wakeword_%j.log

set -euo pipefail
HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
source "$HERE/config.sh"
export WORK_DIR

module load "$PYTHON_MODULE" 2>/dev/null || true
source "$WORK_DIR/venv/bin/activate"

# No internet on compute nodes: make sure nothing even tries
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export PYTHONUNBUFFERED=1

echo "Job $SLURM_JOB_ID on $(hostname), $(date): '${WAKE_PHRASE:-hey echo}'${GENERATE_ONLY:+ (clips only)}"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv || true

# Fail within seconds (not hours in) if this torch build can't run on the GPU we got
python -c "import torch; x = torch.ones(8, device='cuda'); print('  torch', torch.__version__, 'OK on', torch.cuda.get_device_name(0), '-', float((x * 2).sum()))" || {
    echo "torch can't use this GPU. On a V100, re-install the CUDA 12.6 build in the venv (login node):"
    echo "  source $WORK_DIR/venv/bin/activate && pip install --force-reinstall --no-deps torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu126"
    exit 1
}

python "$HERE/train_hey_nova.py"

echo "Finished $(date)"
