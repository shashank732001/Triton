"""
Your own wake-word recordings -> extra training data, so the model learns your voice and accent
and not only the computer voices. Used by train_hey_nova.py (step 13b).

Each take is trimmed to the spoken phrase, then copied many times with random changes that mimic
what the real mic hears: faster/slower speech, a different microphone (cheap, far, TV speaker
bandwidth), room echo, loudness from across the room to right next to it, and something in the
background (music, your own chatter, hiss or rumble), sometimes even clipping. Then it's turned into
the same 16x96 feature windows the model sees live.
Your other recordings (sound-alike phrases, ordinary speech) become "don't fire" examples, also in
varied copies, so "Hey Tristan" in a noisy room doesn't fire either.
"""

import numpy as np
import soundfile as sf
from scipy.signal import butter, fftconvolve, resample, sosfilt

SR = 16000
CLIP_SEC = 2.0               # 2.0 s of audio = exactly the 16 feature frames the model looks at
WINDOW = 16
FRAME = 1280                 # 80 ms: openWakeWord's unit of audio
END_JITTER_SEC = 0.25        # the window may stop up to this long after the phrase ends
QUIET_GAP_SEC = 0.4          # a pause this long ends the phrase when trimming (a "hey ... echo" pause is ~0.3 s)
PHRASE_SEC = (0.35, 1.5)     # trimmed takes outside this range are probably mis-cut and get skipped
                             # (far takes run a little long: the room's echo trails the phrase)


