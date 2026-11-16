# Triton Voice Assistant: how it all works

**Triton** is a voice assistant that controls Windows PCs. You say the wake word, then a command, and it
acts and answers you out loud. Everything runs locally:

- **Ears:** speech-to-text with Vosk plus Whisper.
- **Brain:** a small LLM that turns sentences into tool calls.
- **Hands:** pywinauto and Windows actions.
- **Voice:** text-to-speech with Piper.

We train the brain and the wake words ourselves on the FAU cluster.

This file is the overview: what each part is, which models we picked and why (with measurements), how
the training works, and where everything lives. Step-by-step commands for each training are in:

| Topic | Detailed guide |
|---|---|
| Brain training (teacher, students, quantizing) | [training/README.md](training/README.md) |
| Wake-word training | [cluster_training/README.md](cluster_training/README.md) |
| Original design plan and milestones | [docs/TRITON_BRAIN_PLAN.md](docs/TRITON_BRAIN_PLAN.md) |

*Last updated 2026-11-04.*

---

## 1. Hardware

| Device | Specs | Role |
|---|---|---|
| **N95 NUC** | Intel N95 (4 cores), 8 GB RAM, Windows 22H2 | Final target: runs the assistant plus the small brain (fine-tuned 1.7B). Comes last; laptop first. |
| **Laptop** | Dell Latitude 3420, i5-1135G7 (4C/8T), 16 GB (2x8 GB DDR4-3200, dual channel), Iris Xe, Thunderbolt 4 | Development, benchmarks, and later the **"big brain"** the N95 sends hard requests to |
| **FAU cluster** (tinyGPU, `tinyx`) | A100 40 GB and V100 32 GB GPUs; 24 h max per job; GPU nodes have **no internet** | Generates training data, trains models |

SSH: `ssh tinyx`, user `iwem119h`, `$WORK` = `/home/woody/iwem/iwem119h`.

---

## 2. How a command flows

```
mic -> wake word (openWakeWord "hey_triton") -> record command until you stop (endpoint.py, Silero VAD)
       (meanwhile: what's on screen, screen.py)
    -> Whisper (speech only) -> rules (commands.py)       sure? -> run it (~0 ms)
    -> each part on its own (brain/clauses.py)           all placed? -> run it (~0 ms)
    -> small local model (0.6B)                          plausible? -> run it (~0.5 s)
    -> laptop big brain, with the screen line            answer within 12 s, else say so plainly
    -> partly placed? do that part, ask about the rest
    -> tools (tools/handlers.py, pywinauto, actions.py) -> do it
    -> Piper speaks the result ("Volume 30", "It's 4:15 PM")
```

The model's answers are **forced into the tool-call grammar** (JSON schema from `tools/schema.py`),
so even a tiny model can't produce invalid output. The only question is whether it picks the right tools.

### The parts and their files

| Part | File(s) | Model / tech | Status |
|---|---|---|---|
| Wake word | `assistant.py`, `models/hey_echo.onnx` | openWakeWord, threshold 0.2 (`settings.json` → `wake_threshold`) | Works, but misses ~half of your takes. **Retrain with new recordings** (section 6) |
| Rules | `commands.py`, `brain/rules.py` | Hand-written | Instant; handles 41.5% of the test set alone |
| Ears | `transcriber.py`, `endpoint.py`, `echo.py` | sherpa-onnx **whisper-small** int8 (alone since 5l), echo-cancelled mic (models in `%USERPROFILE%\TritonModels\stt`); end of speech by Silero VAD + loudness + new words | Works; listening no longer cut at 6 s (section 5k) |
| Screen context | `screen.py`, `brain/clauses.py` | UI Automation: window in front + clickable names | New 2026-11-04 (section 5k) |
| Brain | `brain/` (router, llm, prompt), `tools/` (schema, handlers, uia, safety) | llama.cpp `llama-server` + GGUF model | Stock Qwen3-1.7B for now; fine-tuned models pending |
| Screen sharing | `tools/screenshare.py` | Fixed phrases → Triton Screen Share's control pipe | New 2026-11-02 (section 5f) |
| Voice | `speaker.py` | **Kokoro v1.0, speaker `bm_george`** (sherpa-onnx; `TritonModels\tts\kokoro-multi-lang-v1_0`); Piper voices still work | Works; the wake word interrupts speech (barge-in) |

Try the brain without the microphone: `.venv\Scripts\python.exe brain\try_it.py --speak`

---

## 3. Choosing the models (measured on the laptop)

All brain numbers come from `bench/bench.py`: **229 test commands** (`bench/eval/eval.jsonl`), each answer
forced into the tool grammar, with the full prompt (tool list plus examples). "Typical" is the median
time per command; "slow" is the 90th percentile.
Results: `bench/results/results.csv`.

### Brain: off-the-shelf models, not fine-tuned

| Model (Q4 unless noted) | Right | Typical / slow | Writes (tokens/s) | RAM | Notes |
|---|---|---|---|---|---|
| rules only (commands.py) | 41.5% | 0 s | – | – | The fast path in front of every model |
| Qwen3-0.6B | 54% (Q4) / 59% (Q8) | 1.1 s | – | <1 GB | Too weak untrained; **86.0% after fine-tuning** (section 4) |
| **Qwen3-1.7B** | 68.1% | 1.5 s / 2.4 s | 18 | 2.3 GB | **In use now** (`triton-mini.gguf` slot), student for the N95 |
| Qwen3-4B-Instruct-2507 | 86.9% | 3.2 s / 5.4 s | 7.9 | 4.8 GB | Student for the laptop (LoRA) |
| Qwen3.5-4B | 87.3% | 5.9 s / 12.3 s | 4.5 | 4.4 GB | No better than Qwen3-4B, and slower |
| **Qwen3.5-9B** | **92.6%** | 5.7 s / 8.2 s | 4.8 | 7.9 GB | **Best practical laptop "big brain"** |
| **Gemma 4 12B** | **95.6%** | 12.4 s / 20 s | 2.9 | 9.1 GB | Smartest, but too slow on CPU. Great with an eGPU |
| gpt-oss-20b (MoE, MXFP4) | 86.9% | 7.1 s / 11.5 s | 6.1 | 9.8 GB | Deleted: not smarter, more RAM |

What we learned:
- On a CPU, **writing speed ≈ memory speed ÷ model size**. Twice the size means half the tokens per second.
- **Mixture-of-experts** (MoE) models only read their active part for each token. gpt-oss-20b uses ~3.6B
  active weights, so it writes as fast as a 4B model. But all 13 GB must still fit in RAM.
- **Reading the prompt** (prefill) is the slow part: 20–75 s for the ~750-token prompt. llama.cpp
  caches it after the first request, so keep the server running. Fine-tuned models use a one-line
  prompt instead, which removes most of this.
- RAM on the laptop: Windows plus normal apps use ~8 GB. In "brain box" mode (browsers closed, Dell bloat
  removed) Windows needs ~3.5–4 GB, so the 9B plus Whisper and Piper fit comfortably.

### Ears: speech-to-text (`bench/stt_bench.py`, 60 sentences spoken by Piper voices)

| Engine | Word errors clean / noisy | Sentences fully right | Time | RAM |
|---|---|---|---|---|
| Vosk (old) | 13.3% / 21.1% | 72% / 57% | – | 190 MB |
| moonshine-tiny | 9.2% / 15.1% | 75% / 65% | 0.13 s | 200 MB |
| **whisper-base** (chosen) | **2.3% / 3.7%** | **93% / 90%** | 0.6 s | 415 MB |
| whisper-small | 4.1% / 7.8% | 90% / 82% | 1.5 s | 850 MB |

### Voice: text-to-speech (`bench/tts_bench.py`, laptop, 2 threads; the N95 is ~1.5–2x slower)

| Voice | Short reply ready in | Speed (RTF; <1 = faster than it talks) | Size | Notes |
|---|---|---|---|---|
| Piper `en_GB-alan-medium` (old) | 0.10–0.14 s | 0.07 | 63 MB | Fast, robotic |
| Piper `en_US-ryan-high` / `en_GB-cori-high` (int8) | 0.3–0.6 s | 0.4 | 40 MB | Clearer |
| Kitten nano v0.8 int8 | 0.6–0.9 s | 0.5 | 45 MB | Tiny |
| Kitten mini v0.8 | ~4 s first sentence | 1.0 | 96 MB | Too slow |
| Kokoro v0.19 int8 | ~5–6 s first sentence | 1.7–2.4 | 152 MB | int8 is *slower* on this CPU |
| **Kokoro v1.0 (fp32), `bm_george`** (chosen) / `af_heart` | **0.8–1.4 s** | 0.7–0.85 | 384 MB | **Most natural** |

- Samples: `voice_samples\tts_compare\numbers_*.wav`.
- **ElevenLabs isn't an option:** it's cloud-only (no downloadable voices), and every reply would send text to its servers.
- **Gotchas found:**
  - sherpa-onnx 2.0.1 won't fall back to espeak, so Kokoro and Kitten need a lexicon. Kokoro v1.0's `lexicon-us-en.txt` /
    `lexicon-gb-en.txt` were copied into the Kitten and Kokoro v0.19 folders.
  - Words missing from the lexicon are **silently skipped**, digits included. So `speaker.spoken()` writes numbers, times and "%"
    as words ("Volume 40%" → "Volume forty percent").
  - Piper "high" voices go through piper-tts, since sherpa's vits path also wants a lexicon.
- The speaker makes the next sentence while the current one plays, so longer answers have no gaps.
- Settings: `"voice": {"name": "kokoro-multi-lang-v1_0", "speaker": "bm_george"}`. Other speakers include `af_heart`, `am_michael`
  and `bf_emma`; `en_GB-alan-medium` brings the old voice back.

### Wake word (`bench/wake_bench.py`, your 50 takes vs 142 other clips)

| Model | Your "Hey Echo"s caught | False wakes |
|---|---|---|
| hey_echo at threshold 0.5 | 21/50 | 0 |
| hey_echo at threshold 0.2 (now) | 27/50 | 0 |
| hey_triton (trained with your voice) | 79/95 | 0 |

→ The fix is retraining hey_echo **with your own recordings**. Checking the wake word with speech-to-text
was tried and dropped: Whisper hears "Hey Echo" as "here you go".

---

## 4. Training the brain (teacher → students)

### The idea: distillation through data

A **big teacher model** writes tens of thousands of example sentences. **Small student models** learn
from them. The students end up good at *our* job (voice commands → tool calls), with no long prompt.

| Role | Model | Why |
|---|---|---|
| **Teacher** | **Qwen3-30B-A3B-Instruct-2507-FP8** (30B MoE, 3B active, 29 GB) | Big enough to write natural, varied English; fast because only 3B are active; FP8 fits one A100 |
| **Student 1: Triton-Mini** | **Qwen3-1.7B**, full fine-tune | For the **N95**: small, fast. Goal: from 68% to 90%+ |
| **Student 2: ultra-compact** | **Qwen3-0.6B**, full fine-tune (lr 2e-5) | Experiment: how small can it go? |
| **Student 3: laptop big brain** | **Qwen3-4B-Instruct-2507**, **LoRA** | For the **laptop**: smarter, still fits |

### Step 1: making the data (`training/gen_data.py`, `gen_data.slurm`, ~2–4 h on one A100)

**The labels come from code, not from the teacher.** We pick the correct tool calls first. The teacher
only writes ways a person might *say* them, so every label is right by construction.

1. **pools:** the teacher lists things code can't invent well: search queries, button names, dictated text,
   general questions, ways to say "never mind".
2. **seeds:** code samples tool calls from `tools/schema.py` plus those pools (800 per tool by default):
   - single commands and two-step commands
   - incomplete requests → `clarify`
   - not a command → `escalate`
   - call-offs → `cancel`
