"""
Triton's better ears: open-vocabulary speech-to-text (Whisper / Moonshine via sherpa-onnx, int8, CPU).

Vosk still listens live: it's instant for the fixed command words the rules read, and it notices
when you stop talking. When the rules aren't sure, this re-reads the recorded command audio and
gives the local model a much cleaner sentence ("search for pasta recipes", "click subscribe").

  ears = Transcriber.from_settings(SETTINGS.get("ears", {}), BASE_DIR)    # None without a model
  text = ears.transcribe(audio_int16_at_16khz)                              # "" if not ready yet

Measured on the laptop (bench/stt_bench.py, 60 eval sentences, word error rate clean/noisy):
  vosk 13.3/21.1%   moonshine-tiny 9.2/15.1% 0.13 s   whisper-base 2.3/3.7% 0.6 s 415 MB (default)
Models: models/stt/<folder> or %USERPROFILE%/TritonModels/stt/<folder>, from
    https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
settings.json: {"ears": {"model": "whisper-base", "threads": 4, "enabled": true}}
"""

import logging
import re
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("assistant")

RATE = 16000
DEFAULT_MODEL = "whisper-base"
ENGINES = {
    "moonshine-tiny": ("moonshine", "sherpa-onnx-moonshine-tiny-en-int8"),
    "moonshine-base": ("moonshine", "sherpa-onnx-moonshine-base-en-int8"),
    "whisper-tiny": ("whisper", "sherpa-onnx-whisper-tiny.en"),
    "whisper-base": ("whisper", "sherpa-onnx-whisper-base.en"),
    "whisper-small": ("whisper", "sherpa-onnx-whisper-small.en"),
}
SPEECH_RMS = 100     # some 80 ms stretch must be at least this loud, or nobody spoke (Whisper would
                     # invent "Thank you." from silence)


def find_model(name, base_dir):
    folder = ENGINES[name][1]
    for d in (base_dir / "models" / "stt" / folder, Path.home() / "TritonModels" / "stt" / folder):
        if d.exists():
            return d
    return None


def load_recognizer(name, folder, threads=4):
    """A sherpa-onnx OfflineRecognizer for one of ENGINES, from its model folder."""
    import sherpa_onnx
    d = Path(folder)
    if ENGINES[name][0] == "moonshine":
        return sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(d / "preprocess.onnx"), encoder=str(d / "encode.int8.onnx"),
            uncached_decoder=str(d / "uncached_decode.int8.onnx"),
            cached_decoder=str(d / "cached_decode.int8.onnx"), tokens=str(d / "tokens.txt"),
            num_threads=threads)
    size = ENGINES[name][1].split("-")[-1]                # "base.en"
    return sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(d / f"{size}-encoder.int8.onnx"), decoder=str(d / f"{size}-decoder.int8.onnx"),
        tokens=str(d / f"{size}-tokens.txt"), num_threads=threads)


def recognise(rec, audio):
    stream = rec.create_stream()
    stream.accept_waveform(RATE, audio.astype(np.float32) / 32768)
    rec.decode_stream(stream)
    return stream.result.text.strip()


