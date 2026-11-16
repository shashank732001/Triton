"""
Trains the personal voice check: a small model that only accepts the wake word in YOUR voice.
Works for this device's wake word (settings.json), or another one with --word, e.g.
    python train_voice_check.py --word hey_triton

Needs:
  the wake-word model           - models/<wake_word>.onnx (from the cluster), or an openWakeWord built-in one
  voice_samples/<wake_word>/positive/*.wav - your takes of the wake word   (from record_samples.py)
  voice_samples/<wake_word>/negative/*.wav - your other speech + room      (from record_samples.py)
Optional:
  models/<wake_word>_many_voices/*.wav     - the wake word in hundreds of computer voices, copied from
                                             a cluster run ($WORK/hey_nova/<wake_word>_output/<wake_word>/positive_test)

Also generates "other people saying it" with the built-in Windows voices, and uses your recordings
for your other wake words as extra "you, but not this word" examples. Prints a held-out test
before saving models/<wake_word>_verifier.pkl. Runs fully offline.
"""

import pickle
import random
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from openwakeword.custom_verifier_model import get_reference_clip_features, train_verifier_model
from openwakeword.model import Model

from assistant import BASE_DIR, FRAME_SIZE, SAMPLE_RATE, VERIFIER_GATE, WAKE_THRESHOLD
from assistant import WANTED_WAKE_WORD as WAKE_WORD      # the name chosen in settings.json

if "--word" in sys.argv:
    WAKE_WORD = sys.argv[sys.argv.index("--word") + 1]
NAME = WAKE_WORD
PHRASE = WAKE_WORD.replace("_", " ").title()         # "hey_echo" -> "Hey Echo"
_custom_model = BASE_DIR / "models" / f"{NAME}.onnx"
WAKE_MODEL = str(_custom_model) if _custom_model.exists() else NAME
VERIFIER_PATH = BASE_DIR / "models" / f"{NAME}_verifier.pkl"
ALL_SAMPLES_DIR = BASE_DIR / "voice_samples"
SAMPLES_DIR = ALL_SAMPLES_DIR / NAME
OTHER_VOICES_DIR = SAMPLES_DIR / "other_voices"
# Far better than Windows' 2 voices for teaching the check what "not you" sounds like
MANY_VOICES_DIR = BASE_DIR / "models" / f"{NAME}_many_voices"
MANY_VOICES_TRAIN, MANY_VOICES_TEST = 400, 200
PAD = np.zeros(SAMPLE_RATE, np.int16)      # 1 s of lead-in so the model has context before speech
HOLDOUT = 5