3. **paraphrase:** the teacher writes ~6 ways to say each seed.
4. **finish:**
   - drop sentences missing their call's words (e.g. the app name)
   - add realistic **speech-recognition mistakes** ("open crome")
   - remove anything that's in the test set
   - split into `train.jsonl` / `val.jsonl`. About **90k examples**.

Data lives in `$WORK/triton_brain/data/`. Each row looks like
`{"text": "turn it up to forty", "calls": [{"tool": "volume", "args": {...}}]}`.

### Step 2: fine-tuning (`training/sft.py`, `sft.slurm` on A100 / `sft_v100.slurm` on V100)

- **What the model learns:** to answer the **one-line "compact" system prompt** with the right JSON calls.
  The tool list now lives in its weights (the "prompt diet"). That's why fine-tuned models are
  benchmarked with `--prompt compact`.
- **Loss on the answer only** (`completion_only_loss`), not on the request.
- **Settings:**
  - 2 epochs, effective batch 64, max 384 tokens
  - cosine learning-rate schedule with 3% warm-up
  - keep the checkpoint with the best validation loss
- **Learning rate:**
  - full fine-tune: 1e-5 (0.6B: 2e-5)
  - LoRA: 2e-4, with rank 32, alpha 64, on all attention and MLP layers
- **Precision:**
  - Full fine-tune keeps **fp32 master weights** and does the maths in bf16. In pure bf16, updates
    this small round away to nothing.
  - LoRA keeps the frozen base in bf16.
- **On V100s** (no bf16, 32 GB): fp16 with loss scaling, 8-bit Adam, 8 examples per step.
  `sft.py` picks this automatically. It needs the separate `venv-v100` from `setup_v100.sh`, because the
  main venv's PyTorch (vLLM's CUDA 12.8 build) doesn't support V100s.
- **After training**, the same job:
  - scores the model on the 229 test commands (`evaluate.py`, A100 only, because it needs vLLM)
  - writes `out/triton-<name>-f16.gguf`

| Student | Rough time on A100 | On V100 |
|---|---|---|
| 0.6B | < 1 h | ~2 h |
| 1.7B | 1–2 h | 3–5 h |
| 4B LoRA | 3–4 h | 8–10 h (keep on A100) |

### Step 3: compress and measure on the laptop

1. Download `out/*` to `C:\Users\shash\TritonModels\trained\`.
2. Compress, then benchmark (the "compression experiment"):
   - `bench\quantize.py` makes Q8_0 / Q5_K_M / Q4_K_M / Q3_K_M / IQ4_XS / IQ3_M versions. It uses an
     **importance matrix** built from our own data, so low-bit versions keep the weights that matter
     for our task.
   - `bench\bench.py llama "<files>" --prompt compact --label finetuned` scores each version.
3. Pick the **smallest version that stays above ~90%**, and copy it to `models\triton-mini.gguf`.

Exact commands: [training/README.md](training/README.md), sections 5–6.

### Current status (2026-10-30)

- ✅ Setup done; teacher data generated (job 1853019): **87,330 train / 2,700 val examples**.
- ❌ The first training jobs all crashed at startup (1853020/21/22 on A100, 1854627/29 on V100).
  The cluster's newer `transformers`/`trl` removed the `warmup_ratio` setting. Fixed in `training/sft.py`:
  warm-up is now given as a number of steps.
- ✅ **Qwen3-0.6B fine-tuned** (job 1855202, A100, 46 min training). The file is in `Voice Assistant\training_out\`, the log in `logs\`.
  - Score: **86.0% on the 229 test commands**, up from 54–59% stock. It matches stock Qwen3-4B (86.9%) at 1/7 the size.
  - 96.2% on the val sentences. The gap means it learned some of the teacher's style; real phrasing is harder.
  - eval_loss fell steadily (0.035 → 0.0062) with no overfitting.
  - Typical misses:
    - "i want to use paint": `switch_to` instead of `open`
    - "close this": `alt+f4` instead of `close`
    - "max volume": `change_volume up` instead of setting it to 100
  - **Compression results on the laptop** (`bench.py --prompt compact`; files in `C:\Users\shash\TritonModels\trained\`):

    | Version | Size | Right | Typical / slow | Writes (tokens/s) | Peak RAM |
    |---|---|---|---|---|---|
    | f16 | 1143 MB | 86.0% | 1.05 s / 1.66 s | 19 | 2.3 GB |
    | Q8_0 | 610 MB | 86.0% | 0.70 s / 1.18 s | 31 | 1.8 GB |
    | Q5_K_M | 424 MB | 86.0% | 0.60 s / 1.15 s | 37 | 1.6 GB |
    | **Q4_K_M** | **378 MB** | **86.0%** | **0.48 s / 0.96 s** | **45** | 1.8 GB |
    | IQ4_XS | 351 MB | 86.5% | 0.51 s / 0.93 s | 40 | 1.4 GB |
    | Q3_K_M | 331 MB | 86.5% | 0.58 s / 1.01 s | 39 | 1.6 GB |
    | IQ3_M | 320 MB | 84.7% | 0.65 s / 0.93 s | 30 | 1.6 GB |

    - **No accuracy is lost down to 3–4 bits**, thanks to the importance matrix. llama.cpp's f16 matches the cluster's 86.0%.
      Only IQ3_M drops (−1.3 points). The +0.5 on IQ4_XS/Q3_K_M is noise.
    - **Q4_K_M is the pick:** the fastest (0.48 s typical, 45 tokens/s), full accuracy, 378 MB.
      It meets the N95 targets (≤500 MB, <1 s on the laptop; expect ~1.5–2x slower on the N95).
    - The one-line prompt is **34 tokens, read in 0.14 s**. The stock models needed 750 tokens and 7–20 s.
      That's the "prompt diet" working.
    - Peak RAM includes llama-server's 4096-token context and buffers, not just the model file.
- ✅ **Qwen3-1.7B fine-tuned** (job 1855199, A100, 56 min training). The file is in `training_out\`, the log in `logs\`.
  - Score: **87.3% on the test commands** (stock: 68.1%) and 96.4% on val. eval_loss reached 0.0060, with no overfitting.
  - **Only +1.3 points over the 0.6B, with the same misses.** The limit is the **training data**, not
    model size: `open` vs `switch_to`, "close this" → `alt+f4`, "max/full volume" → `change_volume up`.
    The next data round should add these patterns.
  - The ~16 `OSError: Device or resource busy ... tmp/pymp-*` tracebacks in the log are harmless. Data-loader
    workers fail to delete their temp folders on the network drive (`TMPDIR=$WORK_DIR/tmp`), and training carries on.
  - **Compression results on the laptop** (`--prompt compact`):

    | Version | Size | Right | Typical / slow | Writes (tokens/s) |
    |---|---|---|---|---|
    | f16 | 3.3 GB | 87.3% | 1.96 s / 2.70 s | 10 |
    | Q8_0 | 1.7 GB | 87.3% | 1.55 s / 2.72 s | 15 |
    | Q5_K_M | 1.2 GB | 87.8% | 1.31 s / 3.91 s | 16 |
    | **Q4_K_M** | **1.06 GB** | **86.5%** | **0.96 s / 1.60 s** | **22** |
    | Q3_K_M | 0.90 GB | 86.0% | 0.98 s / 1.42 s | 22 |
    | IQ4_XS | 0.96 GB | 86.0% | 1.21 s / 1.84 s | 17 |
    | IQ3_M | 0.85 GB | 86.0% | 2.01 s / 3.57 s | 9 |

    - As with the 0.6B, 3–4 bits cost almost nothing (−0.8 points at most).
    - But the 1.7B Q4 is **twice as slow and 3x the size** of the 0.6B Q4 for **+0.5 points**. So **the 0.6B Q4 is the N95 brain** for now.
- ✅ **Qwen3-4B LoRA fine-tuned** (job 1855205, A100, 93 min training; 8 GB f16 file in `training_out\`).
  - Score: **85.2% on the test commands**, slightly *below* stock 4B with the full prompt (86.9%). 96.6% on val.
  - eval_loss reached 0.0066, with no overfitting.

### Round 1 verdict: the data is the limit, not the model size

| Fine-tuned | Test commands | Val |
|---|---|---|
| Qwen3-0.6B | 86.0% | 96.2% |
| Qwen3-1.7B | 87.3% | 96.4% |
| Qwen3-4B LoRA | 85.2% | 96.6% |

All three models land at ~86% and make **the same mistakes**. ~96% on the teacher's sentences against ~86% on ours
means the teacher data teaches different habits from the ones our test set expects. Of the 0.6B's 32 misses, most fall into
a few patterns that **round 2 of `gen_data.py` must cover**:

| Pattern | Example | Model says | Should be |
|---|---|---|---|
| "search for X" | "search for rain sounds" | `escalate` / `type_text` | `web_search` |
| "…and search" after opening YouTube | "open youtube and search for pasta" | `web_search` / `type_text` | `youtube_search` |
| Close the current window | "close this", "exit this program" | `alt+f4` / `clarify` | `close` (no target) |
| Chat or questions | "why is the sky blue", "how are you doing" | `type_text` | `escalate` |
| Polite endings | "that's all thanks" | `type_text` | `cancel` |
| Starting vs focusing an app | "i want to use paint", "bring up spotify" | `switch_to` | `open` |
| Bringing a window forward | "bring notepad to the front" | `window maximize` | `switch_to` |
| Extremes | "max volume" | `change_volume up` | `set_volume 100` |

- `type_text` is clearly over-learned: the model uses it as a fallback for anything unfamiliar.
- A few test labels are debatable and could be loosened instead ("go to sleep" → `screen off` vs PC sleep;
  "click the search box" matching only "search").
- **Plan:** fix the seeds and pools in `gen_data.py` → regenerate the data → retrain. The 0.6B takes only 46 min,
  so iterate on it first, then retrain the 1.7B.

### Round 2 data changes (`training/gen_data.py`, 2026-10-30; not yet run)

| Round 1 cause | Round 2 fix |
|---|---|
| `tricky`/`escalate` pool sentences starting like commands ("search for…") were labelled `escalate` | Drop "not a command" sentences that start with a command word (search, open, close, play…) |
| Multi-step pairs taught "open youtube, then **web** search" | Open + search: YouTube → `youtube_search`; a browser or Google → `web_search`. New pair: open browser → open site |
| `press_keys` seeds had no required words, so "close this window" was labelled `alt+f4` | Each key sentence must name its key (`f4`, `control t`, `enter`…). Close-without-target seeds go from 30% to 50%, and they can't mention f4 |
| Typing seeds needed only the text, so chat-like sentences became `type_text` | Typing sentences must contain a typing verb (type, write, enter…) |
| "max volume" seeds were dropped (no number in the sentence) | Volume-100 seeds accept max / maximum / full / all the way |
| The teacher used "bring up X" for both `open` and `switch_to` | Word rules: `open` can't say "switch / to the front"; `switch_to` can't say "open / launch / start / bring up / use" |
| No "that's all thanks"-style endings | The cancel pool also asks for ways to end the conversation |
| Plain short forms were rare | **TEMPLATES**: ~1,800 code-written sentences ("search for X", "close this", "max volume", "i want to use X", "that's all thanks", short chit-chat → `escalate`) with random fillers and prefixes |

Seeds now support `["any", …]` and `["not", …]` word rules, which are both checked by the filter and told to the teacher.

**Caveat: the test set is now less independent.** These fixes were designed from its misses. Exact test sentences are
still removed from training, but close variants ("please close this") are in. Round-2 test scores will flatter the model
a little. **Record your own real commands as a fresh test set** (`bench\stt_bench.py --record` or the trace store) to
measure honestly.

**Running round 2 (cluster):** upload `gen_data.py`, delete the cached data, regenerate, then retrain the 0.6B first:
```
scp "C:\Users\shash\TritonBundle\Voice Assistant\training\gen_data.py" tinyx:/home/woody/iwem/iwem119h/triton_brain/code/training/
cd $WORK/triton_brain && mv data data_round1 && mv runs runs_round1 && mv out out_round1
cd code/training && jid=$(sbatch --parsable gen_data.slurm)
STUDENT=Qwen/Qwen3-0.6B LR=2e-5 NAME=Qwen3-0.6B-r2 sbatch --dependency=afterok:$jid sft.slurm
```

```
# laptop: upload the fixed file
scp "C:\Users\shash\TritonBundle\Voice Assistant\training\sft.py" tinyx:/home/woody/iwem/iwem119h/triton_brain/code/training/
# cluster: check the settings are accepted (should print "config OK"), then resubmit
cd $WORK/triton_brain/code/training && source ../../venv/bin/activate
python -c "from trl import SFTConfig; SFTConfig(output_dir='/tmp/x', warmup_steps=10, eval_strategy='steps', max_length=384, completion_only_loss=True, gradient_checkpointing=True, load_best_model_at_end=True, metric_for_best_model='eval_loss', save_strategy='steps', report_to='none', use_cpu=True); print('config OK')"
STUDENT=Qwen/Qwen3-1.7B sbatch sft.slurm
STUDENT=Qwen/Qwen3-0.6B LR=2e-5 sbatch sft.slurm
STUDENT=Qwen/Qwen3-4B-Instruct-2507 LORA=1 sbatch sft.slurm
# a few minutes later: loss lines, not a traceback
tail -20 $(ls -t sft_*.log | head -1)
```

**"Done" in squeue doesn't mean it worked.** Always check the log for the accuracy line and
`Finished`. `AssocGrpGRES` in squeue means your **group's** GPU quota is full. The job just waits;
nothing is wrong. If the A100s are busy, use `sft_v100.slurm` with the same arguments.

---

## 5. Where everything lives

| What | Where |
|---|---|
| Source code (all three Triton projects, moved off the Desktop/OneDrive on 2026-11-04) | `C:\Users\shash\TritonBundle\` = `Voice Assistant\`, `Triton Launcher\` (was nova-launcher), `Triton Screen Share\` (was lag-free remote display) |
| LLM models + llama.cpp (build b11963) | `C:\Users\shash\TritonModels\models\`, `...\TritonModels\llama.cpp\` (outside OneDrive: too big to sync) |
| Speech-to-text models | `C:\Users\shash\TritonModels\stt\` |
| Fine-tuned models from the cluster | `C:\Users\shash\TritonModels\trained\` |
| Piper voices, wake-word models | `Voice Assistant\models\piper\`, `Voice Assistant\models\*.onnx` |
| Your voice recordings | `Voice Assistant\voice_samples\<wake word>\{positive,negative,live}\` |
| Benchmark results | `Voice Assistant\bench\results\results.csv`, `stt_results.csv` |
| Cluster: brain training | `$WORK/triton_brain/` (`code/`, `venv/`, `venv-v100/`, `models/`, `data/`, `runs/`, `out/`) |
| Cluster: wake-word training | `$WORK/hey_nova/` (your recordings in `my_voice/`), code in `$WORK/cluster_training/` |
| Settings | `Voice Assistant\settings.json` (wake word, voice, ears, optional `wake_threshold`, `screen_share`) |
| Big brain certificate (laptop) | `C:\Users\shash\TritonModels\big_brain_cert.pem` + `big_brain_key.pem`; fingerprint in `big_brain.json` |
| N95 launcher bundle | `Triton Launcher\dist\TritonLauncher\` (+ `.zip`); the voice part is built in `Triton Launcher\voice-build\` |

---

## 5b. Running it on the N95

**Current brain:** fine-tuned **Qwen3-0.6B Q4_K_M** (86%, ~0.5 s on the laptop, 378 MB). On the laptop it's set in
`settings.json` → `"brain": {"model": "...\\TritonModels\\trained\\triton-Qwen3-0.6B-Q4_K_M.gguf"}`. Files named
`triton*` automatically get the one-line ("compact") prompt they were trained with (`brain/llm.py`).

**End-to-end test on the laptop (2026-10-30):** 8 noisy Piper-spoken commands went through Whisper, the 0.6B Q4 and Piper.
- **7/8 right.** Whisper took ~0.3 s and the brain ~0.4 s, so **~0.7 s from the end of speech to the decision**.
- It coped with mis-hearings: "Joe Open Apps" → `show_open_apps`, "Close this tub" → `close_tab`.
- The miss was the known round-1 gap: "close this window" → minimize.
- Everything loads in ~11 s.

**Full-bundle test (2026-10-30, run from the bundle folder):**
1. A Hey Triton test take woke it (0.98 against threshold 0.5, voice check on).
2. Whisper heard "Make the volume 90%" in 0.31 s.
3. The brain chose `set_volume 90` in 0.66 s.
4. Kokoro George said "Volume ninety percent".

**1. Laptop: build the bundle** (one self-contained folder, **~1 GB** with Kokoro, outside OneDrive):
```
.venv\Scripts\python.exe make_n95_bundle.py
```
It writes `Voice Assistant\dist\TritonVoice\` with:
- the code and the N95's own `settings.json` (wake word **hey_triton**, same voice and ears)
- `models\triton-mini.gguf` (the brain; `BRAIN.txt` says which one), `models\hey_triton.onnx` plus its verifier,
  the Vosk model, `models\stt\sherpa-onnx-whisper-base.en` (int8 only), the Piper voice
- `llama.cpp\` (llama-server)

Options: `--brain <gguf>` (e.g. a round-2 model), `--wake hey_echo`, `--out <folder>`. Re-running it copies only changed files.

**2. N95: double-click `Start Triton.bat`.** The N95 runs Windows 10 IoT Enterprise **LTSC 2021** (21H2, build 19044; supported
until 2032, with no Store or winget). Before the first run, install **Python 3.13 or 3.14** (python.org, 64-bit, tick
"Add python.exe to PATH") and the **VC++ runtime** (`https://aka.ms/vs/17/release/vc_redist.x64.exe`). Then copy the folder over and
double-click **`Start Triton.bat`**:
- **First time:** it runs `setup_n95.ps1 -Startup` (5–10 min, internet once). That creates the venv, installs the packages, copies
  openWakeWord's own model files from `openwakeword_models\` (its pip package doesn't include them: melspectrogram, embedding, VAD),
  checks every model, adds a login shortcut, starts Triton, and writes `.venv\triton_ready.txt`.