def load(path):
    audio, sr = sf.read(str(path), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SR:
        raise ValueError(f"{path}: {sr} Hz, expected {SR}")
    return audio


def trim(audio, margin_sec=0.05):
    """Cut a take down to the spoken phrase: the loud stretch with the most energy in it
    (not just the loudest moment, which can be a click or a bump of the mic)."""
    hop = SR // 100
    n = len(audio) // hop
    rms = np.sqrt((audio[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    loud = np.flatnonzero(rms > max(np.percentile(rms, 10) * 4, np.percentile(rms, 99) * 0.1, 1e-3))
    if len(loud) == 0:
        return audio
    # split the loud frames wherever there's a long enough pause
    segments = np.split(loud, np.flatnonzero(np.diff(loud) > QUIET_GAP_SEC * 100) + 1)
    seg = max(segments, key=lambda s: float((rms[s[0]: s[-1] + 1] ** 2).sum()))
    margin = int(margin_sec * SR)
    return audio[max(0, seg[0] * hop - margin): min(len(audio), (seg[-1] + 1) * hop + margin)]


def _excerpt(audio_or_path, n, rng):
    """n samples from a file or array, from a random spot (looped if it's shorter)."""
    if isinstance(audio_or_path, np.ndarray):
        a = audio_or_path
        if len(a) <= n:
            return np.resize(a, n)
        start = int(rng.integers(0, len(a) - n))
        return a[start: start + n]
    frames = sf.info(str(audio_or_path)).frames
    if frames <= n:
        return np.resize(load(audio_or_path), n)
    start = int(rng.integers(0, frames - n))
    audio, _ = sf.read(str(audio_or_path), start=start, frames=n, dtype="float32")
    return audio if audio.ndim == 1 else audio.mean(axis=1)


def _noise(n, rng):
    """Hiss (white) or rumble/fan (pink-ish)."""
    white = rng.standard_normal(n).astype(np.float32)
    if rng.random() < 0.5:
        return white
    pink = np.cumsum(white)
    pink -= np.convolve(pink, np.ones(200) / 200, mode="same")     # keep it from drifting
    return pink.astype(np.float32)


def place(speech, rng, jitter_sec=END_JITTER_SEC):
    """Put the phrase in a 2 s clip so it ends shortly before the clip does (like live audio)."""
    clip = np.zeros(int(CLIP_SEC * SR), np.float32)
    end = len(clip) - int(rng.uniform(0, jitter_sec) * SR)
    speech = speech[-end:]
    clip[end - len(speech): end] = speech
    return clip


def _colour(s, rng, rirs):
    """How the voice itself arrives: speed, the microphone, the room, the distance."""
    if rng.random() < 0.6:                                   # speak faster/slower (pitch moves with it)
        s = resample(s, int(len(s) * rng.uniform(0.85, 1.15))).astype(np.float32)
    if rng.random() < 0.5:                                   # a different mic: cheap, far, or TV-speaker bandwidth
        low, high = rng.uniform(60, 400), rng.uniform(2800, 7600)
        s = sosfilt(butter(2, [low, high], btype="band", fs=SR, output="sos"), s).astype(np.float32)
    if rirs and rng.random() < 0.75:                         # a room's echo: most of what a far mic hears
        peak = np.abs(s).max() + 1e-9
        s = fftconvolve(s, rirs[int(rng.integers(len(rirs)))])[: len(s)].astype(np.float32)
        s *= peak / (np.abs(s).max() + 1e-9)
    return s * 10 ** rng.uniform(-1.5, 0.4)                  # from 1/30 as loud (across the room) to 2.5x


def _mix(clip, speech_rms, rng, backgrounds, babble):
    """Something going on in the background, 0-30 dB below the voice (often: a TV room is rarely silent)."""
    r = rng.random()
    if backgrounds and r < 0.45:                             # music
        bg, snr = _excerpt(backgrounds[int(rng.integers(len(backgrounds)))], len(clip), rng), rng.uniform(0, 25)
    elif babble and r < 0.65:                                # your own chatter behind the phrase
        bg, snr = _excerpt(babble[int(rng.integers(len(babble)))], len(clip), rng), rng.uniform(5, 25)
    elif r < 0.85:                                           # hiss / fan / rumble
        bg, snr = _noise(len(clip), rng), rng.uniform(5, 30)
    else:
        return clip
    bg_rms = np.sqrt(np.mean(bg ** 2)) + 1e-9
    return clip + bg * (speech_rms / bg_rms) / 10 ** (snr / 20)


def augment(speech, rng, rirs, backgrounds, babble=()):
    s = _colour(speech, rng, rirs)
    clip = _mix(place(s, rng), np.sqrt(np.mean(s ** 2)) + 1e-9, rng, backgrounds, babble)
    if rng.random() < 0.05:                                  # a mic that clips when you're close and loud
        clip = clip * rng.uniform(2, 5)
    return np.clip(clip, -1, 1)


def _int16(clips):
    return (np.stack(clips) * 32767).astype(np.int16)


def positive_features(takes, copies, features, rirs=(), backgrounds=(), seed=0, babble=()):
    """Each trimmed take once as recorded plus `copies` augmented versions -> (N, 16, 96)."""
    rng = np.random.default_rng(seed)
    rirs, backgrounds, babble = list(rirs), list(backgrounds), list(babble)
    out = []
    for speech in takes:                     # one take at a time keeps memory flat with 100+ copies
        clips = [place(speech, rng, jitter_sec=0.1)]
        clips.extend(augment(speech, rng, rirs, backgrounds, babble) for _ in range(copies))
        out.append(features.embed_clips(_int16(clips), batch_size=64)[:, -WINDOW:, :])
    return np.concatenate(out).astype(np.float32)


def negative_features(recordings, features, stride=2, copies=0, rirs=(), backgrounds=(), seed=3):
    """Every 16-frame window of your other recordings, as recorded and in `copies` varied versions
    (other mic, room, background) -> (N, 16, 96)."""
    rng = np.random.default_rng(seed)
    rirs, backgrounds = list(rirs), list(backgrounds)
    windows = []
    for audio in recordings:
        versions = [audio]
        for _ in range(copies):
            s = _colour(audio, rng, rirs)
            versions.append(np.clip(_mix(s.copy(), np.sqrt(np.mean(s ** 2)) + 1e-9, rng, backgrounds, ()), -1, 1))
        for v in versions:
            # half a second of silence each side, then up to a whole number of 80 ms frames: openWakeWord's
            # melspectrogram miscounts frames for some lengths (sped-up copies hit it: 607 vs 608)
            v = np.concatenate([np.zeros(SR // 2, np.float32), v, np.zeros(SR // 2, np.float32)])
            v = np.pad(v, (0, -len(v) % FRAME))
            emb = features.embed_clips(_int16([v]), batch_size=1)[0]
            windows.extend(emb[i: i + WINDOW] for i in range(0, len(emb) - WINDOW + 1, stride))
    return np.stack(windows).astype(np.float32)
