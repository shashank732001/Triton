#!/bin/bash
# Fine-tunes one student, scores it, and converts it to GGUF for the laptop. Submit after gen_data:
#     STUDENT=Qwen/Qwen3-1.7B sbatch sft.slurm                          # Triton-Mini (full fine-tune)
#     STUDENT=Qwen/Qwen3-0.6B LR=2e-5 sbatch sft.slurm                  # ultra-compact
#     STUDENT=Qwen/Qwen3-4B-Instruct-2507 LORA=1 sbatch sft.slurm       # laptop "big brain"
# Optional: EPOCHS=3, LR=..., NAME=... (to keep runs with different settings apart)
# Triton-Mini v2 (act / talk / ask again, after gen_converse.slurm): add DATA=data_v2, e.g.
#     DATA=data_v2 STUDENT=Qwen/Qwen3-0.6B LR=2e-5 sbatch sft.slurm
#   -> out/triton-converse-<name>-f16.gguf ("converse" in the name tells the assistant which prompt to use)
# Triton-Mini v3 (after V3=1 sbatch gen_converse.slurm):
#     DATA=data_v3 STUDENT=Qwen/Qwen3-0.6B LR=2e-5 sbatch sft.slurm   -> out/triton-converse-v3-Qwen3-0.6B-f16.gguf
# Watch: tail -f sft_<jobid>.log
#
# A100 only, as in gen_data.slurm. 1.7B takes ~1-2 h, 0.6B under 1 h, 4B LoRA ~3-4 h.
#SBATCH --job-name=triton-sft
#SBATCH --partition=a100
#SBATCH --gres=gpu:a100:1
#SBATCH --time=10:00:00
#SBATCH --output=sft_%j.log

set -euo pipefail
HERE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
source "$HERE/config.sh"
export WORK_DIR
module load "$PYTHON_MODULE" 2>/dev/null || true
source "$WORK_DIR/venv/bin/activate"
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false

: "${STUDENT:?set STUDENT, e.g. STUDENT=Qwen/Qwen3-1.7B sbatch sft.slurm}"
NAME="${NAME:-${STUDENT##*/}${LORA:+-lora}}"
GGUF="triton-$NAME-f16.gguf"
if [ -n "${DATA:-}" ]; then                 # data_v2 -> converse-<name>; data_v3 -> converse-v3-<name> (keeps v2)
    V="${DATA#data_}"
    TAG="converse"; [ "$V" != "v2" ] && TAG="converse-$V"
    GGUF="triton-$TAG-$NAME-f16.gguf"; NAME="$TAG-$NAME"
fi
echo "Job $SLURM_JOB_ID on $(hostname), $(date): $NAME"
nvidia-smi --query-gpu=name,memory.total --format=csv || true
python -c "import torch; torch.ones(1, device='cuda'); print('  torch', torch.__version__, 'OK on', torch.cuda.get_device_name(0))"
cd "$HERE"

echo "== 1/3 training"
python sft.py --student "$STUDENT" --name "$NAME" ${LORA:+--lora} --epochs "${EPOCHS:-2}" ${LR:+--lr "$LR"}     ${DATA:+--data "$DATA"}

echo "== 2/3 scoring"
if [ -z "${DATA:-}" ]; then python evaluate.py "$WORK_DIR/runs/$NAME/final"
else echo "  (v2: scored on the laptop with bench/real_bench.py, on your real commands)"; fi

echo "== 3/3 GGUF (f16; the laptop quantizes it)"
mkdir -p "$WORK_DIR/out"
python "$WORK_DIR/llama.cpp/convert_hf_to_gguf.py" "$WORK_DIR/runs/$NAME/final" \
    --outtype f16 --outfile "$WORK_DIR/out/$GGUF"
cp "$WORK_DIR/${DATA:-data}/val.jsonl" "$WORK_DIR/out/val${DATA:+_${DATA#data_}}.jsonl"
ls -lh "$WORK_DIR/out"
echo "Finished $(date)"
