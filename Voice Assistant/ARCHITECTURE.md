# Triton on the N95: target architecture (2026-11-10)

Design from four web-research reports (other local assistants, audio, the brain, how an assistant feels) combined with
Triton's own measurements (README 5q-5s). Nothing here is built yet. Numbers marked *est.* are estimates; no source
measured an N95, so every new part has to be tested on Triton's own recordings before it replaces anything.

## The main idea

Triton does not need a bigger model. Published results for narrow assistants agree with ours: a fine-tuned 0.35-0.6B
model reaches 91-97% and can beat its teacher ([distil labs, Qwen3-0.6B 90.9%](https://www.distillabs.ai/blog/the-llm-in-your-voice-assistant-is-the-bottleneck-replace-it-with-an-slm),
[LFM2.5-350M 96.7%](https://huggingface.co/distil-labs/distil-lfm25-home-assistant)). What makes a narrow assistant
feel "super intelligent" is everything around the model:

1. **It hears the right words** (speech-to-text that is told which words to expect).
2. **It knows who is talking** (you, not the TV).
3. **Code does what the model is bad at** (splitting two-part commands, matching names, deciding when to act).
4. **It knows you** (your phrases, your nicknames, your habits) and gets better every week.

Our remaining misses are exactly these: garbled words, TV speech, two-part commands, act-vs-answer, label noise.

## Pipeline

```
mic ──► echo cancel (TV sound removed, loopback reference)
     ──► wake word + speech gate            ──► earcon "I'm listening" + on-screen overlay
     ──► [A] speaker score (is it you?)   ┐
     ──► [B] speech-to-text, boosted with │   runs in parallel while you speak
            app names + on-screen names   ┘
     ──► end of turn (silence; smart-turn model only if the sentence looks unfinished)

text ──► T0 rules                      (exists)                         ~5 ms
     ──► T1 your own confirmed commands (exact + fuzzy match)           <50 ms est.
     ──► T2 split "X and Y" into parts  (code; each part restarts at T0)
     ──► T3 Triton-Mini 0.6B            (cached prompt, output limited
                                         to real apps / on-screen names) 0.4-2 s
     ──► T4 name resolver               (sound-alike + spelling distance)
     ──► T5 laptop big brain            (knowledge questions; "let me check")

decide ──► one evidence score = speaker score + word confidence + model confidence + how risky the action is
        ──► act  |  act, undoable  |  ask "Chrome or Edge?"  |  "say that again"  |  ignore silently

do ──► action first, then a short pre-recorded spoken reply; overlay shows what was heard
log ──► every turn, with outcome (repeated? undone?) ──► weekly review ──► aliases now, training rows for the next model
```

## Ears

| Part | Now | Target | Why |
|---|---|---|---|
| Echo cancel | attempts | WebRTC AEC3 with the PC's own output as reference, resampled to the mic clock, delay re-estimated continuously | TV speech is the biggest source of false commands. Clock drift between loopback and mic is the known reason this fails on Windows ([Mozilla bug](https://bugzilla.mozilla.org/show_bug.cgi?id=884365)) |
| Wake word | one model | add a speech gate and a verifier trained on recordings of your own TV | Smart speakers misfire about once per hour of TV ([study](https://petsymposium.org/popets/2020/popets-2020-0072.php)) |
| Speaker check | none | CAM++ (7M parameters) or ECAPA via sherpa-onnx, enrolled in the room with the TV on, used as a score, not a hard gate | Short far-field sentences are much harder than the published 0.7% error, so it must be tuned on your recordings |
| Speech-to-text | Vosk partials + Whisper-base | A/B test a streaming transducer (Zipformer, or Nemotron 0.6B int8) in sherpa-onnx with a hotword list | Only transducer models support real word boosting ([sherpa-onnx](https://csukuangfj.github.io/sherpa/onnx/hotwords/index.html)); Whisper and Moonshine cannot. The list = app catalog + on-screen names + command words, rebuilt per screen. Targets "mute the toy", "net flicks", "new tap" |
| End of turn | silence timeout | keep; add Smart Turn v3 (8 MB, 13-95 ms on CPU) only if the wait feels long | [Daily](https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/) |
| Voice | Piper | keep Piper; pre-record the ~100 most common replies | Piper is the fastest option measured; newer voices are 3-4x slower |

Speech-to-text gets echo-cancelled audio only, never noise-suppressed audio (denoisers can raise word errors).

## Brain

Keep the generative 0.6B model as the core and put cheap tiers in front. Classifier-only designs are fast on fixed
commands but cannot write search queries or typed text and reject off-topic speech poorly
(40% false accepts in one [router test](https://dev.to/sitanshukr08/synaptoroute-a-study-in-local-semantic-routing-2mid)).

- **T1 personal command memory.** A table of `normalized sentence + app in front -> calls`, filled only from commands
  that worked at least twice and were never undone. Exact match first, then fuzzy/embedding match with a margin over
  the runner-up. Home Assistant's version of this tier took accuracy from 49.6% to 93.3% at 84 ms
  ([canonicalizer](https://community.home-assistant.io/t/assist-canonicalizer-improve-voice-assistant-accuracy-for-natural-device-control-with-instant-local-feedback/1014170)).
  Your top 50 commands become instant. Entries expire and are removed on undo.
- **T2 split first.** "Open YouTube and play real Madrid highlights" is split by code, then each half is a simple
  command the model already gets right. Splitting before understanding needs no retraining
  ([DialogUSR](https://arxiv.org/abs/2210.11279)). v2 and v3 both fail this sentence today.
- **T3 the model, constrained.** (a) Static prompt first so the cached prefix is reused; restore the cache at boot.
  On the N95 reading the prompt is most of the 0.9-2.4 s. (b) A grammar built per request: tool names are a fixed
  list; `open`/`switch_to` targets come from the app catalog; `click` targets from what is on screen. Wrong names
  become impossible.
- **T4 name resolver.** Match what was heard against real names by sound and spelling distance
  (+40.8% on mis-heard names in [one study](https://preview.aclanthology.org/setup/D19-3011)).
- **Confidence replaces word rules.** Read the probability of the first token (`calls` / `say` / `repeat`),
  calibrate it once on the 1000-item test, combine with speaker score and word confidence. Raw model confidence is
  badly off until calibrated ([Nyckel](https://nyckel.com/blog/calibrating-gpt-classifications)). `_mini_doubtful`
  stays for power/lock/close.
- **Silence is an answer.** Low speaker score and no wake word: ignore, say nothing. "Say that again" is only for
  you, and after one generic repeat Triton asks a targeted question built from its top two guesses, then gives up
  after three tries ([Google guidance](https://developers.google.cn/assistant/conversation-design/errors)).

## Learning loop (what makes it yours)

1. **Log** every turn: audio clip, text, screen, tier that answered, decision, time, and the outcome signals
   "repeated within 10 s" and "undone".
2. **Undo and teach.** "No, not that" reverses the last action and marks the turn wrong. "When I say X, do Y" adds an
   alias or a macro ("movie night") straight into T1.
3. **Weekly review page.** Shows the week's doubtful turns; you (or the laptop's big model as a first pass) fix the
   labels. Fixed turns become T1 entries at once and training rows for the next cluster run.
4. **Retrain in batches, never live.** A new model ships only if it beats the old one on the frozen 1000 items and the
   N95 set. Alexa's version of this loop cut failures by 30% ([paper](https://arxiv.org/pdf/1911.02557)); live
   retraining is where drift and bad data get in.
5. **Cleaner training data for v4.** Two or three teacher passes, keep only labels that agree, drop rows a label-error
   finder flags; add speech-to-text noise to training sentences. Rejected data left in costs small models 2-5 points
   ([APIGen](https://arxiv.org/abs/2406.18518)).

## Feel

In order of value, all near-zero compute: instant earcon on wake and on "got it"; act first, speak after; ~100
pre-recorded replies with 3-5 variants each; overlay with state and "heard: ..."; a 5-8 s follow-up window without the
wake word; targeted questions; undo; a small profile (name, nicknames, routines) used only on chat turns so it cannot
hurt commands; a weekly report (success rate, repeats, false wakes per hour, time to action).

## Build order

| Phase | What | Needs | Expected |
|---|---|---|---|
| 1. Brain tightening | confidence score, name resolver, compound split, per-request grammar, prompt cache, T1 memory | laptop only, replay on the 1000 items | fewer wrong actions, two-part commands fixed, top commands instant; accuracy +3-6 pts *est.* |
| 2. Feel | earcon, act-first, cached replies, overlay, follow-up window, undo | N95 launcher | feels much faster with the same models |
| 3. Ears | AEC reference fix, speaker check, transducer speech-to-text A/B, wake verifier | recordings from the N95 (you, TV, others) | fewer garbles and far fewer TV triggers; the largest real-world gain, and the least certain |
| 4. Learning loop + v4 | logging, review page, teach-me, cleaned data, cluster run | phases 1-3 logging | steady weekly improvement |

RAM stays inside today's budget: speech-to-text 0.3-0.7 GB, model 0.4 GB, the rest about 0.3 GB *est.*

## Not recommended

- A bigger local model: 3-8B models on an N150 took 10-150 s per answer
  ([report](https://community.home-assistant.io/t/running-a-local-llm-on-an-intel-n150-mini-pc-for-home-assistant-a-practical-journey/1007724)).
- Replacing the model with a classifier, or JEPA-style models.
- More context in the prompt: accuracy falls as tools and facts are added.
- Live on-device retraining.

## Open questions

- No source measured any of this on an N95; phase 3 picks depend on the A/B test.
- 100% is not reachable: some sentences are unclear even to a person. The goal is zero wrong actions, instant top-50
  commands, and a short, specific question when unsure.