- **After that:** it just starts Triton. A second copy quits by itself (single-instance lock).
- Also allow apps to use the microphone: Settings → Privacy → Microphone → "Allow desktop apps…".
- **Don't copy a `.venv` from another PC** (its paths are absolute). Delete `.venv` to force a fresh setup.

The manual way, which does the same thing, in the folder:
```
powershell -ExecutionPolicy Bypass -File setup_n95.ps1 -Startup
```
It creates `.venv`, installs `requirements.txt` (`pywinauto==0.6.9` and `comtypes==1.4.17` are pinned), checks every model is
present, adds a login shortcut (`-Startup`), and starts the assistant. Look for the tray icon; the log is `assistant.log`.

**Swapping in a better brain later:** run `make_n95_bundle.py --brain <new Q4 gguf>`, then copy just
`models\triton-mini.gguf` to the N95 and restart the assistant.

**Launcher integration (2026-11-02): done.** The Triton Launcher bundle now carries this assistant as `voice\TritonVoice.exe`
(PyInstaller, no Python needed on the N95), built by `Triton Launcher\build-voice.ps1` from `make_n95_bundle.py`'s folder. See section 5f.

**RAM on the N95 (8 GB), estimated from laptop measurements:**

| Part | RAM |
|---|---|
| Brain: llama-server with the 0.6B Q4, 2048-token context | ~1–1.5 GB |
| Whisper-base | ~0.4 GB |
| Vosk + wake word + Piper + Python | ~0.6–0.8 GB |
| **Total** | **~2–2.7 GB**, so Windows and Chrome keep the rest |

---

## 5s. Triton-Mini v3 0.6B scored (2026-11-09)

