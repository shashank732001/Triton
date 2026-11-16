"""
When has a spoken command ended? Silero VAD (the copy openWakeWord ships, ~2 ms per 80 ms frame) instead of a
fixed listening window.

The old way listened for a fixed 6 s (Vosk's own pause guess could end it sooner). Real commands
("open Netflix and select Shashank") were still going at 6 s and got cut: the name came out as "session".

    ep = Endpointer(start_window=6)
    for frame in mic:                       # 80 ms int16 frames at 16 kHz
        done = ep.push(frame, text_so_far)   # None while listening, else "end" / "no speech" / "too long"
    start, end = ep.speech_span()            # sample range worth sending to Whisper (None: nobody spoke)

Rules:
- Speech has to start within start_window seconds, or it's "no speech" (silence never reaches Whisper, so no
  made-up "Thank you." / "[BLANK").
- It ends after END_SILENCE_SEC of no speech, or DANGLING_SILENCE_SEC when the words so far stop on
  "open / to / the / search / select…", because a pause there is mid-sentence.
- The first WAKE_TAIL_SEC is ignored: in one-breath mode the recording starts right after "Hey Triton" is spotted,
  so the end of "Triton" can be in it, and a pause after the wake word mustn't end the command.
- Never longer than max_sec (a TV talking would otherwise keep it open forever).
"""

import numpy as np
from openwakeword.vad import VAD

RATE = 16000
FRAME = 1280                 # 80 ms, what the assistant reads from the mic
SPEECH_PROB = 0.5            # Silero's score for "someone started speaking"
KEEP_PROB = 0.3              # ...and for "still speaking" once started (Silero's own advice: start - 0.15 or so).
                             # Soft endings ("...select Shashank", said across the room) score 0.2-0.5
# The Rapoo mic across the room peaks around 3,000 of 32,767, and Silero scores quiet speech low: the VAD hears a
# louder copy (same idea as transcriber.louder), at most MAX_GAIN times, so room noise isn't blown up
TARGET_PEAK = 8000
MAX_GAIN = 8.0
# Even boosted, a soft last word across the room can score as silence ("...select Shisha" did). Two more signs that
# you're still talking keep listening open: the level is clearly above the room's background, or the word
# recogniser (Vosk) is still producing new words
LOUDER_THAN_ROOM = 2.5
MIN_RMS = 60
END_SILENCE_SEC = 0.8        # pauses inside a sentence measured on real clips: up to ~0.4 s
DANGLING_SILENCE_SEC = 1.8   # "open ...", "set the volume to ...": thinking of the rest
WAKE_TAIL_SEC = 0.4
MAX_COMMAND_SEC = 12.0
PAD_BEFORE_SEC = 1.0         # audio kept before the speech for Whisper: generous, because a soft first word ("That's
                             # all") can score low, and cutting it costs far more than a little silence
PAD_AFTER_SEC = 0.8
SHORT_SPEECH_SEC = 0.8       # so far only a short burst ("Hey Triton", "uh"): wait longer before calling it the end
SHORT_SILENCE_SEC = 1.3

# A sentence ending in one of these isn't finished ("open ... youtube", "set the volume to ... 30"). Not "up" or
# "stop": "turn it up" and a bare "stop" are whole commands and shouldn't wait.
DANGLING = {"open", "close", "to", "the", "a", "an", "and", "for", "search", "play", "switch", "show", "set", "turn",
            "increase", "decrease", "my", "on", "of", "with", "in", "into", "at", "type", "write", "click", "select",
            "choose", "pick", "go", "put", "start", "launch", "share", "about", "is", "what's", "whats", "can", "could",
            "please", "find", "watch", "then", "make", "volume"}


class Endpointer:
    def __init__(self, start_window=6.0, max_sec=MAX_COMMAND_SEC, skip_sec=WAKE_TAIL_SEC, only_wake=None):
        # only_wake(text): True when the words so far are just the wake phrase ("hey triton", "hair triter"): then a
        # pause is the gap before the command, not the end of it
        self.only_wake = only_wake or (lambda text: False)
        self.vad = VAD()
        self.start_window, self.max_sec, self.skip = start_window, max_sec, skip_sec
        self.n = 0                   # frames pushed
        self.first = None            # first and last speech frame
        self.last = None
        self.reason = None
        self.peak = 1000.0           # loudest frame so far (from 1,000, so the first quiet frames aren't boosted 8x)
        self.room = None             # background level (RMS) while nobody speaks
        self.text = ""               # the word recogniser's text so far

    @property
    def elapsed(self):
        return self.n * FRAME / RATE

    @property
    def started(self):
        return self.first is not None

    def push(self, frame, text_so_far=""):
        """One 80 ms frame in; None while the command goes on, else why it's over."""
        if self.reason:
            return self.reason
        prob, rms = 0.0, 0.0
        if len(frame) >= FRAME:
            x = frame[:FRAME].astype(np.float32)
            rms = float(np.sqrt(np.mean(x ** 2)))
            self.peak = max(self.peak, float(np.abs(x).max()))
            gain = min(MAX_GAIN, TARGET_PEAK / self.peak)
            # Raw and boosted both scored (Silero keeps state, so two copies): the boost catches soft far speech,
            # but on one real clip it hid a plain "Open Netflix" the raw audio scored fine
            if not hasattr(self, "vad_raw"):
                self.vad_raw = VAD()
            prob = max(float(self.vad.predict(np.clip(x * gain, -32768, 32767), frame_size=FRAME // 2)),
                       float(self.vad_raw.predict(x, frame_size=FRAME // 2)))
        self.n += 1
        if self.elapsed <= self.skip:
            return None
        if self.first is None:
            # Learn the room's level while waiting for speech (quickly down, slowly up)
            if prob < KEEP_PROB:
                self.room = rms if self.room is None else self.room + (rms - self.room) * (0.3 if rms < self.room else 0.05)
            if prob >= SPEECH_PROB:
                self.first = self.last = self.n - 1
        else:
            loud = rms > max(MIN_RMS, (self.room or 0) * LOUDER_THAN_ROOM)
            new_words = text_so_far.strip() != self.text
            if prob >= KEEP_PROB or loud or new_words:
                self.last = self.n - 1
        self.text = text_so_far.strip()
        if self.elapsed >= self.max_sec:
            self.reason = "too long"
        elif self.first is None:
            if self.elapsed >= self.start_window:
                self.reason = "no speech"
        else:
            words = text_so_far.lower().split()
            need = END_SILENCE_SEC
            if (self.last - self.first + 1) * FRAME / RATE < SHORT_SPEECH_SEC:
                need = SHORT_SILENCE_SEC
            if (words and words[-1] in DANGLING) or (words and self.only_wake(text_so_far)):
                need = DANGLING_SILENCE_SEC
            if (self.n - 1 - self.last) * FRAME / RATE >= need:
                self.reason = "end"
        return self.reason

    def speech_span(self):
        """(start, end) sample indices of the speech plus a little padding, or None if nobody spoke."""
        if self.first is None:
            return None
        start = max(0, self.first * FRAME - int(PAD_BEFORE_SEC * RATE))
        end = min(self.n * FRAME, (self.last + 1) * FRAME + int(PAD_AFTER_SEC * RATE))
        return start, end
