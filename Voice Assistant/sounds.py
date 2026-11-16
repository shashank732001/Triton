"""
The assistant's sounds: soft bell-like chimes (instead of the old winsound beeps), synthesised on
start-up into sounds/ next to the assistant, so there are no audio files to ship or lose.

  wake()    rising two-note chime: "I'm listening"
  ok()      bright quick sparkle: "got it"
  fail()    soft falling two notes: "didn't catch that"

HDMI/TV audio goes to sleep after a few seconds of silence and swallows the first ~0.5-1 s of the
next sound - the whole wake chime. keep_awake() plays an inaudible loop (faint dither noise, since
some drivers treat pure digital silence as "nothing playing") from the moment someone starts
speaking, so the output is awake by the time "Hey Triton" has been said.
"""

import logging
import threading
import time
import wave
import winsound

import numpy as np

log = logging.getLogger("assistant")

RATE = 44100
PEAK = 0.32          # overall volume (fraction of full scale): pleasant, not startling

# name: (length in s, [(note Hz, start s, decay s, loudness), ...])
SOUNDS = {
    "wake": (0.55, [(880.0, 0.00, 0.14, 0.55), (1318.5, 0.08, 0.20, 0.50)]),                       # A5 -> E6
    "ok":   (0.45, [(1318.5, 0.00, 0.10, 0.38), (1760.0, 0.06, 0.14, 0.34), (2637.0, 0.12, 0.12, 0.14)]),  # E6 A6 E7
    "fail": (0.60, [(659.3, 0.00, 0.14, 0.42), (493.9, 0.13, 0.20, 0.40)]),                        # E5 -> B4
}

KEEP_AWAKE_SEC = 15  # keep the audio output awake this long after the last sign of speech

_paths = {}
_lengths = {}
_lock = threading.Lock()       # PlaySound plays one sound per process: chimes and the keep-alive loop take turns
_awake_until = 0.0
_busy_until = 0.0              # a chime is playing; don't restart the loop over it
_looping = False
_keeper = None


def _bell(freq, decay, length):
    """One soft bell note: a sine plus a couple of quieter overtones, quick attack, smooth decay."""
    t = np.arange(int(length * RATE)) / RATE
    tone = (np.sin(2 * np.pi * freq * t)
            + 0.28 * np.sin(2 * np.pi * 2 * freq * t) * np.exp(-t / (decay * 0.5))
            + 0.08 * np.sin(2 * np.pi * 3.01 * freq * t) * np.exp(-t / (decay * 0.3)))
    attack = np.minimum(1.0, t / 0.006)               # 6 ms: no click, still crisp
    return tone * attack * np.exp(-t / decay)


def _render(length, notes):
    out = np.zeros(int(length * RATE))
    for freq, start, decay, gain in notes:
        i = int(start * RATE)
        out[i:] += _bell(freq, decay, length - start)[:len(out) - i] * gain
    out *= PEAK / np.abs(out).max()
    fade = int(length * 0.3 * RATE)                   # ease the tail into silence
    out[-fade:] *= np.linspace(1, 0, fade) ** 2
    return (out * 32767).astype(np.int16)


def prepare(base_dir):
    """Write the sounds to base_dir/sounds (a few KB, takes milliseconds)."""
    sound_dir = base_dir / "sounds"
    sound_dir.mkdir(exist_ok=True)
    rendered = {name: _render(length, notes) for name, (length, notes) in SOUNDS.items()}
    # 1 s of +-2 LSB noise (about -84 dB): inaudible, but real audio as far as the driver is concerned
    rendered["keepawake"] = np.random.default_rng(1).integers(-2, 3, RATE).astype(np.int16)
    for name, samples in rendered.items():
        path = sound_dir / f"{name}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(samples.tobytes())
        _paths[name] = str(path)
        _lengths[name] = len(samples) / RATE


def keep_awake(seconds=KEEP_AWAKE_SEC):
    """Someone is speaking: make sure the audio output is (or wakes up) ready for a chime."""
    global _awake_until, _keeper
    _awake_until = max(_awake_until, time.monotonic() + seconds)
    if _keeper is None and _paths:
        _keeper = threading.Thread(target=_keep_awake_loop, name="audio-keepalive", daemon=True)
        _keeper.start()


def _keep_awake_loop():
    global _looping
    flags = winsound.SND_FILENAME | winsound.SND_NODEFAULT | winsound.SND_ASYNC | winsound.SND_LOOP
    while True:
        time.sleep(0.1)
        with _lock:
            now = time.monotonic()
            try:
                if now < _awake_until and now >= _busy_until and not _looping:
                    winsound.PlaySound(_paths["keepawake"], flags)
                    _looping = True
                elif now >= _awake_until and _looping:
                    winsound.PlaySound(None, 0)
                    _looping = False
            except Exception:
                log.exception("Audio keep-alive failed")
                _looping = False


def _play(name, wait):
    global _looping, _busy_until
    flags = winsound.SND_FILENAME | winsound.SND_NODEFAULT | (0 if wait else winsound.SND_ASYNC)
    with _lock:
        _looping = False           # playing a sound replaces the keep-alive loop; the keeper restarts it after
        _busy_until = time.monotonic() + _lengths[name]
        try:
            winsound.PlaySound(_paths[name], flags)
        except Exception:
            log.exception("Couldn't play the %s sound", name)


def wake():
    # Not waited for: a blocking PlaySound adds ~0.35 s of device open/close on top of the chime
    _play("wake", wait=False)


def ok():
    _play("ok", wait=False)


def fail():
    _play("fail", wait=False)
