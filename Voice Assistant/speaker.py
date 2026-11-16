"""
Triton's voice: says replies out loud with an offline neural voice, sentence by sentence.

  speaker = Speaker.from_settings(SETTINGS.get("voice", {}), BASE_DIR)    # None when there's no voice
  speaker.say("Volume 40%")              # queued, returns at once
  speaker.say("Lock the computer?", wait=True)   # returns once it's been said (before listening for "yes")
  speaker.stop()                         # hush, e.g. when the wake word is said again (barge-in)

Two kinds of voice (settings.json "voice"):
  {"name": "en_GB-alan-medium"}                          Piper (piper-tts): models/piper/<name>.onnx (+ .json)
  {"name": "kokoro-multi-lang-v1_0", "speaker": "bm_george"}
                                                         a sherpa-onnx voice folder: Kokoro, Kitten or a Piper
                                                         "high" voice (vits-piper-*), in models/tts/<name> or
                                                         %USERPROFILE%/TritonModels/tts/<name>, from
                                                         github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models
Also "speed" (1.0 = normal), "threads" (sherpa voices, default 2) and "enabled".
Loading takes a few seconds, so it happens in the background; until then say() returns False and the
caller plays the old chime instead. bench/tts_bench.py compares voices (speed + sample WAVs).
"""

import logging
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

import sounds

log = logging.getLogger("assistant")

DEFAULT_VOICE = "en_US-hfc_female-medium"   # clear, natural female voice; the quickest Piper voice to make
BLOCK = 1024                            # samples written at a time; stop() takes effect within ~50 ms
CACHE_MAX_CHARS = 60                    # phrases up to this long are kept once made ("Opening Netflix", "Sorry?")
CACHE_SIZE = 150


def find_voice(name, base_dir):
    """('piper', path) or ('sherpa', folder) for a voice name, or None if it isn't installed."""
    piper = base_dir / "models" / "piper" / f"{name}.onnx"
    if piper.exists():
        return "piper", piper
    for d in (base_dir / "models" / "tts" / name, Path.home() / "TritonModels" / "tts" / name):
        if d.is_dir():
            return "sherpa", d
    return None


