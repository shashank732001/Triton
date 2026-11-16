# Triton Brain: plan

Goal: say anything to "Hey Triton" and have the N95 do it. That means opening, clicking, typing and
controlling any app through Windows UI Automation (pywinauto). Everything runs on the N95 using our
own small, quantized model, trained on the university A100s. The laptop is optional extra power,
never a requirement.

## Hardware we design for

| Device | Specs | Role |
|---|---|---|
| **NUC** | Intel N95 (4 cores), 8 GB RAM, UHD graphics (shared RAM), 250 GB | **Target.** Runs everything. Budget: model <= ~1 GB RAM, whole assistant <= ~2.5 GB, leaving room for Chrome |
| **Laptop** | Dell Latitude 3420, i5-1135G7 (11th gen, 4C/8T), 16 GB, Iris Xe, 512 GB NVMe | Development and benchmarking. Optional "big brain" on the LAN for open-ended questions |
| **Cluster** | FAU tinyGPU A100 (GPU nodes have no internet; same setup as the wake-word jobs) | Data generation, fine-tuning, distillation, quantization |
| Later | ESP32 satellites, servos, lights | Out of scope for now |

## What exists today (keep it)

- **Wake word:** openWakeWord `hey_triton.onnx` plus a personal voice check (trained on the cluster).
- **Speech-to-text:** Vosk small, restricted to the command vocabulary. Fast and accurate, but it
  can only hear words it already knows.
- **Understanding:** `commands.py`, an ideas-based rule parser. It's instant and handles the common
  commands well.
- **Actions:** `actions.py`, `desktop.py`, `apps.py`, `monitor.py`, plus the launcher pipe.
- **Feedback:** chimes and the overlay bubble.

The new design **adds to** this rather than replacing it. The rule parser stays as the 0 ms fast path.

## Architecture (all on the NUC, inside TritonVoice)

```
 mic -> [wake word: hey_triton] -> [speech-to-text] -> text
                                                        |
                                                   ROUTER (cascade)
          +---------------------------+-----------------+--------------------------+
          | 1. rules (commands.py)    | 2. Triton-Mini (local LLM)  | 3. laptop LLM (optional) |
          |    confident? -> run      |    JSON tool call(s),        |    chat / hard questions,|
          |    ~0 ms                  |    grammar-constrained       |    only if reachable     |
          +---------------------------+-----------------+--------------------------+
                                                        |
                                                  TOOL LAYER (registry + safety)
            system: volume/media/screen/launcher | apps & windows | browser/sites |
            UIA (pywinauto): read UI tree, click, type, keys, scroll | answer (speak only)
                                                        |
                                        [TTS: Piper] + chime/bubble  ->  speaker
                                                        |
                                 trace store (SQLite) -> training data for the next model
```

### 1. Speech-to-text: open vocabulary
- Candidates: **Moonshine** (ONNX, built for edge devices, low latency), **whisper base/small**
  int8 (faster-whisper), and Whisper via **OpenVINO** on the UHD graphics.
- Keep the Vosk grammar recognizer as a parallel fast path. If Vosk's grammar result matches a
  rule with high confidence, skip the slower model.
- Pick a winner by benchmarking on recordings of my own voice (word error rate and latency on the N95).

### 2. Router cascade
1. **Rules:** `Commands.understand()` already scores matches. Above a threshold, run the command.
2. **Triton-Mini:** our fine-tuned small LLM. Output is forced into a JSON tool-call grammar
   (llama.cpp GBNF), so even a 0.6B model can't produce invalid calls. It can return a short plan
   (several calls), or `escalate`, or `clarify("which window?")`.
3. **Laptop:** a llama.cpp server with a 3–8B model over the LAN, used when Triton-Mini says
   `escalate` (general knowledge, long answers). If the laptop is off: "I can't answer that offline."

### 3. Tool layer
One registry: name, JSON schema, risk level, handler. The same schema drives the model's training
data, the grammar, and the safety checks.

- **High-level tools first** (reliable for a small model): `open_app`, `close_app`, `switch_to`,
  `set_volume`, `media(play|pause|next)`, `open_site`, `youtube_search(query)`,
  `browser(new_tab|back|scroll)`, `screen(off|on)`, `type_text`, `press_keys`, `go_home`,
  `show_open_apps`, `answer(text)`.
- **UIA tools** for everything else, via pywinauto with the `uia` backend:
  - `ui_snapshot(window)`: a compact, numbered list of visible controls
    (`[12] Button "Subscribe"`, `[13] Edit "Search"`), trimmed to ~60 lines so the prompt stays short.
  - `click(id)`, `set_text(id, text)`, `invoke_menu(path)`, `scroll(direction)`.
  - A short agent loop: snapshot -> act -> snapshot, max ~5 steps, with spoken progress.
    Small models are weak at long GUI tasks. They handle the common cases; hard ones escalate to
    the laptop model.