Trained by the user on the cluster (`V3=1 gen_converse.slurm` → `DATA=data_v3 sft.slurm`, LR 2e-5), quantized with the same
calibration as v2 (`bench\quantize.py ...-v3-...-f16.gguf --types Q4_K_M Q8_0`; `--val val_v3.jsonl` fails, the calibration
code reads v1's `text`/`calls` rows). Files: `TritonModels\trained\triton-converse-v3-Qwen3-0.6B-{Q4_K_M,Q8_0}.gguf`.
Results: `TritonModels\bench_big\v3-0.6B-mini.{jsonl,log}`, `v3-0.6B-n95.log`. All with `_mini_doubtful`.

| Q4_K_M | 1000 items | real / v1 / v2 parts | model-decided | dangerous | N95 sentences (23) | model time, same moment |
|---|---|---|---|---|---|---|
| v2 0.6B | **87%** | 84 / 88 / 87% | **95%** | 0 | 14/23 | 0.57 s |
| v3 0.6B | 85% | 84 / 86 / 85% | 92% | 0 | **20/23** | 0.64 s |

- **N95 sentences:** v3 fixes everything it was built for: thanks → reply, "Closet" → ask again, "Clear the search bar" → the
  Clear button, "Do you want the browser" → ask again, small talk over a screen → reply, garbles ("Camilla-field", "Open M2")
  → ask again. Still wrong: "Nothing that's all" → ask again (want cancel), "Open YouTube and play real Madrid highlights" →
  open only, "In the search bar can you search Brooklyn 99" → type_text instead of web_search. Caveat: v3's code-written
  groups were designed from these exact failures (the sentences themselves were kept out of the data).
- **1000 items:** v3 lost 30, won 15. Wins are mostly chat/junk (say 6, repeat 5). Losses are 28 commands: knowledge
  questions now answered by voice instead of web_search (≈7, partly a labelling choice), "click on the first video" →
  target "first", "new tap"/"mute the toy" → ask again, "nothing, sorry" → reply not cancel, a few two-part commands and
  arg slips. So v3 is more cautious: fewer wrong actions on noise, a few more missed real commands.
- **Speed:** the first big run said p50 0.50 s vs 0.40 s, but repeat answers were slower too (laptop busier). Back to back on
  the N95 set: v2 0.57 s, v3 0.64 s (v3 gives more spoken replies, which are longer).

---

## 5r. N95 log, a lighter check for v2, and Triton-Mini v3 (2026-11-06)

**N95 log** (copy it to `TritonModels\trained\assistant.log`; `Voice Assistant\assistant.log` is the laptop's own).
v2 0.6B ran there from 11:06: 0.9-2.4 s per decision (first one ~6 s, prompt not cached yet). Its mistakes, now
`bench/eval/n95.jsonl` (23 items, kept out of training): "thanks buddy that was quick" -> play, "Closet" -> close,
"clear the search bar" (screen shows "Clear search query") -> close tab, "play the first one" on results -> click
"YouTube", "do you want the browser" -> new tab, small talk over a screen -> ask again, a reply claiming an action it
didn't do ("I'll close the call now"). Not the model: "real Madrid highlights on the search bar" searched with those
words (clauses), `media next` crashes in `actions._sessions`, and the N95 launcher's friend plugin answers off-topic.

**The check was hiding v2's real score.** `_doubtful()` was written for v1 (which had to pick a tool for everything);
on 1000 items it threw away 72 right v2 answers to stop 5 wrong ones ("open the first video" -> the first result: no
word overlap; "and search X" looked like a dropped second part). Replaying v2's answers against each check: only
`power` and `set_volume` stopped more than they broke (the app-list check alone broke 38). Now, for Triton-Mini
(`llm.mini`), `_mini_doubtful()` checks only power / lock / set_volume / cancel, plus closing the front window or tab
and opening a tab need words for it ("Closet" -> close, "do you want the browser" -> new tab). v1 keeps `_doubtful`.
`real_bench.py --no-doubt` skips the check (experiment).

| v2 0.6B, laptop, 4 threads | 1000 items | real | v1 | v2 | Decided by model | Dangerous | N95 test (23) |
|---|---|---|---|---|---|---|---|
| old `_doubtful` | 78% | 88% | 85% | 75% | 80% | 0 | |
| no check | 87% | 82% | 87% | 87% | 95% | 2 (power) | 52%, 4 dangerous |
| **`_mini_doubtful`** | **87%** | 84% | 88% | 87% | **95%** | **0** | **61%, 0 dangerous** |

Model time p50 0.40 s, p90 0.64 s. The N95 launcher (separate build) needs this change too to get the gain.

**Triton-Mini v3 data** (`training/gen_v3.py`, `gen_converse.py --v3` -> `data_v3/`): v2's data (reusing its cached
teacher work) plus ~13k code-written rows of near-identical sentences with different answers: close it / close this
window / "close that thing" (ask which) / "closet" (ask again); thanks after a command (talk, never act) vs "thanks, now
pause it"; "play the first one" on a page of results -> that result; "clear the search bar" -> the Clear button on
screen; people talking ("do you want the browser") and word salad -> ask again; polite and long commands; two commands
joined by and/then. Small talk also with a screen line; chat replies claiming an action are dropped; every round-2
command is labelled again by the teacher and dropped where the tools disagree (drop rate printed in the log).
Dry run with a fake teacher: all groups built, no fake-act replies, no N95 test sentence in the data.
`sft.slurm` names it `triton-converse-v3-<student>` (v2 files stay), val as `out/val_v3.jsonl`.

Run (laptop, then cluster):

```
cd "C:\Users\shash\TritonBundle\Voice Assistant"
scp -r tools brain bench training iwem119h@tinyx:/home/woody/iwem/iwem119h/triton_brain/code/
```
```
cd $WORK/triton_brain/code/training
jid=$(V3=1 sbatch --parsable gen_converse.slurm)
DATA=data_v3 STUDENT=Qwen/Qwen3-0.6B LR=2e-5 sbatch --dependency=afterok:$jid sft.slurm
```

Then copy `out/triton-converse-v3-Qwen3-0.6B-f16.gguf` and `out/val_v3.jsonl` back, quantize
(`bench/quantize.py`), and compare with v2 on `big.jsonl` and `n95.jsonl`.

## 5q. The 1000-item test (2026-11-06)

`bench/build_big_eval.py` writes `bench/eval/big.jsonl` (1000 items, each tagged `part`): real 49 + v1 eval 229 +
722 from `val_v2.jsonl` (all chat / TV chatter / garble / screen / follow-up / knowledge items, plus sampled
commands). Forms: 738 act, 163 say, 99 repeat. The v2 part is in the training style, so it favours the fine-tunes.
Run: `real_bench.py --eval bench\eval\big.jsonl --threads 4 --quiet --out x.jsonl` (prints per-part scores and
"decided by the model" = items the rules didn't place). Results in `TritonModels\bench_big\`.

| Model (Q4_K_M, 4 threads) | All | real | v1 | v2 | Decided by model | Dangerous | Model p50 / p90 |
|---|---|---|---|---|---|---|---|
| **Qwen3-0.6B v2 converse** | **78%** | 88% | 85% | 75% | **80%** | 0 | **0.52 / 0.86 s** |
| Qwen3-0.6B v1 | 69% | 84% | 81% | 64% | 65% | 0 | 0.52 / 0.86 s |
| Qwen3-1.7B v2 converse | 78% | 84% | 85% | 75% | 80% | 0 | 1.13 / 1.91 s |
| Qwen3.5-2B untrained | 59% | 88% | 76% | 51% | 47% | 2 | 2.14 / 4.07 s |
| LFM3-1.7B untrained | 51% | 78% | 70% | 43% | 34% | 2 | 6.69 / 9.28 s |

- v2 0.6B = v2 1.7B on 1000 items, at half the time: the 0.6B stays the pick. v2 beats v1 mostly on
  "ask again" (garble/TV: 93% vs 49%) and talk-back.
- Qwen3.5-2B's 88% on real was luck of the small set: on the rest it trails v1. It and LFM3 both fired `close`
  on "close that thing" (should ask which). Untrained candidates are not worth it.
- Weakest spot for every model: v2 commands (67% for the fine-tunes; exact-call scoring is strict).

## 5p. Newer base models: LFM3-1.7B and Qwen3.5-2B, untrained (2026-11-06)

Downloaded to `TritonModels\candidates` (LiquidAI/LFM3-1.7B-GGUF and unsloth/Qwen3.5-2B-GGUF, Q4_K_M). LFM3 needs
llama.cpp b11960+, so our b11963 runs it. Untrained models get the laptop's full converse prompt: new
`"prompt": "converse-full"` in `brain/llm.py` (the mini act / say / repeat decision with `converse_prompt()`), and
`"checkpoints": true` (hybrid models re-read the prompt otherwise). `real_bench.py` has `--prompt` and `--checkpoints`.

Real eval (49), laptop, 4 threads; tokens/s from `llama-bench -t 4 -p 512 -n 32`:

| Model (Q4_K_M) | Trained? | Right | Model p50 | Read prompt tok/s | Write tok/s |
|---|---|---|---|---|---|
| Qwen3-0.6B v2 converse | yes | 43/49 (88%) | 0.37 s | 205 | 41 |
| Qwen3-1.7B | no | 42/49 (86%) | 2.61 s | 75 | 18 |
| LFM3-1.7B | no | 38/49 (78%) | 7.61 s | 81 | 18 |
| Qwen3.5-2B | no | 43/49 (88%) | 2.01 s | 92 | 12 |

- Qwen3.5-2B untrained ties the trained 0.6B, but writes 3.5x slower (12 vs 41 tok/s). Even trained with the
  one-line prompt, a ~15-token reply alone is ~1.3 s on the laptop, ~3-4 s on the N95.
- LFM3 is as fast as Qwen3-1.7B per token, but its 7.6 s means the prompt cache (checkpoints) doesn't work for it in
  b11963; it says instead of repeating on garble and missed "go back". Not worth training on today's numbers.
- 49 items = about +-9 points; a ~1000-item test (v2 held-out val, the 229-item eval, real) is next, once
  `val_v2.jsonl` is copied from the cluster (`$WORK/triton_brain/out/`).

## 5o. Triton-Mini v2 trained and scored (2026-11-05, night)

The cluster jobs (gen_converse + sft on `data_v2`) produced `triton-converse-Qwen3-0.6B-f16.gguf` and
`-1.7B-f16.gguf` in `TritonModels\trained`. `bench\quantize.py` now calibrates "converse" models on the chat
template with COMPACT_CONVERSE (what `LocalLLM.decide` sends) and caps the importance matrix at `--chunks 100`
(282 chunks took ~70 min for 1.7B). Q4_K_M and Q8_0 made for both.

`bench\real_bench.py --model <gguf> --threads 4` (49 real commands, laptop, 4 threads like the N95; new line
"model asked Nx" times only the model, since rules answer in ~0 s):

| Model (Q4_K_M) | Right | Dangerous | Model p50 / p90 | Size |
|---|---|---|---|---|
| v1 0.6B (current N95) | 41/49 (84%) | 0 | 0.37 / 0.47 s | 378 MB |
| **v2 0.6B converse** | **43/49 (88%)** | 0 | 0.37 / 0.44 s | 378 MB |
| v2 1.7B converse | 41/49 (84%) | 0 | 0.88 / 1.06 s | 1056 MB |

- v2 0.6B fixes TV chatter ("Hydrated", "She wanted to put anything on" -> repeat) and the "and germany?"
  follow-up (-> say); new miss "Bring clumps forward" -> maximize Chrome.
- v2 1.7B chats ("hey how are you doing" -> say) but acts on garble: "Mr Meera my laptop" -> lock, "No could the
  Triton assistant running here" -> close, "Stop filming" -> cancel. 2.4x slower, 2.8x bigger. Not recommended.
- Expect ~2-3x the laptop times on the N95. Not bundled here: the user installs the chosen GGUF on the N95's own
  launcher (copied as `voice\models\triton-mini.gguf`, settings "brain" needs `"prompt": "converse-mini"`, since
  the prompt is otherwise picked by "converse" in the file name).

## 5n. Streaming ears, rule fixes, Triton-Mini v2 pipeline (2026-11-05, later)

- **Ears:** `settings.json` "ears" = `moonshine-medium-streaming` (transcriber.py `StreamingEars`; moonshine-voice
  0.1.5). Audio is fed while you talk (worker thread) and the text is ready ~0.01–0.25 s after you stop (live-speed
  test). whisper-small is now only the **second listen**: when nothing in a command was understood ("repeat"), the
  same recording is heard again by whisper-small and tried once more (`_second_listen`). The model lives in
  `TritonModels\stt\moonshine-medium-streaming` (257 MB; copied into the bundle). If the N95 can't keep up live,
  the log says "Streaming ears fell behind": switch to `moonshine-small-streaming`.
- **Rule fixes** (from the real-command misses): "open X on Chrome" = open X; "bring up / pull up / fire up";
  "play X on YouTube Music / Spotify" = open it + search; typed text without "here on the search bar"; "shut up",
  "that's all Triton" end it; shutdown only when the computer is meant ("stop the assistant" no longer shuts down);
  a window/close target that isn't an app is doubted. Real commands: **67% -> 84% (41/49), 0 dangerous**.
- **Triton-Mini v2 ("converse-mini")**, ready to train: `training/gen_converse.py` + `gen_converse.slurm` build
  act / say / repeat data with screen lines, follow-ups, TV chatter, small talk (teacher-written) and your real
  sentences (`training/real_seeds.jsonl`, never the test set). Run time: `LocalLLM.decide()` (brain/llm.py),
  `COMPACT_CONVERSE` + `mini_schema()` (brain/prompt.py); used automatically when the model file name has
  "converse" in it (the bundle sets "prompt": "converse-mini"). Real knowledge questions still go to the laptop.
  On the cluster (after uploading the code, training/README.md step 1):
  ```
  jid=$(sbatch --parsable gen_converse.slurm)
  DATA=data_v2 STUDENT=Qwen/Qwen3-0.6B LR=2e-5 sbatch --dependency=afterok:$jid sft.slurm
  DATA=data_v2 STUDENT=Qwen/Qwen3-1.7B         sbatch --dependency=afterok:$jid sft.slurm
  ```
  Then on the laptop: quantize (`bench/quantize.py`) and score with `bench/real_bench.py --model <gguf>`; the one
  that beats 84% on the real commands (and is fast enough) goes into the bundle.

## 5m. Smarter and faster N95: first measurements (2026-11-05)

- **Where the time goes (laptop log):** you stop talking -> 0.8–1.8 s silence wait -> whisper-small **2.0 s p50 / 3.7 s p90**
  (58 real clips; ~2x that on the N95) -> small model **0.44 s p50** -> action. The ears are the slow part, not the model.
- **Moonshine streaming** (`moonshine-voice` 0.1.5, transcribes *while* you talk; tested in a scratch venv, not installed
  here): text ready **~0.03 s p50, 0.4–1.0 s p90** after you stop. Compute on the laptop: tiny 0.35x, small 0.58x,
  medium 0.62x real time, so on the N95 medium/small may not keep up live (needs an N95 test). Accuracy on the
  real clips: medium is roughly a tie with whisper-small (better on "Brooklyn Nine-Nine", "Shashank", worse on
  "open crop", "Vishnu Meerar"); small/tiny worse. Key-term biasing barely helped and once looped "Shashank Shashank…".
- **Real-use test set** `bench/eval/real.jsonl` (49 things you actually said, from conversation.log: commands,
  mishearings, TV chatter, chat) + `bench/real_bench.py` (runs the real `_understand()` without the laptop).
  Today's N95 path (rules + clauses + fine-tuned 0.6B): **33/49 (67%), 1 dangerous** ("stop the assistant" ->
  shutdown, which still asks first), vs 86% on the synthetic eval. Misses: app names misheard ("crome", "you took"),
  multi-step dropped ("bring up spotify and skip"), "that's all Triton" -> close Triton, chat it can't answer.

## 5l. Round 2 (ears compared), round 3 (echo cancellation), live fixes (2026-11-04, later)

**Ears compared on 55 real clips** (12 from your live session today), counting clips whose command came out unusable:

| Ear | Unusable | Time per clip | Verdict |
|---|---|---|---|
| **whisper-small** | **4** | 1.35 s | **Now the only ear** |
| whisper-base | more ("Shisha", "open crawl"…) | 0.49 s | Was the first pass, but whisper-small re-heard nearly every live sentence anyway |
| Parakeet TDT 0.6B v2 int8 | 8 ("Popper grow" = Open Chrome, "I'm gonna go to" = my laptop here) | 0.69 s | Better on names/numbers, worse on short commands. Not used |
| Parakeet + hotwords (score 1.5 / 3) | many: invents hotwords in noise ("Spotify that's all…", "mirror mirror mirror") | 0.74 s | Not used. Its `bpe.vocab` is home-made from `tokens.txt` (none shipped) |
| + GTCRN denoiser | blanked "That's all", garbled others | +0.6 s | Not used |

Models stay in `TritonModels\stt\sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8` (~660 MB) and `TritonModels\enhance\gtcrn_simple.onnx`
(delete if space matters). Settings: `"ears": {"model": "whisper-small", "second_opinion": ""}`; `make_n95_bundle.py` does the same
(expect ~2.7 s per command on the N95).

**Echo cancellation (`echo.py`)**: WebRTC AEC3 from the `livekit` package; the reference is what the default speaker plays
(WASAPI loopback via `soundcard`; follows speaker changes every 10 s). Every mic frame goes through it before the wake word and
the ears. Settings `"echo_cancel": true` (default). Live test on the laptop (Kokoro speech on the speakers as "the TV"):

| Mic audio | Whisper heard |
|---|---|
| TV only, raw | "opening YouTube and searching for low-fiving" |
| TV only, echo cancelled | nothing (15 dB removed) |
| TV + your real command clip, raw | "increased the volume to 90%" |
| TV + your command, echo cancelled | "increase the volume to 90 percent" |

The stricter wake word and no follow-ups while media plays are kept for now; loosen them once it's proven live on the TV.
New packages (`requirements.txt`, and `build-voice.ps1` collects them): `livekit`, `soundcard`.

**Fixes from the live session:**
- **Crash:** the assistant died at 11:35:54 (pythonw heap corruption 0xc0000374) one second after "Laptop too slow… dropped".
  `RemoteBrain.cancel()` closed the TLS connection from another thread while it was being read. Now it only sets a flag;
  the reading thread stops at the server's next 1-second `{"wait"}` line and hangs up itself (tested 3x: 0.2-1.05 s).
- **Screen line capped** at 12 names of <= 30 characters: 36 names made the 4B take 14.9 s instead of 10.0 s.
- "search for Netflix in the current opened Chrome" / "…in the current core tab" -> opens Netflix (where-words dropped,
  a bare site name opens the site).
- "select the first profile", "the second one", "the last video" -> clicks by position among the on-screen items
  (menus/headers skipped). Live: the first profile is `shashank732001`.

- **"Couldn't say …" (PortAudioError -9988, also seen 2026-11-03):** `find_mic()` restarts PortAudio on every mic re-pick (every 30 s
  on a backup mic), which killed the speaker's kept-open output. Now the assistant closes it first (`Speaker.release_output()`,
  after any reply in progress) and a write to a dead stream reopens it once. Tested: speech works after a PortAudio restart.