def _sherpa_tts(folder, speaker, threads):
    """A sherpa-onnx OfflineTts for a Kokoro / Kitten / vits-piper folder, and the speaker id to use."""
    import sherpa_onnx
    d = Path(folder)
    model = next(d.glob("model*.onnx"), None) or next(d.glob("*.onnx"))
    espeak = str(d / "espeak-ng-data")
    names = []
    # sherpa-onnx 2.x won't fall back to espeak for these: it needs a lexicon. Kokoro v1.0 ships
    # lexicon-us-en.txt / lexicon-gb-en.txt; Kitten and Kokoro v0.19 work with copies of the same files.
    lexicon = d / ("lexicon-gb-en.txt" if speaker.startswith("b") else "lexicon-us-en.txt")
    lexicon = str(lexicon) if lexicon.exists() else ""
    if "kokoro" in d.name:
        cfg = sherpa_onnx.OfflineTtsModelConfig(kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=str(model), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"), data_dir=espeak,
            lexicon=lexicon), num_threads=threads)
    elif "kitten" in d.name:
        cfg = sherpa_onnx.OfflineTtsModelConfig(kitten=sherpa_onnx.OfflineTtsKittenModelConfig(
            model=str(model), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"), data_dir=espeak,
            lexicon=lexicon), num_threads=threads)
    else:
        cfg = sherpa_onnx.OfflineTtsModelConfig(vits=sherpa_onnx.OfflineTtsVitsModelConfig(
            model=str(model), tokens=str(d / "tokens.txt"), data_dir=espeak), num_threads=threads)
    if "kokoro" in d.name or "kitten" in d.name:
        import onnxruntime
        meta = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"]).get_modelmeta()
        names = meta.custom_metadata_map.get("speaker_names", "").split(",")
    tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=cfg))
    sid = names.index(speaker) if speaker in names else 0
    return tts, sid


ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(n):
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + number_words(n % 100))
    if n < 1_000_000:
        return number_words(n // 1000) + " thousand" + ("" if n % 1000 == 0 else " " + number_words(n % 1000))
    return " ".join(ONES[int(d)] for d in str(n))


def spoken(text):
    """Write numbers out for lexicon-based voices (Kokoro, Kitten), which silently skip words they don't
    know, digits included: "Volume 40%" would be said as "Volume". Piper's espeak does this itself."""
    def clock(m):
        h, mins = int(m.group(1)), int(m.group(2))
        return number_words(h) + (" o'clock" if mins == 0 else f" oh {ONES[mins]}" if mins < 10
                                  else " " + number_words(mins))
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", clock, text)
    t = re.sub(r"\b([AaPp])\.?[Mm]\.?\b", lambda m: m.group(1).upper() + " M", t)
    t = t.replace("%", " percent")
    return re.sub(r"\d+", lambda m: f" {number_words(int(m.group()))} ", t).replace("  ", " ").strip()


def load_voice(name, base_dir, speaker="", speed=1.0, threads=2):
    """A function text -> iterator of (sample_rate, int16 audio), one item per sentence. None if missing."""
    found = find_voice(name, base_dir)
    if found is None:
        return None
    kind, path = found
    if kind == "piper":
        from piper import PiperVoice
        from piper.config import SynthesisConfig
        voice = PiperVoice.load(str(path))
        config = SynthesisConfig(length_scale=1.0 / speed)        # length_scale > 1 is slower speech

        def say(text):
            for chunk in voice.synthesize(text, config):
                yield chunk.sample_rate, np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
        return say

    tts, sid = _sherpa_tts(path, speaker, threads)

    def say(text):
        # Sentence by sentence, so the first one plays while the rest is still being made
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
            if sentence:
                audio = tts.generate(spoken(sentence), sid=sid, speed=speed)
                if not len(audio.samples):                       # sherpa logs why and returns nothing
                    raise RuntimeError(f"voice {name} produced no audio for {sentence!r}")
                yield audio.sample_rate, (np.clip(np.asarray(audio.samples), -1, 1) * 32767).astype(np.int16)
    return say


class Speaker:
    def __init__(self, name, base_dir, speaker="", speed=1.0, threads=2):
        self.name, self.base_dir = name, base_dir
        self.speaker, self.speed, self.threads = speaker, speed, threads
        self.ready = threading.Event()
        self._voice = None
        self._jobs = queue.Queue()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._cache = {}             # short phrases already made (text -> audio), played without waiting
        self._out, self._out_rate = None, 0
        threading.Thread(target=self._run, name="speaker", daemon=True).start()

    @classmethod
    def from_settings(cls, cfg, base_dir):
        if not cfg.get("enabled", True):
            return None
        name = cfg.get("name", DEFAULT_VOICE)
        if find_voice(name, base_dir) is None:
            log.info("No voice called %s (models/piper or models/tts); replies are shown, not spoken", name)
            return None
        return cls(name, base_dir, cfg.get("speaker", ""), cfg.get("speed", 1.0), cfg.get("threads", 2))

    def say(self, text, wait=False, start_within=0):
        """Queue text to be spoken. False if the voice isn't loaded yet (or failed to load).
        wait: return once it's been said. start_within: return once it's audible (at most that many seconds),
        so "Opening Netflix" is heard as Netflix opens, not after."""
        if not text or not self.ready.is_set() or self._voice is None:
            return False
        self._stop.clear()
        self._idle.clear()
        done, started = threading.Event(), threading.Event()
        self._jobs.put((text, done, started))
        if wait:
            done.wait(timeout=30)
        elif start_within:
            started.wait(timeout=start_within)
        return True

    def prepare(self, phrases):
        """Make these short phrases now, in the background, so they play the moment they're needed."""
        def make():
            self.ready.wait()
            for p in phrases:
                if self._voice is not None and p not in self._cache and len(p) <= CACHE_MAX_CHARS:
                    try:
                        self._remember(p, list(self._voice(p)))
                    except Exception:
                        log.exception("Couldn't prepare %r", p)
        threading.Thread(target=make, name="speaker-prepare", daemon=True).start()

    def _remember(self, text, parts):
        if len(self._cache) >= CACHE_SIZE:
            self._cache.pop(next(iter(self._cache)))           # the oldest
        self._cache[text] = parts

    def stop(self):
        """Stop talking now and drop anything still queued."""
        self._stop.set()
        while True:
            try:
                _, done, started = self._jobs.get_nowait()
                done.set()
                started.set()
            except queue.Empty:
                break

    @property
    def speaking(self):
        return not self._idle.is_set()

    def wait(self, timeout=30):
        return self._idle.wait(timeout)

    def _run(self):
        try:
            self._voice = load_voice(self.name, self.base_dir, self.speaker, self.speed, self.threads)
            list(self._voice("Ready."))         # first run is slower; get it out of the way
            log.info("Voice loaded: %s %s", self.name, self.speaker)
        except Exception:
            self._voice = None
            log.exception("Couldn't load the voice %s; replies won't be spoken", self.name)
        self.ready.set()
        if self._voice is None:
            return
        while True:
            text, done, started = self._jobs.get()
            try:
                self._speak(text, started)
            except Exception:
                log.exception("Couldn't say %r", text)
            finally:
                done.set()
                started.set()
                if self._jobs.empty():
                    self._idle.set()

    def _sentences(self, text):
        """The voice's sentences, made one ahead in a helper thread: the next is ready when this one ends."""
        if text in self._cache:
            yield from self._cache[text]
            return
        ready = queue.Queue(maxsize=2)
        made = []

        def hand_over(item):
            while not self._stop.is_set():          # a stopped reader never takes it: don't wait forever
                try:
                    ready.put(item, timeout=0.2)
                    return True
                except queue.Full:
                    pass
            return False

        def make():
            try:
                for item in self._voice(text):
                    if not hand_over(item):
                        return
            except Exception as e:
                hand_over(e)
            hand_over(None)
        threading.Thread(target=make, name="speaker-make", daemon=True).start()
        while (item := ready.get()) is not None:
            if isinstance(item, Exception):
                raise item
            made.append(item)
            yield item
        if len(text) <= CACHE_MAX_CHARS and not self._stop.is_set():
            self._remember(text, made)          # "Opening Netflix" again: instant

    def _output(self, rate):
        """The sound output, kept open between replies: opening it costs ~200 ms before every word otherwise."""
        if self._out is not None and self._out_rate != rate:
            self._close_output()
        if self._out is None:
            self._out = sd.OutputStream(samplerate=rate, channels=1, dtype="int16")
            self._out.start()
            self._out_rate = rate
        return self._out

    def release_output(self, wait=10):
        """Close the kept-open sound output (after any reply in progress, up to `wait` s), so PortAudio can be
        restarted safely: the assistant's mic re-scan (find_mic) does that, and it killed the open stream
        ("Couldn't say …", PortAudioError -9988). It's reopened by the next reply."""
        end = time.monotonic() + wait
        while self.speaking and time.monotonic() < end:
            time.sleep(0.1)
        self._close_output()

    def _close_output(self, now=False):
        if self._out is not None:
            try:
                self._out.abort() if now else self._out.stop()
                self._out.close()
            except Exception:
                pass
            self._out = None

    def _speak(self, text, started):
        sounds.keep_awake()        # HDMI/TV audio sleeps when silent and would swallow the first word
        wrote = False
        try:
            for rate, audio in self._sentences(text):      # one chunk per sentence
                if self._stop.is_set():
                    return
                stream = self._output(rate)
                for i in range(0, len(audio), BLOCK):
                    if self._stop.is_set():
                        return
                    try:
                        stream.write(audio[i:i + BLOCK])
                    except sd.PortAudioError:
                        # The kept-open stream died under us (PortAudio was restarted by the mic re-scan, or the
                        # device changed): reopen once and go on; a second failure is a real one (outer except)
                        self._close_output(now=True)
                        stream = self._output(rate)
                        stream.write(audio[i:i + BLOCK])
                    wrote = True
                    started.set()
            if wrote:
                self._out.write(np.zeros(BLOCK * 4, dtype=np.int16))     # let the last syllable drain out
        except sd.PortAudioError:
            self._close_output(now=True)            # the device went away (TV off?): reopened next time
            raise
        finally:
            if self._stop.is_set():
                self._close_output(now=True)        # cut off: drop what's still buffered