- **Safety:** the risk level decides what happens.
  - `safe` runs straight away.
  - `confirm` asks "Close Word without saving?" and waits for yes/no. This covers closing
    unsaved work, delete, shutdown, sending messages and purchases.
  - `never`: no typing into password fields (UIA `IsPassword`), no admin or system settings.
  - Every action is logged.

### 4. Text-to-speech
- **Piper** (ONNX voice, ~60 MB, much faster than real time on the N95) for short replies:
  "Opening YouTube", "Volume 30", answers.
- Stream the reply sentence by sentence. Keep the chimes for instant feedback.

### 5. Trace store
- SQLite, one row per request: audio clip id, text heard, router tier, tool calls, result, latency,
  and any **correction** ("no, I meant Brave").
- This becomes Triton-Mini's next training set: real phrasing, real speech-to-text errors, real failures.

## Triton-Mini: the edge model (the learning part)

**Targets on the N95:** >= 95% exact-match tool calls on the test set; < 1 s from end of speech to
action for single commands; model file <= 500 MB.

1. **Baseline:** benchmark off-the-shelf candidates on the laptop and the N95. Candidates are
   Qwen3-0.6B, Qwen3-1.7B, Gemma-3-1B, Llama-3.2-1B and SmolLM2-1.7B.
   - Runtimes: llama.cpp (Q8_0, Q4_K_M) and OpenVINO INT4 (CPU vs UHD graphics).
   - Metrics: accuracy, time to first token, tokens/s, peak RAM, size.
2. **Data (cluster, teacher model):**
   - Seed: every `COMMANDS` entry, the app catalog, `shortcuts.json` and the tool registry.
   - A big teacher (e.g. Qwen3-32B in vLLM, offline on a GPU node) generates tens of thousands of
     varied requests with the correct tool calls: multi-step, ambiguous -> `clarify`,
     off-topic -> `escalate`, cancellations ("never mind").
   - **Speech-noise augmentation:** add realistic speech-to-text errors ("open crome", "valium up")
     so the model is robust to what the recognizer actually outputs.
   - Splits: train / validation / test. The test set is held out and later grows from real traces.
3. **Fine-tune (A100):** full fine-tune for 0.6B, LoRA for 1.7B (TRL or Unsloth).
   - Then **knowledge distillation**: train on the teacher's output distributions, not just its text.
   - Later: DPO preference training on corrections from the trace store.
4. **Prompt diet (big win on edge):** after fine-tuning, the tool schemas live in the weights, so
   the prompt shrinks from ~800 tokens to ~30.
   - Prefill (reading the prompt) on the N95 is the slow part, so this alone can save seconds.
   - Also cache the fixed prefix (KV cache) between requests.
5. **Compression sweep:** merge the LoRA, then export GGUF Q8_0 / Q5_K_M / Q4_K_M / Q3_K_M /
   IQ quants (with an importance matrix from our data). Also export OpenVINO INT4 with NNCF
   data-aware (AWQ) compression.
   - Plot accuracy against latency, RAM and size, and pick the smallest model that meets the targets.
   - Stretch goals: structured pruning plus distillation "healing" (Minitron-style), vocabulary
     trimming, speculative decoding (a tiny draft model with a bigger verifier).
6. **Ship:** copy the winning model to `voice\models\triton-mini.gguf`, as a drop-in file, the
   same way the wake-word models work.

## Code layout (in `Voice Assistant/`)

```
brain/        router.py, llm.py (llama.cpp / OpenVINO wrapper), grammar.py
tools/        registry.py, system.py (wraps actions/desktop), uia.py (pywinauto), safety.py
speech/       stt.py (Moonshine/Whisper/Vosk), tts.py (Piper)
traces.py
bench/        eval/*.jsonl, bench.py (any model x runtime x device -> results.csv), plots
training/     gen_data.py, sft.py, distill.py, export_quant.py, *.slurm, README.md  (cluster)
cluster_training/   (unchanged: wake word)
```

## Milestones

1. **M1: benchmark harness and eval set.** Build ~300 test requests from the current commands plus
   new UIA-style ones. Run `bench.py` for baselines on the laptop and then the N95.
2. **M2: tool registry and pywinauto UIA tools with safety.** Router step 2 starts with a stock
   Qwen3-1.7B. Goal: the first end-to-end "click the search box on YouTube and type lofi".
3. **M3: open-vocabulary speech-to-text, Piper replies, trace store.**
4. **M4: cluster.** Generate data, fine-tune Triton-Mini, run the compression sweep, pick the winner,
   ship it.
5. **M5: multi-step UIA agent loop, plus laptop escalation.**
6. Later: ESP32 satellites, servos and lights.
