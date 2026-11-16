#!/bin/bash
# sft.slurm on a V100 instead of an A100 (for when the group's A100 quota is full). Same arguments:
#     STUDENT=Qwen/Qwen3-0.6B LR=2e-5 sbatch sft_v100.slurm             # quickest: try this one first
#     STUDENT=Qwen/Qwen3-1.7B sbatch sft_v100.slurm
#     STUDENT=Qwen/Qwen3-4B-Instruct-2507 LORA=1 sbatch sft_v100.slurm
# Needs setup_v100.sh once. fp16 instead of bf16 and 8-bit Adam (sft.py picks them on its own);
# roughly 2-3x slower than an A100. No scoring step here: evaluate.py needs vLLM, which needs an
# A100; the laptop benchmark (bench.py --prompt compact) scores the GGUF instead.
# Watch: tail -f sft_<jobid>.log   If the loss shows nan in the first few hundred steps, fp16
# overflowed: use the A100 job for that model.
#SBATCH --job-name=triton-sft-v100
#SBATCH --partition=v100
#SBATCH --gres=gpu:v100:1
#SBATCH --time=24:00:00
#SBATCH --output=sft_%j.log

set -euo pipefail
HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
source "$HERE/config.sh"
export WORK_DIR
module load "$PYTHON_MODULE" 2>/dev/null || true
source "$WORK_DIR/venv-v100/bin/activate"
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false

: "${STUDENT:?set STUDENT, e.g. STUDENT=Qwen/Qwen3-1.7B sbatch sft_v100.slurm}"
NAME="${NAME:-${STUDENT##*/}${LORA:+-lora}}"
echo "Job $SLURM_JOB_ID on $(hostname), $(date): $NAME"
nvidia-smi --query-gpu=name,memory.total --format=csv || true
python -c "import torch; torch.ones(1, device='cuda'); print('  torch', torch.__version__, 'OK on', torch.cuda.get_device_name(0))"
cd "$HERE"

echo "== 1/2 training"
python sft.py --student "$STUDENT" --name "$NAME" ${LORA:+--lora} --epochs "${EPOCHS:-2}" ${LR:+--lr "$LR"}

echo "== 2/2 GGUF (f16; the laptop quantizes it)"
mkdir -p "$WORK_DIR/out"
python "$WORK_DIR/llama.cpp/convert_hf_to_gguf.py" "$WORK_DIR/runs/$NAME/final" \
    --outtype f16 --outfile "$WORK_DIR/out/triton-$NAME-f16.gguf"
cp "$WORK_DIR/data/val.jsonl" "$WORK_DIR/out/val.jsonl"
ls -lh "$WORK_DIR/out"
echo "Finished $(date)"