**Bundle rebuilt 2026-11-04** (`Triton Launcher\build.ps1 -RebuildVoice`): `dist\TritonLauncher` (1.6 GB) + `.zip`, voice exe 1.5 GB
(whisper-small, echo cancellation, all of 5k/5l; big brain https://192.168.0.204:8765, pinned). Smoke-tested on the laptop:
ready in 13 s, echo reference found, typed "what time is it" / volume / chat (laptop over Wi-Fi, 11.9 s) all fine, speech fine
after a mic re-check.

**Still open:** the 4B big brain takes 5-12 s per reply on this laptop when busy (the 12 s limit catches the rest); not yet
installed on the N95 or tried with the TV.

## 5k. Round 1 of the ears and conversation fixes (2026-11-04)

**What was wrong (live log of 2026-11-03, 42 real clips replayed):** the ears mostly heard right, but
1. **commands were cut at 6 s**: "Open Netflix and select Shashank" was still going, so the name became "session";
2. **clear sentences still failed**: the rules gave up on two-part sentences, the 0.6B re-typed words ("Brooklyn99" ->
   "bronley99") and picked YouTube instead of Netflix's search, then the laptop timed out: 12-40 s, then "say that again";
3. **one laptop doing everything**: with the big brain busy, whisper-small took 6.7 s (1.6 s idle) and the 0.6B 15 s (0.5 s).
   Abandoned laptop requests kept running and slowed the next one.

**Changes:**

| Fix | Where | What it does |
|---|---|---|
| End of speech | `endpoint.py` | Silero VAD (openWakeWord's copy, ~2 ms/frame) on the raw and an 8x-boosted copy; "still talking" also = level 2.5x above the room, or Vosk producing new words. Ends 0.8 s after you stop (1.3 s after a short burst, 1.8 s after "open / to / select…" or just the wake phrase); you get 6 s to start, 12 s max. No speech = nothing sent to Whisper (no made-up "Thank you."). Whisper gets the speech plus 1 s before. |
| What's on screen | `screen.py` | Window title + visible clickable names (~30 ms), taken as listening starts (audio queues meanwhile) |
| Part by part | `brain/clauses.py` | Splits at "and / then + verb"; each part via the rules, then patterns: search (on the site in front, or open the named site first), select/click (name matched to the screen: "Shashant" -> "Shashank"), type, scroll, press key. Words are copied as heard, "9/9" -> "99". |
| Copy, don't re-type | `clauses.fix_calls` | Model and laptop answers get the search/typing words from the sentence, click names spelled as on screen, and YouTube only if said or in front |
| Specific questions | `assistant._which_one`, "partial" | A click that finds nothing: "I can't see “X”. I see Shashank, Kids… Which one?" and a bare name answers it. Half understood: does that half, then "I didn't get “…”. Say that part again?" |
| Small model skipped | `_doubtful` / `_understand` | When part of the sentence is unclear, or a two-part sentence got one action |
| Laptop limit + cancel | `_converse_with_laptop`, `remote.cancel`, server heartbeat | 12 s (`laptop_deadline_sec`) to decide or start talking, then "that's taking me too long…". Hanging up stops the laptop's model within ~1 s (a `{"wait"}` line every second while it writes) |
| Laptop priority | `brain/llm.py` | The big brain's llama-server runs below normal priority, so the ears and small model win the cores |
| Laptop prompt | `brain/prompt.py` | Explains the `[screen: … \| on screen: …]` line; "here" = that window; copy words exactly |
| Click waits | `tools/handlers.do_click`, `uia.best_match` | Up to 8 s after opening a site (Netflix profiles load slowly); misheard names matched letters-only |

**Measured:**
- Replay of the 42 real clips: no command cut short any more ("…select Sheshan" keeps its name), 3 silent clips rejected
  before Whisper, same or better text on the rest. Not replayable: "Hey Triton… [long pause] command" (needs live Vosk text).
- Decision path (real rules, fine-tuned 0.6B, fake laptop): every failure from 2026-11-03 is now placed instantly and right:
  open + click Shashank, `web_search "Brooklyn 99"` on Netflix, "select Shashant" -> Shashank. A laptop that doesn't answer -> plain
  reply after the limit instead of 40 s.
- Real laptop 4B with screen lines: "pick the shashant one" -> click Shashank, "find me brooklyn nine nine in here" -> search on
  Netflix, "Mr Meera my laptop" -> mirror, chat answered; 5-7 s each. Cancel frees it in 0.1 s; the next request 6.7 s (was 13 s).
- **Not yet tried live by voice** on the laptop or the N95; the N95 and launcher bundles are not rebuilt yet.

**Restart needed:** the big brain (`Start Big Brain.bat` / tray Restart) for the new prompt and priority; the assistant for the rest.

## 5j. Screen control fixed, voice before action, female voice, test console (2026-11-03)

**Screen control (pywinauto), tested on a test page and real YouTube:** typing and scrolling worked all along
when the right window was in front. Clicking on web pages did not: a freshly started Chrome shows UI Automation
only its own toolbar for the first seconds, so "click shashank" could miss the Netflix profile, or hit Chrome's
own profile button, also named "shashank".
- `tools/uia.py`: `find()` searches the web page (its Document) first and waits up to 3 s for it, then the
  browser/app. `actions.open_in_chrome` starts Chrome with `--force-renderer-accessibility`.
- **Search inside the site:** `web_search` uses the search box of the page or app in front (YouTube: 0.8 s; Netflix's
  Search button then its box), else Google. Right after "open netflix" it waits (up to 8 s) for Netflix to be in front.
- `type_text` first focuses a text box if none has the keyboard. Scroll top/bottom = Home/End.
- N95 model check: a YouTube search you didn't ask for ("open netflix and search for X") goes to the laptop.

**Voice:** default `en_US-hfc_female-medium` at 1.15× (the quickest Piper voice to make; samples of 6 female voices
in `voice_samples/female_voices`). `speaker.py` keeps the sound output open and keeps short phrases once made:
a repeated "Opening Netflix" is audible in ~50 ms (was ~300 ms + device opening). `_do` waits (≤1.5 s) until the
preview is audible before acting, so the words come with the action. No more "Yes sir"; the laptop prompt says
"talk like a relaxed friend, never sir, no 'How can I help you today?'".

`make_n95_bundle.py` now defaults to this voice at 1.15× and also packs `Test Console.bat`. Bundle rebuilt
2026-11-03 (1145 MB, big brain https://192.168.0.204:8765, fallback mode).
Launcher build (`Triton Launcher\build-voice.ps1` → `voice-build\TritonVoice`, 1.4 GB, copy it as the launcher's
`voice\` folder): `TritonVoice.exe --console` opens the test console (`Test Console.bat` next to the exe), and
`live.jsonl` sits next to the exe. Smoke-tested on the laptop: ready in 18 s, typed question answered by the big brain.

**Startup:** the bubble says "Triton is starting…" until the voice and the N95 model are loaded, then
"Ready · say Hey Triton" and it says "Triton's ready." Until then the N95 model is skipped (no 20 s wait).

**YouTube waking it / hearing videos:** while Windows' media controls report something playing
(`actions.something_playing`, checked every 2 s) the wake word needs score 0.75 for 3 frames
(settings `media_wake_threshold`) and there's no follow-up listening. Whisper's "[BLANK…" counts as silence.

**Saved audio:** capped by size (settings `command_clips_mb` 40, `wake_clips_mb` 40), oldest deleted first.

**Test console** (`test_console.py`, tray > Test console, or `Test Console.bat`): every step live from `live.jsonl`
(`events.py`): heard → who decided (rules / N95 model / laptop / ask again) → each action ✓/✗ → what was said.
Type a command to run it through the same pipeline without the ears (127.0.0.1:8767 only).

**Laptop big brain, 9B vs 4B (measured today):** with context checkpoints (`-ctxcp 32 -cms 256`, now passed by
`brain_server.py`), Qwen3.5-9B no longer re-reads its 1,165-token prompt (only ~15 new tokens), but it still takes
7–19 s per reply (writing is ~2.5 tokens/s on this CPU), once timed out at 25 s, and answered "scroll down a bit"
with words instead of scrolling. Back to **Qwen3-4B-Instruct-2507** (~3–5 s).

**Web search (laptop big brain):** the converse reply has a 4th form, `{"search": "short query"}`, for news,
results, weather, prices, "latest"/"today" questions (CONVERSE_RULES + 2 examples). `brain_server.searched()` then
searches DuckDuckGo (`brain/websearch.py`, `pip install ddgs` in the laptop's .venv, no key; 3 results × 160 chars),
and asks the model again with the results and today's date, allowing only `{"say"}` (`prompt.say_schema()`), told to
trust the results over its memory. Streamed: "Let me look that up." at ~4 s, the answer at ~18–25 s (reading the
results is the slow part on this CPU); `{"wait": 1}` lines every 5 s keep the N95's 25 s read timeout from firing
(the N95 ignores unknown lines, so no N95 change was needed). Only the search query leaves the laptop. Tested:
"who won the 2026 world cup" → Spain 1-0 Argentina; "weather in Lisbon today"; "latest iPhone"; questions it knows
("capital of France") and commands are answered without searching. Also fixed: streamed replies were decoded as
Latin-1 ("—" came out as "â€”"); `converse_stream` now reads UTF-8.

---

## 5i. Ears from the N95's real recordings, one-breath commands, streamed answers (2026-11-02)

**What 30 real N95 commands showed** (`n95 commands\commands\`, saved by the N95: audio plus what Whisper heard):
- **Quiet audio:** the Rapoo mic across the room peaks around 3,000 of 32,767. whisper-base garbled it: "stops are in my laptop
  here", "open crawl", "Mr Meera".
- **Cut off too early:** listening ended at the first pause ("open", "I", "and", "If I can increase the volume to").
- **"Hey Triton" inside the command:** "hair triter go home", "Hydrated", "Hit right hand…".
- **TV speech** after a wake-up ("We're like he might actually be going super saiyan…").

**Ears compared on those 30 clips** (laptop; the N95 is ~2x slower):

| Model | Per clip | Notes |
|---|---|---|
| whisper-base (as before) | 0.6 s | |
| whisper-base, louder | 0.7 s | small gains |
| **whisper-small, louder** | 1.4 s | **got 6 more right**: "stop showing my laptop here", "go home", "Open Chrome", "Search for YouTube here", "show my laptop here", "which to mirror" |
| moonshine-base | 0.23 s | worse (empty or wrong on half) |

**Changes:**
- `transcriber.louder()`: every clip is raised to ~8,000 peak (at most 20x) before Whisper. `tidy()` drops "(buzzer)"/"[music]".
- **Second opinion:** whisper-base hears every command. If that isn't a clear command (rules, screen sharing or a pause request can't
  place it; `_clear()`), **whisper-small re-hears the same audio** before the model or laptop sees it. Clear commands stay fast;
  unclear ones cost ~1 s more here (~2 s on the N95). Settings: `"ears": {"second_opinion": "whisper-small"}` (`""` = off).
  The bundle now carries whisper-small int8 (+358 MB).
- **Unfinished sentences:** when Vosk hears a pause right after "open / to / the / search / play / set / my…" (`DANGLING`), it
  listens up to 1.5 s more.
- **`strip_wake()`** removes "Hey Triton" and its mishearings ("hair triter", "hey tritan", "triton, …") from the start. If that
  was all there was, Triton answers and listens again.
- **One breath (`"wake_style": "chime"`, default):** a short chime instead of saying "Yes?", and the audio since the wake word is
  kept, so "Hey Triton, open YouTube" works in one go (~1 s faster). `"wake_style": "voice"` brings back "Yes?".
- **The N95's 0.6B must find its action in the sentence** (`_supported`): a volume needs its number, the time needs "time",
  cancel needs a "never mind"-type word, searches and apps need their words. Otherwise the laptop decides (it can ask again).
  Before, "minute" → said the time, and "huh" → cancel.
- Filler noises ("huh", "hmm", "uh") count as nothing heard. "That's all / never mind / forget it / nothing" end the
  conversation on the N95 at once (`ENOUGH`). Bare "stop" still pauses media.

**Replay of the 30 real clips through the real code** (listening, both Whispers, decisions, the N95's 0.6B, the laptop):

| | Before | Now |
|---|---|---|
| Right action | a handful | **15**: extend, mirror ×2, show, stop ×3, go home ×2, open Chrome, open YouTube, YouTube search, volume 90, "that's all" ends |
| Wrong action | many ("open meera", "go home" for "Get ready", web search for "search search…") | **1**: volume 9, from a clip already cut off before "ninety" (the unfinished-sentence wait addresses this live) |
| Asked again or harmless reply | | the rest. "my laptop here" → "Do you want to mirror your laptop screen to the TV?" |

**Streamed answers:** `/converse` with `"stream": true` sends each finished sentence as it's written (HTTP/1.1 chunked,
`{"say_part": …}` lines, then `{"final": …}`; `llm.converse_stream`, `partial_say`). The N95 starts speaking at the first one
and drops the filler. The prompt asks for a very short first sentence.

| Asked | Speech starts | Whole answer |
|---|---|---|
| "tell me a joke" | 2.9 s | 4.0 s |
| "why is the sky blue" | 3.3 s | 5.8 s |
| "how does a rainbow form" | 4.4 s | 7.8 s |

- The first try used HTTP/1.0 without a length. The client saw nothing until the connection closed, then failed on the TLS close and
  asked again (double time). Chunked HTTP/1.1 fixed it, and the N95 now also reuses its TLS connection.
- If a stream breaks after some sentences were said, they're kept rather than asked for again.

**Speculative decoding (Qwen3-0.6B drafting for the 4B): slower here, 4.5 s vs 4.1 s mean.** On a 4-core CPU the draft model takes
the same cores and memory bandwidth. Not used; `LocalLLM(draft=…)` stays for a GPU. This llama.cpp build's flags are
`--spec-draft-n-max/-n-min` (the old `--draft-max` is gone).

## 5h. Pause by voice, cut it off, quiet while screen sharing (2026-11-02)

- **Pause by voice:** "stop listening for 10 minutes", "be quiet for an hour", "take a break for half an hour", "don't listen for 30 seconds",
  "stop listening" (no time = until asked).
  - `snooze_request()` parses the time; it's checked first in `_understand`.
  - While paused, the wake word still runs, but only "Hey Triton… start listening / resume listening / wake up / I'm back" is
    answered. Everything else is ignored silently, with no "Yes?".
  - It comes back by itself when the time is up (a chime), or with the tray's **Resume**.
  - The tray's own Pause is different: it closes the mic completely.
- **Cut it off:** "Hey Triton" while it's waiting for the laptop or while it's talking stops it right there (`_watch_for_wake`: the
  mic keeps running the wake word with the voice check; a late laptop reply is dropped). Then it listens for your next sentence:
  "Hey Triton… never mind" cancels. A bare "stop" can't work while it talks, because it would hear its own voice.
- **While screen sharing** (`_watch_sharing` asks Triton Screen Share's pipe every 3 s):
  - the wake word needs ≥ 0.7 (`"sharing_wake_threshold"`; your takes score 0.84+)
  - no follow-up listening
  - no near-miss clips saved (they'd teach the wake word the laptop's audio)
  - `"sharing_listen": "off"` makes it not listen at all until sharing stops (then stop sharing from the bubble)
- **Tested** with recordings fed into the real assistant's audio queue (N95 settings):
  - 10/10 of your untouched test takes cut it off; 0/10 background and "don't fire" recordings did
  - "stop listening for 2 seconds" → paused, said "Okay, I'll stop listening for 2 seconds", back after 2 s
  - an open-ended pause ended with tray Resume
  - **Not yet tried on the N95 by voice.**

## 5g. Ask instead of guessing, conversation, fast big brain, tray (2026-11-02, later)

**What went wrong on the N95 (from the laptop's log):** the N95 sent the laptop misheard text like "Mr Meera" (= "mirror"),
"Get ready", "Hydrated" and "search search search…" (Whisper making things up on background sound). The laptop had to answer with
a tool call, so it acted: "Mr Meera" → open an app called "meera", "Get ready" → go home. Each trip took 6–9 s on top of
the N95's own try.

**Now (`assistant._understand` returns act / say / repeat):**
1. **Nonsense is caught on the N95** (`garbled()`): nothing heard, Whisper's noise fillers ("Thank you.", "you", "[music]"), or a word
   repeated 3 times in a row. Triton answers "Sorry, could you say that again?" (varied) and keeps listening without the wake word.
2. Screen-share phrases and the sure rules act instantly; the N95's 0.6B acts when its answer passes `_doubtful`.
3. Otherwise, **one** request to the laptop: `POST /converse` → `{"calls": [...]}` (do it), `{"say": "..."}` (talk) or
   `{"repeat": true}` (misheard). The grammar allows exactly these three (`brain/prompt.py` `converse_schema`), and the prompt
   (`CONVERSE_RULES`) says asking again always beats doing the wrong thing. It also says to fix a mishearing only when sure
   ("mr meera my laptop" → mirror), keep spoken answers under 30 words, and use the conversation so far.
4. If the laptop takes over 2.5 s, the N95 says a filler ("Hmm.", "One sec.").
5. Nothing sure and no laptop → ask again. **The old fallback to the rules' unsure guess is gone.**

**The laptop model is now Qwen3-4B-Instruct-2507 (Q4).** The same 18 sentences through `/converse`:

| Model | Mean | "Say that again" | Notes |
|---|---|---|---|
| Qwen3.5-9B | ~9 s | 4–5 s | Hybrid architecture: llama.cpp can't reuse its cached prompt, so it re-reads the whole tool list every request |
| **Qwen3-4B** | **3.9 s** | **1.4 s** | All 18 right after adding three rules + examples (made-up time → `info`, "close this window" target, one `youtube_search`) |

The tray's **Model** menu switches between them (saved in `big_brain.json`).

**Routing test** (`assistant._understand` with the real rules, fine-tuned 0.6B and the laptop's 4B; times on the laptop, the N95's
own model is ~2–3x slower):
- rules: "volume up", "open youtube", "close this window", "minimize this": 0.0 s
- the 0.6B: "i want to watch netflix", "what time is it", "scroll down", "click play", "type hello world": 0.4 s
- caught on the N95: "search search search search", "Thank you.": 0.0 s
- the laptop:
  - "Mr Meera", "Get ready", "Hydrated" → asks again, ~2.2 s
  - "open netflix and play" → open + play, 7 s
  - "play lofi music on youtube" → `youtube_search`, 5 s
  - "why is the sky blue", "hey how are you", "tell me a joke" → spoken answers, 4–6 s

**Big brain tray icon** (`brain_server.py` `run_tray`; green = ready, amber = loading, grey = stopped):
- Clear old conversation (keep today): removes earlier days from `conversation.log` (2026-11-04). The conversation window has
  the same plus "Clear all" as buttons (each asks first), and shows a line per day ("Today · Wed 4 Nov 2026") above its messages.
- Show conversation: `brain_viewer.py`, a live chat-style window of `conversation.log`. It shows what the N95 heard, what it did
  by itself (the N95 reports it with `POST /note`), and what the laptop answered, with timings.
- Open full log, Start / Stop (stop frees the model's memory; the N95 then works alone), Restart, Model, Quit.
- `--no-tray` runs it without the icon. The model now uses 1 llama-server slot, so every request reuses the cached prompt.

## 5f. One launcher bundle, voice screen sharing, HTTPS big brain (2026-11-02)

**One bundle for the N95:** `Triton Launcher\build.ps1 -RebuildVoice` makes `dist\TritonLauncher` (+ zip) with:
- the launcher itself
- `voice\TritonVoice.exe`: **this assistant, frozen with PyInstaller** (`build-voice.ps1`). It first runs
  `make_n95_bundle.py --out voice-build\stage`, then freezes `assistant.py` (`--collect-all` openwakeword, vosk, winrt,
  sherpa_onnx, piper (espeak-ng-data), pywinauto; comtypes submodules) and copies `models\`, `llama.cpp\`, `settings.json`,
  `shortcuts.json` and `BRAIN.txt` next to the exe. The VC++ runtime DLLs go into `llama.cpp\` (app-local), so the N95
  needs neither Python nor a VC++ install.
- `display\`: Triton Screen Share, now with its control pipe.

**The N95 brain is the fine-tuned Qwen3-0.6B Q4_K_M** (86.0%, 378 MB, ~0.5 s on the laptop). The 1.7B was only +0.5 points
at twice the time and 3x the size (section 4), so it isn't used.

**Screen sharing by voice** (`tools/screenshare.py`; tool `screen_share` in `tools/schema.py`):
- `screenshare.intent()` spots these sentences with fixed patterns **before** the rules and the model (`assistant._understand`),
  so they're instant and need no retraining. The big brain's full prompt lists the tool too, for odd phrasings.

  | Say | Action | What happens |
  |---|---|---|
  | "mirror my laptop", "show my laptop here", "share my screen", "screen share" | `mirror` | TV starts "Show here"; the laptop shares by itself, or is asked to (pull) after 4 s |
  | "extend my laptop screen here", "use the TV as a second screen", "switch to extend" | `extend` | the same, then the laptop switches to its virtual second screen |
  | "share the sound" / "mute the laptop" | `sound_on` / `sound_off` | the laptop's sound on the TV |
  | "share my mic" / "stop sharing my mic" | `mic_on` / `mic_off` | the TV's mic → the laptop's apps (VB-Cable "Triton Mic") |
  | "share my camera" / "turn the webcam off" | `camera_on` / `camera_off` | the TV's webcam → "Triton Camera" on the laptop |
  | "hide the laptop screen" / "bring the laptop screen back" | `hide` / `unhide` | |
  | "stop sharing", "stop mirroring", "disconnect the laptop" | `stop` | |
- It talks to the bubble over `\\.\pipe\TritonScreenShare` (this Windows user only): one line in (`show extend`), one JSON
  status line back. If the bubble isn't running, it starts `..\display\TritonScreenShare.exe --background` first.
- For mirror/extend it waits up to 20 s (`"screen_share": {"connect_wait": 20}`) for the picture, then says "Your laptop is
  on screen", or why not ("I can't find your laptop…"). Follow-up listening stops once the laptop is on screen, because its
  sound would be heard as commands.
- `"screen_share": {"role": "share"}` (for an assistant on the laptop) makes "share my screen" send *this* screen to the TV.
- **Tested:** 56/56 phrases (40 screen-share sentences, plus 16 ordinary commands that must not match). The pipe and the tool
  were tested against a test copy of Triton Screen Share (status, show, stop, sound, errors). **Not yet tested between the
  real laptop and the TV.**
- Hands-free needs the laptop and TV **paired once** (6-digit code, a click on each) and Triton Screen Share running on the
  laptop. `Set Up Laptop.bat` makes it start with Windows and share automatically. A pull from a paired PC is now accepted
  without a click.
- **Watch out:** the Rapoo mic only opens in exclusive mode for screen sharing (see the Triton Screen Share notes). If "share
  my mic" takes it exclusively, Triton can't hear you until the mic share stops: use the panel, or test this first.

**HTTPS big brain** (`brain_server.py`, `brain/remote.py`):
- The server makes its own certificate once and serves HTTPS (TLS 1.2+). The certificate is ECDSA P-256, valid 10 years,
  stored as `%USERPROFILE%\TritonModels\big_brain_cert.pem` + `_key.pem` (outside OneDrive). `big_brain.json` gets
  `"cert_sha256"`.
- The N95 **pins** that fingerprint (the `PinnedCertificate` adapter, urllib3 `assert_fingerprint`) instead of trusting
  certificate authorities. The check runs in the TLS handshake, before the key or any command is sent. With a wrong
  certificate, nothing is sent and the laptop is skipped for 60 s.
- `make_n95_bundle.py` is the one-time pairing. It writes `"big_brain": {"url": "https://<laptop IP>:8765",
  "urls": ["https://<laptop name>:8765"], "key", "cert_sha256", "mode": "fallback"}` into the N95's settings. `"urls"` are
  tried when the IP fails (if the laptop's address changed).
- **Tested on the laptop:**
  - right certificate: `/understand` and `/answer` work, and the second URL is used when the first is dead
  - wrong fingerprint: refused before anything is sent
  - wrong key: 401
  - https without a fingerprint: not used
- **Warm-up fix:** the 9B needs ~70 s to read the full tool prompt on this CPU. The warm-up request had a 60 s limit, and
  when it ran out the start thread died silently, so the server answered 503 "not ready" forever. The warm-up now gets 600 s,
  and a failed warm-up no longer blocks "ready" (`brain/llm.py` `_start`).
- **Tested over the Wi-Fi address with the N95 bundle's own settings (pinned HTTPS):**

  | Request | Answer | Time |
  |---|---|---|
  | "bring up spotify and skip to the next song" | `open spotify` + `media next` | 20.5 s |
  | "close this window" | `close` | 6.5 s |
  | "could you put my laptop on the tv as a second screen" | `screen_share extend` | 7.4 s |
  | `/answer` "what's the capital of australia" | "The capital of Australia is Canberra." | 4.7 s |

  Windows Firewall already allows `C:\Python313\pythonw.exe` inbound on Public, so the N95 can reach port 8765 as is.
- **Laptop, once:** double-click **`Set Up Laptop.bat`** (`setup_laptop.ps1`). It:
  - starts the big brain at every sign-in (hidden, via `start_brain.vbs`; log in `brain_server.log`)
  - adds a firewall rule for TCP 8765 from the local subnet only (admin prompt; works with the Wi-Fi on Public)
  - sets Triton Screen Share to start with Windows and share automatically

## 5e. Conversation, N95-first routing, saved command audio (2026-10-30, later)

- **The N95 bundle now uses `"mode": "fallback"`.** In order:
  1. instant rules (when certain)
  2. **the N95's fine-tuned 0.6B (~0.5 s on the laptop, ~1.5 s on the N95)**
  3. the laptop, only if the 0.6B gives nothing, asks back, or its answer looks wrong
- **What counts as "looks wrong"** (`assistant._doubtful`), from the 0.6B's known round-1 slips:
  - typing with no "type" / "write" in the sentence
  - a question turned into typing, a click or an open
  - "search" / "find" / "look" with no search tool
  - "play X" turned into the bare play key
  - opening an app that doesn't exist
- A clear question the 0.6B fumbled goes **straight to the laptop's `/answer`**, skipping a second round trip.
- Tested (local 0.6B + laptop 9B):
  - "volume up", "open netflix", "click shashank", "scroll down", "next episode", "type hello world", "minimize this": **0.0–0.6 s, N95 only**
  - "play lofi music on youtube" → laptop → open + YouTube search (16 s)
  - "why is the sky blue" → laptop answer
- **Conversation:** after Triton answers, it keeps listening for **5 s for a follow-up without the wake word** (`"follow_up_sec"`, `0` = off),
  up to 8 turns. It shows "Listening…" and stops on silence, two misses in a row, "that's all" (cancel), or **once something starts playing**
  (media play, or clicking "Play"), so a film's dialogue isn't taken as commands. "Hey Triton" said inside a follow-up is stripped.
- **Conversation memory:** the N95 sends the last 6 turns (`{"user", "reply"}`) with every laptop request (`brain/remote.py`), and
  `brain_server.py` / `LocalLLM.ask/answer(history=…)` include them, so "how far is the moon" … "and mars?" works. Cleared when the
  conversation ends. **Restart `Start Big Brain.bat` to get this.** An older server ignores the history.
- **`brain_server.py` refuses to start if port 8765 is taken.** On Windows a second copy used to bind silently: two 9B models in 16 GB.
- **Ears:**
  - `transcriber.trim_silence` cuts the quiet around a command before Whisper hears it: short words 23 → 25 of 40 right.
  - **Every command's audio + what Whisper and the grammar heard** are saved to `voice_samples\commands\` on the N95
    (newest 500; `"save_command_clips": false` turns it off). Bring them back to measure the ears on your real voice and mic,
    then pick bigger ears or fine-tune Whisper on your voice (like the wake word).
- **Screen control:** `click <name>`, `type …`, `press …`, `scroll` already work by voice, and only risky clicks (buy, pay, send, delete,
  install, sign out…) ask for a "yes" first. With follow-ups, "open netflix" … "click shashank" … "click play" works as a conversation.

## 5d. How the N95 assistant decides and talks (2026-10-30)

**Decision order** (`assistant._understand`; `settings.json` → `"big_brain": {"mode": ...}`). Each step runs only if the one before it can't place the command:
1. `"always"` mode only: the laptop gets everything first.
2. **The fixed rules, when they're certain (instant).** "volume up", "pause", "open youtube", "close this window".
   - `brain/rules.confident()` no longer trusts "play X": media commands only allow media words, and "youtube" sentences only
     allow open or YouTube search.
   - This fixes "play … on YouTube" opening YouTube Music (it was `media play` → `play_music()`).
3. **The laptop's big brain** (`"first"`, the default for the N95). The bubble shows "Thinking…".
4. The N95's own fine-tuned model.
5. `"fallback"` mode: the laptop, but only if the N95's model couldn't place it or asked a question back.
6. Last resort: the rules' unsure guess, except "play X".

`brain/remote.py` talks to the laptop. It connects with a 1.5 s timeout; if the laptop is off, it's skipped for 60 s, so a dead
laptop costs 1.5 s once. `escalate` (questions) → laptop `/answer` → spoken; without the laptop, "My big brain on the laptop isn't reachable".

**Hearing (fixed 2026-10-30, after "search on Netflix" kept muting the N95):**
- **The cause:** the instant command grammar (Vosk with a word list) knows only **341 words, and "search" isn't one of them**
  (nor "find", "type", "watch", "movie", "profile", "choose"). A word-list recognizer must pick words from its list, so
  "search on netflix" became "silence netflix". The rules were *certain* that meant mute, and Whisper never ran.
- **Now (`assistant._listen_for_command`):**
  - Whisper hears **every** command. Listening ends when the free Vosk recognizer hears you stop; the grammar no longer cuts it short.
  - Whisper's words decide when they form a clear command.
  - Otherwise the grammar's command is used **only if the free recognizer (no word list) heard the same command**. The grammar is
    good at one-word commands Whisper fumbles ("mute" → "Nute"), and the agreement check blocks "silence netflix".
  - Otherwise the brain gets Whisper's words. `transcriber.tidy` also drops the quote marks Whisper puts around queries.
- **Test:** spoken commands in 4 voices fed through the real listening code. **0 false mutes in 76 cases.**
  - "Search on Netflix", "Search it", "Choose my profile" and "Search for rain sounds" are heard right and go to the brain.
  - "Volume up" and "Set the volume to 30 percent" run instantly.
  - Weak spot: single words like "Mute" or "Stop" in some voices; those go to the brain instead of acting wrongly.
  - Cost: about +1–1.5 s per command on the N95 (Whisper runs every time).
- **If it's still not good enough:** record real commands on the Rapoo and score Vosk vs whisper-base vs whisper-small (`bench/stt_bench.py --record`),
  or fine-tune Whisper on your voice on the cluster.

**Talking:**
- On the wake word it **says "Yes?" / "Yes sir?" / "Listening."** instead of the chime (`"wake_reply"` in settings; `[]` brings the chime back).
- **The action is announced as soon as it's understood**, while it runs (`tools/handlers.say_first`: "Opening YouTube, then searching YouTube for lofi").
  Results that *are* the answer (time, battery) are still said afterwards.
- **The N95 voice is Piper `en_GB-alan-medium` again.** Kokoro needed 1.5–2.5 s per reply on the N95. The laptop keeps Kokoro.
  `make_n95_bundle.py --voice/--speaker` picks the N95 voice.

**Tested from the bundle against the laptop (2026-10-30):**

| Said | Path | Time |
|---|---|---|
| "volume up" / "pause" / "close this window" | rules | 0.0 s |
| "play lofi music on youtube" | laptop 9B → open youtube + youtube_search + play | **17 s** |
| "what's the capital of japan" | laptop `/answer` → "The capital of Japan is Tokyo…" | 8.8 s |
| laptop off | gives up | 1.5 s, then skipped |

**The 9B is slow on the laptop's CPU (6–17 s per command).** For speed, choose one:
- `"mode": "fallback"` in the N95 settings: the N95's 0.6B answers first in ~1.5 s, and the laptop only gets the hard ones.
- Or set `big_brain.json` `"model"` to Qwen3-4B (about 2x faster, 86.9%).

## 5c. The laptop "big brain" (receiver)

*Now HTTPS with a pinned certificate, started at sign-in: see section 5f. The notes below are from the HTTP version.*

`brain_server.py` keeps a bigger model loaded on the laptop and answers the N95 over the home network. Start it by
double-clicking **`Start Big Brain.bat`**, and keep that window open.
- **Model:** Qwen3.5-9B Q4 by default (92.6% on the test commands). Set by `big_brain.json`, which is created on first run with
  `port` (8765), a random `key`, `model` and `threads`. Laptop only: not bundled, never shared.
- **`POST /understand {"text": ...}` → `{"calls": [...]}`:** tool calls for a command the N95 wasn't sure about. The N95 still carries them out.
- **`POST /answer {"text": ...}` → `{"answer": "..."}`:** a 1–2 sentence spoken-style answer to a non-command question.
- **`GET /health`.** Every request needs `Authorization: Bearer <key>`; a wrong key gets 401. It **never runs anything on the laptop**.
- Two llama-server slots, so commands and answers each keep their own cached prompt. Otherwise each question would force a 37 s
  re-read of the tool prompt.
- Plain HTTP with a shared key: fine on the home network, but **don't forward the port to the internet**. Windows asks once
  whether Python may accept connections: allow **Private networks** only.
- It prints the laptop's addresses at startup (now `192.168.0.204`). The N95 will need `"big_brain": {"url": "http://192.168.0.204:8765", "key": "<key>"}`.

**Tested over the network (2026-10-30):**
- Wrong key → 401. Load time 73 s.
- `/understand`:
  - "close this window" → `close` (the N95's 0.6B says minimize)
  - "bring up spotify and skip to the next song" → `open spotify` + `media next`
  - "open youtube and search for cricket highlights" → `open youtube` + `youtube_search`
  - 6–13 s each.
- `/answer`: "what's the capital of australia" → "The capital of Australia is Canberra." (3.7 s). Other answers took 7–11 s.
- `brain/llm.py` gained `LocalLLM.answer()` (free text, no tool grammar) and a `slots` option.

## 6. Wake-word retraining: "Hey Triton" round 2 (done 2026-10-30)

**Result (job 1856821, Rapoo mic, 255 takes / 98 negatives; 32 test takes never trained on):**

| | Round 1 | **Round 2** |
|---|---|---|
| Your untouched test takes caught (threshold 0.2, 2 frames) | 68% of Rapoo takes | **32/32** (32/32 at 0.5 too) |
| False wakes, 218 other clips (sound-alikes, your speech, TV-like sentences) | 1 ("Hey Brighton" 0.74) | **0** (highest 0.17) |
| `your_voice` in the log (heavily augmented held-back takes, harder than round 1's test) | 0.891 | 0.894 at 3.95 fp/hr |

**Voice check** (`train_voice_check.py --word hey_triton`, 600 computer voices + Windows voices + your other recordings):
- Your takes score 0.99–1.00.
- Single-frame scoring let 20/206 other voices through. With the assistant's real rule (2 frames in a row):
  - at **threshold 0.5: you 32/32 (lowest 0.84), other voices 0/500**
  - at 0.2: 1/500
- So the **N95 uses `wake_threshold` 0.5** (set by `make_n95_bundle.py`).
- Caveat: some of those 500 voices may have been in the check's training sample.

*How it was prepared:*

**Goal:** wake *every time* for your voice, and only for your voice. Two models share the job:
- **The wake model** (`models\hey_triton.onnx`, trained on the cluster) hears the phrase. Round 1 caught 0.89 of
  your held-back takes, and 79/95 of all your takes at threshold 0.5.
- **The voice check** (`models\hey_triton_verifier.pkl`, trained on the laptop by `train_voice_check.py`) rejects other
  people. It trains against 400 computer voices saying "hey triton" (`models\hey_triton_many_voices`), Windows voices,
  and your own other recordings.

**What changed for round 2:**

| Where | Change |
|---|---|
| `cluster_training/my_voice.py` | **Heavier augmentation of your takes:** speed ±15%; a random mic (band-pass 60–400 Hz to 2.8–7.6 kHz: cheap, far or TV speaker); room echo (75%); loudness from 1/30 to 2.5×; background in 85% of copies (music 0–25 dB, **your own chatter** 5–25 dB, hiss or rumble 5–30 dB); 5% clipping |
| | **Your "don't fire" recordings are augmented too** (6 varied copies), so "Hey Tristan" in a noisy room still doesn't fire |
| `cluster_training/train_hey_nova.py` | **120 copies per take** (was 40), so your voice outnumbers the computer voices. Env vars: `MY_VOICE_COPIES`, `MY_VOICE_NEG_COPIES` |
| | 15% of takes held back for the `your_voice` score (was a fixed 10) |
| | Real wake-ups saved by the assistant (`live/wake_*_cmd.wav`) count as extra takes |
| `cluster_training/train.slurm` | Time limit 4 h → 6 h, for the extra augmentation |
| `record_samples.py` | All 10 Hey Triton sound-alikes (was 6). New **`--test`** round saves to `test\`, which is **never uploaded** |
| `bench/wake_bench.py` | **`--test`** scores the model on that untouched test set: the honest "wakes every time" number |

**Step 1: record (~25 min in total, ideally spread over two days).** Best done **on the N95 itself, with its own mic,
where you'll really sit**: the bundle includes `record_samples.py`. Run it in the TritonVoice folder with
`.venv\Scripts\python.exe record_samples.py --word hey_triton ...`, then copy `voice_samples\hey_triton` back to the laptop.

| Round | Command flag | Takes | Why |
|---|---|---|---|
| Normal, on the N95 mic | *(none)* | 20 + 10 sound-alikes + sentences + room noise | The real mic's sound |
| Quiet and soft, x2 | `--more` | 2 × 30 | Whispers, lazy voice, music playing |
| From your seat, x2 | `--far` | 2 × 40 | The real distance and echo |
| **Test (keep local)** | `--test` | 30 | Honest score; never trained on |

That's ~250 takes with the 95 you already have. Use different times of day and moods (tired, loud, with the TV on).

**Mic:** `record_samples.py` **only records with the Rapoo webcam mic**, the one the N95 uses (it's plugged into the laptop for
recording). If Windows doesn't show it, the recorder says "Rapoo mic not found" and records nothing, instead of silently
using the laptop's mic array. `--any-mic` overrides this. It prints the mic it's using when it starts.

**Step 2: cluster.** The synthetic clips from round 1 are cached, so only your-voice augmentation and training rerun (~2–3 h on an A100).
```
# laptop: upload the code, then your recordings (NOT test\)
scp "C:\Users\shash\TritonBundle\Voice Assistant\cluster_training\my_voice.py" "C:\Users\shash\TritonBundle\Voice Assistant\cluster_training\train_hey_nova.py" "C:\Users\shash\TritonBundle\Voice Assistant\cluster_training\train.slurm" tinyx:/home/woody/iwem/iwem119h/cluster_training/
ssh tinyx "mkdir -p /home/woody/iwem/iwem119h/hey_nova/my_voice/hey_triton"
scp -r "C:\Users\shash\TritonBundle\Voice Assistant\voice_samples\hey_triton\positive" "C:\Users\shash\TritonBundle\Voice Assistant\voice_samples\hey_triton\negative" tinyx:/home/woody/iwem/iwem119h/hey_nova/my_voice/hey_triton/
# cluster: keep round 1's model, then train
cp $WORK/hey_nova/hey_triton.onnx $WORK/hey_nova/hey_triton_round1.onnx
cd $WORK/cluster_training && WAKE_PHRASE="hey triton" sbatch train.slurm
tail -f $(ls -t wakeword_*.log | head -1)     # watch "your_voice=" (round 1 ended at 0.89)
```

**Step 3: laptop.** Bring the model home, retrain the voice check, score it honestly, and rebuild the N95 bundle:
```
scp tinyx:/home/woody/iwem/iwem119h/hey_nova/hey_triton.onnx "C:\Users\shash\TritonBundle\Voice Assistant\models\"
.venv\Scripts\python.exe train_voice_check.py --word hey_triton
.venv\Scripts\python.exe bench\wake_bench.py --wake hey_triton --test
.venv\Scripts\python.exe make_n95_bundle.py
```
Then copy `models\hey_triton.onnx` and `models\hey_triton_verifier.pkl` to the N95's TritonVoice folder.

---

## 7. Plans and ideas

- **Laptop big brain:** done (HTTPS, pinned certificate, API key; section 5f). Model candidates: Qwen3.5-9B now; the
  fine-tuned 4B if it beats 92%.
- **Speed-ups for big models on the laptop:**
  - keep the server running and save the read prompt to disk
  - speculative decoding (a small draft model guesses ahead)
  - llama.cpp Vulkan on the Iris Xe for prompt reading
  - "Best performance" power mode
- **Hardware options:**
  - 2x16 GB RAM (~€60–80) gives room, not speed.
  - An **eGPU over Thunderbolt 4** with a 16 GB card would make Gemma 4 12B and other 12B+ models instant.
- **Web answers:** search (DuckDuckGo or self-hosted SearXNG), then the 9B reads the snippets, then Piper
  speaks. Only the search words leave the PC. **Safety rule:** the step that reads web results may only
  answer, never control the PC.
- **N95:** pywinauto works on Windows 22H2. Pin `pywinauto==0.6.9` and `comtypes==1.4.17`.
- **Known rule bugs:**
  - Vosk hears "bring up spotify" as "bring apps firefox".
  - The rules stop listening too early on multi-step commands ("set volume to 20 and play music").
- **Later:** an ESP32 ring mouse (BLE HID plus a motion sensor and TinyML gestures), webcam hand gestures
  (MediaPipe), a projector or AR display, and PyInstaller packaging. The package must include piper's
  espeak-ng-data, sherpa-onnx and pywinauto.

---

## 8. Glossary

- **Quantization:** storing weights in fewer bits (16 → 8 → 4) to shrink the model and make it faster;
  `Q4_K_M` ≈ 4.5 bits per weight. Quality drops a little; the benchmark shows how much.
- **Importance matrix (imatrix):** measures which weights matter most on our data, so quantization keeps
  those more precise.
- **Distillation:** a small *student* learns from a big *teacher*. Here, by training on the teacher's sentences.
- **Full fine-tune vs LoRA:** a full fine-tune updates every weight. LoRA freezes the model and trains
  small add-on matrices; it's cheaper, and is merged back in afterwards.
- **MoE (mixture of experts):** a big model where only a few "experts" run for each token.
  It has the knowledge of a big model at the speed of a small one, but needs the RAM for all of it.
- **Prefill vs decode:** prefill means reading the prompt; decode means writing the answer
  token by token. On a CPU, decode speed is limited by memory speed.
- **GGUF:** llama.cpp's model file format. **llama-server:** llama.cpp's local HTTP server that Triton talks to.
- **Grammar-constrained output:** llama.cpp only allows tokens that keep the answer valid JSON for our tools.
