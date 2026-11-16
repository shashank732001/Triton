#!/bin/bash
# Same job as train.slurm, on a V100 instead of an A100 (use it when the A100s are busy; ~1.5-2x slower).
#     WAKE_PHRASE="hey triton" sbatch train_v100.slurm
#     sbatch train_v100.slurm                          ("hey echo")
# Watch progress with:   tail -f wakeword_<jobid>.log
#SBATCH --job-name=wakeword
#SBATCH --partition=v100
#SBATCH --gres=gpu:v100:1
# No --cpus-per-task / --mem: TinyGPU hands out a fixed share of CPU cores and RAM per GPU
#SBATCH --time=08:00:00
#SBATCH --output=wakeword_%j.log

# The job itself lives in train.slurm (its #SBATCH lines are just comments when sourced)
source "${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}/train.slurm"
