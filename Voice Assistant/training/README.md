# Train Triton-Mini on the university cluster

This pipeline turns the stock **Qwen3-1.7B** (68% on our 229-sentence test set) into **Triton-Mini**.
Triton-Mini understands your voice commands without a long prompt. The same pipeline also trains:
- an ultra-compact **0.6B** version
- a **4B** "big brain" for the laptop

```
setup.sh (login node, once) -> gen_data.slurm (teacher writes ~90k training sentences, 2-4 h)
     -> 3 x sft.slurm in parallel (train + score + GGUF, 1-4 h each) -> laptop: quantize + bench
```

Everything goes into `$WORK/triton_brain` (about 80 GB at peak). The wake-word folder `$WORK/hey_nova` is not touched.

## 1. Upload the code (laptop, PowerShell)

```
ssh iwem119h@tinyx "mkdir -p /home/woody/iwem/iwem119h/triton_brain/code"
cd "C:\Users\shash\TritonBundle\Voice Assistant"
scp -r tools brain bench training iwem119h@tinyx:/home/woody/iwem/iwem119h/triton_brain/code/
```

Re-run the `scp` whenever the code changes. Your data and models on the cluster stay where they are.

## 2. Setup (cluster LOGIN node, once, about 30-60 min)

GPU nodes have no internet, so everything is downloaded here first, roughly 45 GB of models:

```
cd $WORK/triton_brain/code/training
tmux new -s triton            # so it keeps going if you disconnect (reattach: tmux attach -t triton)
bash setup.sh
```

It ends with `Pre-flight PASSED`. If a check says FAIL, send me that output.

## 3. Submit everything in one go (cluster)

```
cd $WORK/triton_brain/code/training
jid=$(sbatch --parsable gen_data.slurm)
STUDENT=Qwen/Qwen3-1.7B                         sbatch --dependency=afterok:$jid sft.slurm
STUDENT=Qwen/Qwen3-0.6B LR=2e-5                 sbatch --dependency=afterok:$jid sft.slurm
STUDENT=Qwen/Qwen3-4B-Instruct-2507 LORA=1      sbatch --dependency=afterok:$jid sft.slurm
squeue --me                                     # PD = waiting, R = running
```

- The three training jobs wait for the data job, then run **at the same time** on three A100s.
- **gen_data and scoring are A100 only:** vLLM's PyTorch build doesn't support V100s, and they lack bf16 and FP8.
  **Training can use V100s** when the A100 quota is full: run `bash setup_v100.sh` once, then
  `sbatch sft_v100.slurm` with the same `STUDENT=...` arguments. It uses fp16 and 8-bit Adam, is 2–3x slower, and skips scoring.
- **Check every log.** A job that crashed still disappears from `squeue`. The first runs all died on
  `warmup_ratio`, which newer transformers removed; `sft.py` now uses `warmup_steps`.
- **Using the rest of your 24 h:** once the first results are in, try variations side by side. Give each run its own `NAME` so they don't overwrite each other:
  ```
  STUDENT=Qwen/Qwen3-1.7B EPOCHS=3 NAME=Qwen3-1.7B-e3     sbatch sft.slurm
  STUDENT=Qwen/Qwen3-1.7B LR=2e-5  NAME=Qwen3-1.7B-lr2e5  sbatch sft.slurm
  SEEDS=1500 sbatch gen_data.slurm      # more data; delete $WORK/triton_brain/data first
  ```

## 4. Check the results (cluster)

```
tail -f gendata_*.log    # pools, "dropped: ...", then 12 example sentences with their labels
tail -f sft_*.log        # training loss falls, then scoring prints e.g.
                         # == Qwen3-1.7B on eval (229 items, compact prompt): accuracy 93.4% ...
```

- **eval:** the same 229 sentences as on the laptop. None of them appear in the training data.
  - The stock 1.7B scored 68% there with a long prompt.
  - Aim for 90% or more with the one-line prompt.
- **val:** held-back generated sentences. These usually score higher, because they're similar to the training data.
- If eval is much lower than val, the model learned the teacher's style rather than the task. Send me the `miss` lines.
- To compare against the stock model on the GPU, run this inside a GPU job:
  `python evaluate.py Qwen/Qwen3-1.7B --prompt full`

## 5. Bring it home (laptop, PowerShell)

```
mkdir C:\Users\shash\TritonModels\trained
scp "iwem119h@tinyx:/home/woody/iwem/iwem119h/triton_brain/out/*" C:\Users\shash\TritonModels\trained\
```

## 6. Compress and measure (laptop)

```
cd "C:\Users\shash\TritonBundle\Voice Assistant"
.venv\Scripts\python.exe bench\quantize.py C:\Users\shash\TritonModels\trained\triton-Qwen3-1.7B-f16.gguf
.venv\Scripts\python.exe bench\bench.py llama "C:\Users\shash\TritonModels\trained\triton-Qwen3-1.7B-*.gguf" --prompt compact --label finetuned
```

`quantize.py` makes 8, 5, 4 and 3-bit versions (Q8_0, Q5_K_M, Q4_K_M, Q3_K_M, IQ4_XS, IQ3_M). It uses an importance matrix built from your own data. `bench.py` adds a row for each version to `bench/results/results.csv`. That's the compression experiment: accuracy vs size vs speed. Pick the smallest version that stays above about 90%.

## Cleaning up

Model checkpoints are under `$WORK/triton_brain/runs`, a few GB per run. Delete old runs you don't need. To remove everything: `rm -rf $WORK/triton_brain`.
