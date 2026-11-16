r"""
Replays your recorded commands (voice_samples/commands) through the real listening code, without a mic.

1. Response: how long after you stop talking does Triton know the sentence is over? Old endpoint settings
   (0.8 / 1.3 / 1.8 s, no "complete" shortcut) against the current ones, on the same clips. Counted in audio, so it's
   the same on the N95.
2. Continuing after a pause: the recorded sentences whose mid-sentence pause (0.55-0.8 s) the shorter end-of-sentence
   time takes for the end, fed in real time: the decision on the first part must be dropped and the whole sentence
   understood.
3. Nothing lost: "Open Netflix ... select the first profile here": the first part is acted on at once (rules), and the
   second is still in the mic for the follow-up, not thrown away.

    .venv\Scripts\python.exe bench\listen_replay.py
"""

import logging
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import assistant as A  # noqa: E402
import endpoint  # noqa: E402
from apps import Catalog  # noqa: E402
from audio_hub import AudioHub  # noqa: E402
from brain.router import Router  # noqa: E402
from commands import Commands  # noqa: E402
from vosk import Model as VoskModel, SetLogLevel  # noqa: E402

FRAME = A.FRAME_SIZE
CLIPS = BASE / "voice_samples" / "commands"


def read(name):
    with wave.open(str(CLIPS / f"{name}.wav")) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def frames_of(audio):
    n = len(audio) // FRAME * FRAME
    return [audio[i:i + FRAME] for i in range(0, n, FRAME)]


ROOM = None


def silence(sec):
    """The room as your mic hears it (the first 0.5 s of a clip, before you spoke), repeated: digital quiet would make
    the clips' own background hiss count as "louder than the room", so pauses would never end."""
    global ROOM
    if ROOM is None:
        ROOM = np.concatenate([read(t.stem)[:8000] for t in sorted(CLIPS.glob("*.txt"))[:8]])
    n = int(sec * 16000)
    return np.resize(ROOM, n)


class _Quiet:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _NoLaptop:                                      # makes the free recogniser run, as with a model; never reached
    mode, available = "fallback", False

    def __getattr__(self, name):
        return lambda *a, **k: None


def make_assistant():
    SetLogLevel(-1)
    me = A.Assistant.__new__(A.Assistant)
    catalog = Catalog(A.SHORTCUTS_FILE)
    catalog.ready.wait(60)
    me.commands = Commands(catalog, A.monitor)
    me.router = Router(me.commands, catalog, None, backup=False)
    me.vosk = VoskModel(str(A.VOSK_MODEL_DIR))
    me.vocabulary = None
    me.llm, me.remote, me.max, me.ears, me.ears_big, me.speaker = None, _NoLaptop(), None, None, None, None
    me.overlay = _Quiet()
    me.stopped = threading.Event()
    me.only_wake, me.pending_click, me.sharing, me.media_playing = False, None, False, False
    me._tl, me._hold = threading.local(), None
    me.audio = AudioHub()
    A.screen.look = lambda: A.screen.Screen("", [])
    A.SAVE_COMMAND_CLIPS = False
    return me


def response(me, settings):
    """Seconds from the end of speech to the end of listening, per clip, with these endpoint settings."""
    for k, v in settings.items():
        setattr(endpoint, k, v)
    out = []
    for txt in sorted(CLIPS.glob("*.txt")):
        said = txt.read_text(encoding="utf-8").splitlines()[0].replace("whisper:", "").strip()
        if not said or said.startswith("["):
            continue
        audio = read(txt.stem)
        me.audio = AudioHub(keep_sec=60)
        me._tl = threading.local()
        r = me._reader()
        # The clips keep 0.8 s after the speech (endpoint.PAD_AFTER_SEC): speech ends there
        speech_end = (len(audio) - int(0.8 * 16000)) // FRAME
        for f in frames_of(np.concatenate([audio, silence(4)])):
            me.audio.push(f)
        heard, free = me._listen_for_command(window=6, skip=0)
        out.append(((r.position - speech_end) * FRAME / 16000, said, free or heard))
    return out


def feed_live(me, parts):
    """Push audio in real time from a thread (as the mic would)."""
    def run():
        for f in frames_of(np.concatenate(parts)):
            me.audio.push(f)
            time.sleep(FRAME / 16000)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def main():
    logging.basicConfig(level=logging.WARNING)
    A.log.setLevel(logging.INFO)
    A.log.addHandler(logging.StreamHandler(sys.stdout))
    A.log.propagate = False
    me = make_assistant()

    if "--skip-response" not in sys.argv:
        print("1. Response after you stop talking (audio seconds)")
        old = {"END_SILENCE_SEC": 0.8, "SHORT_SILENCE_SEC": 1.3, "DANGLING_SILENCE_SEC": 1.8,
               "COMPLETE_SILENCE_SEC": 0.8}
        new = {k: getattr(endpoint, k) for k in old}
        A.log.disabled = True
        before, after = response(me, old), response(me, new)
        A.log.disabled = False
        for (b, said, was), (a, _, heard) in zip(before, after):
            print(f"   {b:5.2f} -> {a:5.2f}  {said[:40]:40s} heard {heard[:38]!r}"
                  + ("" if was == heard else f"  (was {was[:38]!r})"))
        b, a = np.array([x[0] for x in before]), np.array([x[0] for x in after])
        print(f"   median {np.median(b):.2f} -> {np.median(a):.2f} s, mean {b.mean():.2f} -> {a.mean():.2f} s,"
              f" same words {sum(x[2] == y[2] for x, y in zip(before, after))}/{len(a)}\n")

    print("2. Continuing after a pause: the sentences the shorter end-of-sentence time cuts, fed in real time")
    for name in ("20261104_121815", "20261103_115209"):
        me.audio = AudioHub(keep_sec=60)
        me._tl = threading.local()
        me._reader()
        feeder = feed_live(me, [silence(0.3), read(name), silence(3)])
        heard, free, decided = me._hear_and_decide(window=6, skip=0)
        feeder.join()
        print(f"   heard {free or heard!r}, decided {decided[2] if decided else None} by "
              f"{decided[4] if decided else None}\n")

    print("3. Nothing lost ('Open Netflix' ... 'Can you select the first profile here')")
    me.audio = AudioHub(keep_sec=60)
    me._tl = threading.local()
    me._reader()
    feeder = feed_live(me, [silence(0.3), read("20261103_115445"), silence(1.0), read("20261104_113500"), silence(3)])
    heard, free, decided = me._hear_and_decide(window=6, skip=0)
    print(f"   first: {free or heard!r} -> {decided[2] if decided else None} by {decided[4] if decided else None}")
    time.sleep(1.0)                                   # acting on it takes a moment; the mic keeps going
    heard2, free2 = me._listen_for_command(window=5, skip=0)
    feeder.join()
    print(f"   follow-up still heard afterwards: {free2 or heard2!r}")


if __name__ == "__main__":
    main()