def tidy(text):
    """Plain words for the model: no trailing full stop, and no doubled sentence.

    On short clips Whisper sometimes says it twice ("Press Enter. Press Enter.")."""
    text = re.sub(r"[\"“”]", "", text)              # it sometimes quotes a query: search for "cricut highlights"
    text = re.sub(r"[(\[][^)\]]*[)\]]", " ", text)   # sounds it describes: "(buzzer)", "[music]"
    words = re.sub(r"[.!?,]+(\s|$)", r"\1", text).split()
    half = len(words) // 2
    if half and len(words) % 2 == 0 and [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
        words = words[:half]
    return " ".join(words)


def trim_silence(audio, frame=320, before=0.25, after=0.35):
    """Cut the quiet before and after the speech (keeping a little): a short command in seconds of
    near-silence is where Whisper garbles or invents words."""
    n = len(audio) // frame * frame
    if not n:
        return audio
    rms = np.sqrt((audio[:n].astype(np.float32).reshape(-1, frame) ** 2).mean(axis=1))
    loud = np.flatnonzero(rms >= max(SPEECH_RMS * 0.5, rms.max() * 0.08))
    if not len(loud):
        return audio
    start = max(0, loud[0] * frame - int(before * RATE))
    end = min(len(audio), (loud[-1] + 1) * frame + int(after * RATE))
    return audio[start:end]


def louder(audio, target=8000, most=20):
    """Raise a quiet recording so its loudest speech peaks near `target` (at most `most` times). The N95's Rapoo
    mic across the room peaks around 3,000 of 32,767; louder, whisper-small heard 6 more of 30 real commands
    right ("stops are in my laptop here" -> "stop showing my laptop here", "open crawl" -> "open chrome")."""
    peak = float(np.percentile(np.abs(audio.astype(np.int32)), 99.9)) if len(audio) else 0
    if peak < 1:
        return audio
    return np.clip(audio.astype(np.float32) * min(target / peak, most), -32767, 32767).astype(np.int16)


def loudest(audio, frame=1280):
    n = len(audio) // frame * frame
    if not n:
        return 0.0
    frames = audio[:n].astype(np.float32).reshape(-1, frame)
    return float(np.sqrt((frames ** 2).mean(axis=1)).max())


STREAMING = {   # Moonshine streaming (moonshine-voice): hears while you talk, so the text is ready when you stop
    "moonshine-tiny-streaming": ("TINY_STREAMING", "tiny-streaming-en"),
    "moonshine-small-streaming": ("SMALL_STREAMING", "small-streaming-en"),
    "moonshine-medium-streaming": ("MEDIUM_STREAMING", "medium-streaming-en"),
}


def find_streaming_model(name, base_dir):
    """models/stt/<name> (the N95 bundle), %USERPROFILE%/TritonModels/stt/<name>, else moonshine-voice's download
    cache (it fetches the model on first use on the laptop)."""
    for d in (base_dir / "models" / "stt" / name, Path.home() / "TritonModels" / "stt" / name):
        if (d / "streaming_config.json").exists():
            return d
    try:
        import moonshine_voice as mv
        return Path(mv.get_model_for_language("en", mv.ModelArch[STREAMING[name][0]])[0])
    except Exception as e:
        log.warning("No %s model (%s)", name, e)
        return None


class StreamingEars:
    """Moonshine streaming: begin() when listening starts, feed(frame) for every 80 ms of mic audio (a worker thread
    does the work, so the listening loop never waits), finish() when you've stopped: the text, ~0.03 s later
    (55 real clips on the laptop, 2026-11-05; whisper-small needed 2.0 s p50, 3.7 s p90 after you stopped)."""

    streaming = True

    def __init__(self, name, folder):
        import queue
        self.name, self.folder = name, folder
        self.ready = threading.Event()
        self._t = self._stream = None
        self._q = queue.Queue()
        self._lock = threading.Lock()

        def load():
            try:
                import moonshine_voice as mv
                from moonshine_voice.transcriber import Transcriber as MT
                self._t = MT(str(folder), mv.ModelArch[STREAMING[name][0]], update_interval=0.3)
                self._t.transcribe_without_streaming([0.0] * RATE)          # first run is slower
                log.info("Ears loaded: %s (streaming)", name)
            except Exception:
                log.exception("Couldn't load %s", name)
                self._t = None
            self.ready.set()

        threading.Thread(target=load, name="ears", daemon=True).start()
        threading.Thread(target=self._work, name="ears-stream", daemon=True).start()

    def _work(self):
        while True:
            item = self._q.get()
            try:
                if isinstance(item, threading.Event):    # finish(): everything before it has been fed
                    item.set()
                elif self._stream is not None:
                    self._stream.add_audio(item, RATE)
            except Exception:
                log.exception("Streaming ears failed on a frame")

    def begin(self):
        if self._t is None:
            return
        with self._lock:
            self._stream = self._t.create_stream(update_interval=0.3)
            self._stream.start()

    def feed(self, frame):
        if self._stream is not None:
            self._q.put((frame.astype(np.float32) / 32768).tolist())

    def finish(self, timeout=6):
        """The whole text heard since begin(); "" if not loaded or nothing was said."""
        if self._stream is None:
            return ""
        done = threading.Event()
        self._q.put(done)
        if not done.wait(timeout):
            log.warning("Streaming ears fell behind by more than %d s", timeout)
        with self._lock:
            stream, self._stream = self._stream, None
            try:
                result = stream.stop()
                text = " ".join(l.text.strip() for l in (result.lines if result else []) if l.text.strip())
            except Exception:
                log.exception("Streaming ears couldn't finish")
                text = ""
            try:
                stream.close()
            except Exception:
                pass
        return tidy(text)

    def transcribe(self, audio):
        """A whole recording at once (for replays and tests)."""
        if self._t is None or len(audio) < RATE // 4 or loudest(audio) < SPEECH_RMS:
            return ""
        with self._lock:
            r = self._t.transcribe_without_streaming((audio.astype(np.float32) / 32768).tolist())
        return tidy(" ".join(l.text.strip() for l in r.lines if l.text.strip()))


class Transcriber:
    def __init__(self, name, folder, threads=4):
        self.name = name
        self.ready = threading.Event()
        self._rec = None
        self._lock = threading.Lock()

        def load():
            try:
                self._rec = load_recognizer(name, folder, threads)
                recognise(self._rec, np.zeros(RATE, dtype=np.int16))      # first run is slower
                log.info("Ears loaded: %s", name)
            except Exception:
                log.exception("Couldn't load speech-to-text model %s; using Vosk's text", name)
            self.ready.set()

        threading.Thread(target=load, name="ears", daemon=True).start()

    @classmethod
    def from_settings(cls, cfg, base_dir):
        if not cfg.get("enabled", True):
            return None
        name = cfg.get("model", DEFAULT_MODEL)
        if name in STREAMING:
            folder = find_streaming_model(name, base_dir)
            return StreamingEars(name, folder) if folder else None
        folder = find_model(name, base_dir) if name in ENGINES else None
        if folder is None:
            log.info("No speech-to-text model '%s' found; the model reads Vosk's text", name)
            return None
        return cls(name, folder, cfg.get("threads", 4))

    def transcribe(self, audio):
        """What was said in audio (int16, 16 kHz); "" if not loaded yet or nobody spoke."""
        if self._rec is None or len(audio) < RATE // 4 or loudest(audio) < SPEECH_RMS:
            return ""
        with self._lock:
            return tidy(recognise(self._rec, louder(trim_silence(audio))))
