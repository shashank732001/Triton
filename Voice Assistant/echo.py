"""
Echo cancellation: removes what this PC itself is playing (YouTube on the TV, Triton's own voice) from the mic, before
the wake word or the ears hear it. WebRTC's AEC3 (the one browsers use for calls), from the livekit package; the
reference is what the default speaker plays, captured with WASAPI loopback (soundcard).

    echo = EchoCanceller.start()      # None if it can't run (no livekit/soundcard, no speaker): mic audio stays as is
    clean = echo.process(block)       # int16, 16 kHz mono, any multiple of 10 ms (the mic's 80 ms frames)

Why: the TV's sound became commands ("We might actually be going super saiyan…" after a wake-up), so while
something plays the wake word needs 0.75 and follow-ups are off. With the echo removed the mic hears mostly you.
Settings: "echo_cancel": true (default) / false.
"""

import logging
import threading
import time

import numpy as np
from scipy.signal import firwin, lfilter, lfilter_zi

log = logging.getLogger("assistant")

RATE = 16000
CHUNK = RATE // 100               # AEC3 works on 10 ms frames
LOOP_RATE = 48000                 # what WASAPI loopback gives on every device we've seen
RECHECK_SEC = 10                  # the default speaker can change (HDMI TV on/off): look again this often


class _Decimate:
    """48 kHz -> 16 kHz with a low-pass first (filter state kept across blocks)."""

    def __init__(self, factor=3):
        self.factor = factor
        self.taps = firwin(63, 0.9 / factor)
        self.state = lfilter_zi(self.taps, 1.0) * 0

    def __call__(self, x):
        y, self.state = lfilter(self.taps, 1.0, x, zi=self.state)
        return y[::self.factor]


class EchoCanceller:
    def __init__(self, rtc, soundcard):
        self.rtc, self.sc = rtc, soundcard
        self.apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)
        self.stopped = threading.Event()
        self.playing = 0.0            # loudness of what the speaker plays (for the log/tests)
        self._pending = np.zeros(0, np.int16)
        threading.Thread(target=self._reference_loop, name="echo-ref", daemon=True).start()

    @classmethod
    def start(cls, enabled=True):
        if not enabled:
            return None
        try:
            from livekit import rtc
            import soundcard
            return cls(rtc, soundcard)
        except Exception as e:                      # missing package, no audio device: just no echo cancelling
            log.warning("Echo cancellation off (%s)", e)
            return None

    def _frame(self, samples):
        return self.rtc.AudioFrame(samples.astype(np.int16).tobytes(), RATE, 1, CHUNK)

    def _reference_loop(self):
        """What the speaker plays, 10 ms at a time, into the canceller's far end. Re-opens on device changes."""
        while not self.stopped.is_set():
            try:
                speaker = self.sc.default_speaker()
                loop = self.sc.get_microphone(speaker.name, include_loopback=True)
                log.info("Echo cancellation: reference is '%s'", speaker.name)
                down = _Decimate(LOOP_RATE // RATE)
                opened = time.monotonic()
                with loop.recorder(samplerate=LOOP_RATE, channels=1, blocksize=LOOP_RATE // 100) as rec:
                    buf = np.zeros(0, np.float32)
                    while not self.stopped.is_set():
                        data = rec.record(numframes=LOOP_RATE // 100)[:, 0]
                        buf = np.concatenate([buf, down(data.astype(np.float32))])
                        while len(buf) >= CHUNK:
                            chunk, buf = buf[:CHUNK], buf[CHUNK:]
                            pcm = np.clip(chunk * 32767, -32768, 32767)
                            self.playing = 0.9 * self.playing + 0.1 * float(np.abs(pcm).mean())
                            self.apm.process_reverse_stream(self._frame(pcm))
                        if time.monotonic() - opened > RECHECK_SEC:
                            if self.sc.default_speaker().name != speaker.name:
                                break                      # the default speaker changed: follow it
                            opened = time.monotonic()
            except Exception as e:
                log.warning("Echo reference stopped (%s); retrying", e)
                self.stopped.wait(2)

    def process(self, block):
        """The mic's audio with this PC's own sound taken out (same length; int16, 16 kHz)."""
        x = np.concatenate([self._pending, block.astype(np.int16)])
        n = len(x) // CHUNK * CHUNK
        out = []
        for i in range(0, n, CHUNK):
            f = self._frame(x[i:i + CHUNK])
            self.apm.process_stream(f)
            out.append(np.frombuffer(f.data, np.int16))
        self._pending = x[n:]
        y = np.concatenate(out) if out else np.zeros(0, np.int16)
        # Keep the length the caller gave (frames of 80 ms are whole 10 ms chunks, so this is just a guard)
        return np.concatenate([y, np.zeros(len(block) - len(y), np.int16)]) if len(y) < len(block) else y[:len(block)]

    def stop(self):
        self.stopped.set()
