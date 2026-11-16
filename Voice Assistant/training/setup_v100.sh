#!/bin/bash
# Run ONCE on the login node (needs internet), after setup.sh, to let sft_v100.slurm train on V100s:
#     cd $WORK/triton_brain/code/training && bash setup_v100.sh
# Why a second environment: setup.sh's venv has vLLM's PyTorch (CUDA 12.8 build), which no longer
# includes V100 support. Training doesn't need vLLM, so this venv has the CUDA 12.6 PyTorch, which
# still runs on V100s. Models, data and llama.cpp are shared with setup.sh (~6 GB extra).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"

module load "$PYTHON_MODULE" 2>/dev/null || echo "  (couldn't load '$PYTHON_MODULE'; using $(python3 --version 2>&1))"
[ -d "$WORK_DIR/venv-v100" ] || python3 -m venv "$WORK_DIR/venv-v100"
source "$WORK_DIR/venv-v100/bin/activate"
# "python -m pip", not "pip": with the conda module loaded, a bare pip can be conda's and install
# outside this venv (then the job says "No module named 'torch'")
python -m ensurepip -q 2>/dev/null || true
python -m pip install -q --upgrade pip
python -m pip install torch --index-url https://download.pytorch.org/whl/cu126
python -m pip install -q trl peft accelerate datasets sentencepiece protobuf bitsandbytes
python -m pip install -q -e "$WORK_DIR/llama.cpp/gguf-py"
python -c "import torch; print('torch', torch.__version__, 'built for', torch.cuda.get_arch_list())"
echo "V100 environment ready. Submit with: STUDENT=Qwen/Qwen3-1.7B sbatch sft_v100.slurm"
