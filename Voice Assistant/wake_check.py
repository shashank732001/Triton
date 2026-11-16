"""
Checks on a wake-up, and finding "Hey Triton" in what the ears wrote.

False wake-ups: the wake-word model woke Triton 45 times out of 78 with nothing said after it (assistant.log), and
those scored as high as real ones (0.53-1.00 against 0.51-0.99), so a stricter threshold can't separate them.
Max confirms a wake-up with a second opinion on the words; for "Triton" that doesn't work on your voice
(bench/wake_confirm_bench.py, 2026-11-16: moonshine-tiny confirmed 25 of the 274 recorded takes that woke the model,
writing "Hate item", "He tried"; Max's acoustic keyword spotter caught at most 11 of 287). What does separate them:

- speech_check(): a real "Hey Triton" always has speech in it. 268 of 269 of your recorded takes that woke the model
  had at least 0.40 s that the speech detector (Silero) called speech in the 2 s before the hit; a click, a door,
  music or a TV sound effect has little or none. MIN_SPEECH_SEC is the bar.
- In the assistant: a wake-up with nothing said after it ends silently (no "say that again", no follow-up
  listening), and while Triton itself is talking (and just after) the wake word needs the stricter media threshold.
"""

import re
from difflib import SequenceMatcher

FRAME_SEC = 0.08
SPEECH_WINDOW_SEC = 2.0      # the wake phrase ends where the model fires; "Hey Triton" takes ~0.8-1.2 s
SPEECH_PROB = 0.5
MIN_SPEECH_SEC = 0.24        # 3 frames; real takes: min 0.40 (1 of 269 lower), median 0.72


def speech_check(probs):
    """(ok, seconds of speech) for the voice detector's per-frame scores of the audio up to the wake hit."""
    window = list(probs)[-int(round(SPEECH_WINDOW_SEC / FRAME_SEC)):]
    seconds = sum(p >= SPEECH_PROB for p in window) * FRAME_SEC
    return seconds >= MIN_SPEECH_SEC - 1e-9, seconds


_HEY = {"hey", "hay", "hi", "hair", "head", "hit", "here", "he", "hate", "heat", "eh", "a", "hello", "okay", "ok"}


def _letters(word):
    return re.sub(r"[^a-z]", "", word.lower())


def tritonish(words):
    """Do these 1-2 words sound like "triton"? ("triton", "tritan", "tri ton", "triter")."""
    s = "".join(_letters(w) for w in words)
    return len(s) >= 4 and SequenceMatcher(None, s, "triton").ratio() >= 0.6


def strip_wake(text):
    """The command without "Hey Triton" (or how the ears misheard it: "hair triter", "hey tritan") at its start: said
    in one breath, or said again after the reply. "" when that was all there was."""
    w = text.split()
    for i in range(min(3, len(w))):                  # "um hey triton", "hey hey triton"
        if _letters(w[i]) in _HEY:
            for k in (1, 2):
                if i + 1 + k <= len(w) and tritonish(w[i + 1:i + 1 + k]):
                    return " ".join(w[i + 1 + k:]).lstrip(",. ")
    if w and len(w[0]) > 4 and tritonish(w[:1]):     # "triton, open youtube"
        return " ".join(w[1:]).lstrip(",. ")
    return text