TTS_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$out = '{out}'
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$tts = New-Object System.Speech.Synthesis.SpeechSynthesizer
$n = 0
foreach ($v in $tts.GetInstalledVoices()) {{
  $tts.SelectVoice($v.VoiceInfo.Name)
  foreach ($rate in -2, 0, 2) {{
    foreach ($text in '{phrase}', '{phrase}!') {{
      $n++; $tts.Rate = $rate
      $tts.SetOutputToWaveFile((Join-Path $out ('other_{{0:D2}}.wav' -f $n)), $fmt)
      $tts.Speak($text)
    }}
  }}
}}
$tts.Dispose()
"""


def load(path):
    with wave.open(str(path)) as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
            raise ValueError(f"{path.name}: expected 16 kHz mono")
        return np.concatenate([PAD, np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), PAD])


def make_other_voices():
    OTHER_VOICES_DIR.mkdir(parents=True, exist_ok=True)
    for old in OTHER_VOICES_DIR.glob("*.wav"):
        old.unlink()
    subprocess.run(["powershell", "-NoProfile", "-Command", TTS_SCRIPT.format(out=OTHER_VOICES_DIR, phrase=PHRASE)],
                   check=True, capture_output=True)
    return sorted(OTHER_VOICES_DIR.glob("*.wav"))


def features(oww, clips, positive):
    if positive:
        feats = [get_reference_clip_features(c, oww, NAME, threshold=0.5, N=5) for c in clips]
    else:
        feats = [get_reference_clip_features(c, oww, NAME, threshold=0.0, N=1) for c in clips]
    return np.vstack(feats)


def train(oww, pos, neg):
    p, n = features(oww, pos, True), features(oww, neg, False)
    if len(p) == 0:
        sys.exit(f"None of your '{PHRASE}' takes were recognised by the wake-word model, so there's nothing "
                 f"to learn your voice from. Re-record (louder/closer) or check the model ({WAKE_MODEL}).")
    return train_verifier_model(np.vstack((p, n)), np.array([1] * len(p) + [0] * len(n)))


def best_score(model, audio):
    model.reset()
    return max(model.predict(audio[i:i + FRAME_SIZE])[NAME] for i in range(0, len(audio) - FRAME_SIZE, FRAME_SIZE))


def main():
    positives = sorted((SAMPLES_DIR / "positive").glob("*.wav"))
    own_negatives = sorted((SAMPLES_DIR / "negative").glob("*.wav"))
    if len(positives) < 8 or len(own_negatives) < 5:
        sys.exit(f"Not enough samples in {SAMPLES_DIR} yet. Run: python record_samples.py --word {NAME}")
    # Your recordings for your other wake words: "you, but not this word"
    for other in sorted(d for d in ALL_SAMPLES_DIR.iterdir() if d.is_dir() and d.name != NAME):
        extra = sorted(other.glob("positive/*.wav")) + sorted(other.glob("negative/*.wav"))
        if extra:
            print(f"  + {len(extra)} of your recordings for '{other.name}' as 'not {PHRASE}'")
            own_negatives += extra

    print(f"Generating '{PHRASE}' in other voices (Windows text-to-speech)...")
    others = make_other_voices()
    print(f"  {len(others)} clips")

    pos = [load(p) for p in positives]
    own_neg = [load(p) for p in own_negatives]
    oth = [load(p) for p in others]
    oww = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")

    base_hits = sum(best_score(oww, a) >= 0.5 for a in pos)
    print(f"Base wake-word model hears '{PHRASE}' in {base_hits}/{len(pos)} of your takes")

    # ---- held-out test: train without some of your takes and half the other voices ----
    random.seed(0)
    order = random.sample(range(len(pos)), len(pos))
    test_pos = [pos[i] for i in order[:HOLDOUT]]
    train_pos = [pos[i] for i in order[HOLDOUT:]]
    random.shuffle(oth)
    test_oth, train_oth = oth[: len(oth) // 2], oth[len(oth) // 2:]
    many = sorted(MANY_VOICES_DIR.rglob("*.wav"))     # rglob: also when copied as <dir>/positive_test/
    if MANY_VOICES_DIR.exists() and not many:
        print(f"  (no .wav files found in {MANY_VOICES_DIR})")
    if many:
        many = [load(p) for p in random.sample(many, min(len(many), MANY_VOICES_TRAIN + MANY_VOICES_TEST))]
        print(f"  + {len(many)} clips in many other voices from {MANY_VOICES_DIR.name}/")
        test_oth += many[:MANY_VOICES_TEST]
        train_oth += many[MANY_VOICES_TEST:]
        oth += many[MANY_VOICES_TEST:]       # the final check trains on these too

    clf = train(oww, train_pos, own_neg + train_oth)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_pkl = Path(tmp) / "verifier.pkl"
        pickle.dump(clf, open(tmp_pkl, "wb"))
        checked = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx",
                        custom_verifier_models={NAME: str(tmp_pkl)}, custom_verifier_threshold=VERIFIER_GATE)
        you = [best_score(checked, a) for a in test_pos]
        you_base = [best_score(oww, a) for a in test_pos]
        them = [best_score(checked, a) for a in test_oth]
        them_base = [best_score(oww, a) for a in test_oth]

    print("\nHeld-out test (clips the check never saw):")
    print(f"  Your '{PHRASE}' accepted:        {sum(s >= WAKE_THRESHOLD for s in you)}/{len(you)}"
          f"   (the wake-word model alone fired on {sum(s >= 0.5 for s in you_base)}/{len(you)})"
          f"   scores {', '.join(f'{s:.2f}' for s in you)}")
    print(f"  Other voices' '{PHRASE}' accepted: {sum(s >= WAKE_THRESHOLD for s in them)}/{len(them)}"
          f"   (without the check: {sum(s >= 0.5 for s in them_base)}/{len(them)})")

    # ---- final model on everything ----
    clf = train(oww, pos, own_neg + oth)
    pickle.dump(clf, open(VERIFIER_PATH, "wb"))
    print(f"\nSaved {VERIFIER_PATH}")


if __name__ == "__main__":
    main()
