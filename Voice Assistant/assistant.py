"""
Voice Assistant prototype.

Say this device's wake word (settings.json: "Hey Echo" on this PC, "Hey Triton" on the Triton Launcher
one; "Hey Jarvis" until that model is trained), wait for the chime, then say a command in your own
words: "set the volume to 30 percent", "make it quieter", "open excel", "close chrome", "next song",
"minimize everything", "turn off the screen"... commands.py works out what you meant (and lists
every command); apps are found by themselves, websites and nicknames are in shortcuts.json.
Runs in the system tray (look under "Show hidden icons").
"""

import ctypes
import json
import logging
import math
import queue
import random
import re
import subprocess
from difflib import SequenceMatcher
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np
import pystray
import sounddevice as sd
import openwakeword
from openwakeword.model import Model as WakeWordModel
from PIL import Image, ImageDraw
from scipy.signal import firwin, lfilter, lfilter_zi
from vosk import KaldiRecognizer, Model as VoskModel, SetLogLevel

import actions
import events
import sounds
from apps import Catalog
from brain import clauses
from brain.llm import LocalLLM
from brain.remote import RemoteBrain
from brain.max import MaxBrain
from brain.router import Router
from brain.rules import MEDIA_ONLY
from commands import COMMANDS, FILLERS, Commands, Failed
from echo import EchoCanceller
from endpoint import Endpointer
import screen
from monitor import MonitorControl
from overlay import Overlay
from speaker import Speaker
from tools import safety, screenshare
from tools.handlers import Tools, say_first, shown_first
from transcriber import Transcriber

# When packaged as TritonVoice.exe (PyInstaller), models/ and the log live next to the exe
BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
VOSK_MODEL_DIR = BASE_DIR / "models" / "vosk-model-small-en-us-0.15"
LOG_FILE = BASE_DIR / "assistant.log"

# Mics to try, in order: (audio API, part of the device name). Falls back to the Windows default mic.
# WASAPI shares the mic with other apps (Claude, Zoom...). Windows noise suppression makes the rapoo
# read as near-silence in a quiet room, but speech comes through.
# WDM-KS ("raw driver") would take the mic exclusively and block other apps, so it isn't used.
# The laptop's built-in mic is the backup, but with the lid closed it's too muffled to be reliable.
MIC_PREFERENCES = [
    ("WASAPI", "rapoo"),
    ("WASAPI", "Microphone Array"),
]
FALLBACK_RECHECK_SEC = 30    # while on a backup mic, look for the preferred one this often
POWER_CHECK_SEC = 2          # how often to check whether the laptop is on battery (mic is off then)
SAMPLE_RATE = 16000          # what the models need; mics that can't do it natively are downsampled
FRAME_SIZE = 1280            # 80 ms frames, what openWakeWord expects

# Stage 1 (Siri-style two-stage design): a near-free loudness check decides whether anyone is
# speaking; only then does audio go to the wake-word model (stage 2).
GATE_MIN_RMS = 30            # never treat anything quieter than this as speech (120 blocked soft-spoken
                             # takes; the rapoo's noise suppression outputs zeros between words anyway)
GATE_NOISE_RATIO = 3.0       # ...or anything less than 3x the room's background noise
GATE_PREROLL_FRAMES = 19     # ~1.5 s of audio kept from before speech starts, fed in when the gate opens
GATE_HANG_FRAMES = 19        # keep the gate open ~1.5 s after speech stops (pauses between words)
# Which wake word this device answers to, so two devices can have different names:
# settings.json next to the assistant (or TritonVoice.exe): {"wake_word": "hey_echo"} on this PC,
# {"wake_word": "hey_triton"} on the one with Triton Launcher.
# Uses models/<wake_word>.onnx (trained on the cluster) or an openWakeWord built-in model of that
# name, plus models/<wake_word>_verifier.pkl as the personal voice check when that exists.
# Until a custom model has been trained, the built-in "Hey Jarvis" stands in.
SETTINGS_FILE = BASE_DIR / "settings.json"
FALLBACK_WAKE_WORD = "hey_jarvis"           # built into openWakeWord, so it always works
try:
    SETTINGS = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
except (OSError, ValueError):
    SETTINGS = {}
WANTED_WAKE_WORD = SETTINGS.get("wake_word", FALLBACK_WAKE_WORD)
_custom_model = BASE_DIR / "models" / f"{WANTED_WAKE_WORD}.onnx"
if _custom_model.exists():
    WAKE_WORD, WAKE_MODEL = WANTED_WAKE_WORD, str(_custom_model)
elif WANTED_WAKE_WORD in openwakeword.MODELS:
    WAKE_WORD = WAKE_MODEL = WANTED_WAKE_WORD
else:
    WAKE_WORD = WAKE_MODEL = FALLBACK_WAKE_WORD
WAKE_PHRASE = WAKE_WORD.replace("_", " ").title()        # "hey_echo" -> "Hey Echo"
VOICE_CHECK_MODEL = BASE_DIR / "models" / f"{WAKE_WORD}_verifier.pkl"
# 0.2, not 0.5: on 50 recorded "Hey Echo"s, 0.5 caught 21 and 0.2 caught 27, and none of 142 other clips
# (similar words, your other speech, TV-like sentences) got above 0.04 (bench/wake_bench.py)
WAKE_THRESHOLD = float(SETTINGS.get("wake_threshold", 0.2))
WAKE_PATIENCE = 2            # frames in a row (80 ms each) at/above the threshold before waking:
                             # real wake words stay up for 2-7, a stray false alarm usually for 1
STREAM_WARMUP_SEC = 1.0      # ignore audio this long after the mic opens (opening it makes a burst of noise)
VERIFIER_GATE = 0.3          # wake-word score at which the personal voice check gets asked
COMMAND_WINDOW_SEC = 6.0     # how long to wait for a command to start after the wake word (endpoint.py ends it)
CONFIRM_WINDOW_SEC = 4.0     # how long to wait for "yes" / "no" after asking
# Said (instead of the chime) when the wake word is heard; settings.json "wake_reply": [...], or [] for the chime
WAKE_REPLIES = SETTINGS.get("wake_reply", ["Yeah?", "Mm-hm?", "I'm listening."])
# "chime": a short chime and listening at once, so you can say it all in one breath (default, ~1 s faster);
# "voice": say one of WAKE_REPLIES first and then listen (the old way)
WAKE_STYLE = SETTINGS.get("wake_style", "chime")
# Conversation: after answering, keep listening this long for a follow-up without the wake word
# ("open netflix" ... "click shashank" ... "that's all"); 0 turns it off. Not after something starts playing,
# or the film's dialogue would be heard as commands.
FOLLOW_UP_SEC = float(SETTINGS.get("follow_up_sec", 5))
MAX_FOLLOW_UPS = 8
# Every command's audio + what was heard, in voice_samples/commands/ (newest 500): real data to measure the
# ears on your voice and mic, and to fine-tune Whisper on later
SAVE_COMMAND_CLIPS = SETTINGS.get("save_command_clips", True)
COMMAND_CLIP_DIR = BASE_DIR / "voice_samples" / "commands"
# Saved audio never grows past these (oldest deleted first): ~200 commands, ~600 wake clips
COMMAND_CLIPS_MB = float(SETTINGS.get("command_clips_mb", 40))
WAKE_CLIPS_MB = float(SETTINGS.get("wake_clips_mb", 40))
# While a video or song plays here, its voices could sound like "Hey Triton": wake only on a clearer
# "Hey Triton" for longer (your own takes score 0.84+ with the personal voice check), and no follow-up listening
MEDIA_WAKE_THRESHOLD = float(SETTINGS.get("media_wake_threshold", 0.75))
MEDIA_WAKE_PATIENCE = 3
# Shown (and said) once everything has loaded, so you know when it can take commands
READY_TEXT = "Ready · say “Hey Triton”"
READY_SAY = SETTINGS.get("ready_say", "Triton's ready.")
WAKE_PREFIX = re.compile(r"^\s*(hey|hi|ok|okay)[\s,]+(triton|echo)\b[\s,.!]*", re.I)
# For _doubtful(): what a sentence needs before the small model may type it, and what makes it a question
TYPE_WORDS = {"type", "write", "enter", "input", "dictate", "fill"}
MINI_CHECKED = {"power", "lock", "set_volume", "cancel"}    # _mini_doubtful: the calls a model invents from noise
QUESTION = re.compile(r"^\s*(what|why|how|who|whom|when|where|which|is|are|was|were|can|could|do|does|did|"
                      r"tell me|explain)\b", re.I)
# Instead of guessing at a misheard command (README 5g): ask again, in varied words
ASK_AGAIN = ["Sorry, could you say that again?", "Sorry, I didn't quite catch that.", "Say that once more?",
             "Hmm, I missed that. Again, please?"]
# Said while the laptop thinks, if it takes longer than this (so there's no dead air)
FILLER_AFTER_SEC = float(SETTINGS.get("filler_after_sec", 2.5))
FILLERS_SAID = ["Hmm.", "One sec.", "Let me think.", "Okay, one moment."]
# The laptop gets this long to decide (or start talking); its 4B usually needs 4-8 s. On 2026-11-03 it took 12-40 s and then you got "say that again";
# now it's dropped (and its work cancelled) at this point, and Triton says so plainly
LAPTOP_DEADLINE_SEC = float(SETTINGS.get("laptop_deadline_sec", 12))
TOO_SLOW = ["Sorry, that's taking me too long. Could you say it more simply?",
            "I'm stuck on that one. Try saying it a different way?"]
# While the laptop's screen and sound are on the TV: "wake_only" = only "Hey Triton" (stricter, no follow-ups, no
# training clips of the laptop's audio); "off" = not listening at all until sharing stops (stop it from the bubble)
SHARING_LISTEN = SETTINGS.get("sharing_listen", "wake_only")
SHARING_WAKE_THRESHOLD = float(SETTINGS.get("sharing_wake_threshold", 0.7))
# What Whisper writes for silence, music or room noise
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "you", "bye", "okay", "so",
                  "subtitles by the amara org community", "please subscribe", "the end", "music", "applause", "hmm",
                  "huh", "uh", "um", "umm", "ah", "oh", "mm", "mhm", "er", "eh", "ha"}
# Ending the conversation: handled at once on this device, never sent anywhere
ENOUGH = re.compile(r"^(ok(ay)? |no |oh )?(that'?s all|that is all|that'?s it|never ?mind|nothing|forget it|no thanks|"
                    r"cancel|nothing else|i'?m (done|good)|all done|shut up|be quiet|quiet)( for now)?"
                    r"( thanks| thank you)?,?( triton)?[.!]?$", re.I)
CANCEL_WORDS = {"cancel", "stop", "nevermind", "nothing", "nope", "quit", "enough", "done", "bye", "goodbye", "thanks"}
YES_WORDS = {"yes", "yeah", "yep", "sure", "okay", "do", "go", "ahead", "confirm"}
NO_WORDS = {"no", "nope", "cancel", "stop", "don't", "not"}
VOICE_FULL_SCALE = 2500      # RMS level that makes the bubble react at full strength
# Warn in the tray if the mic delivers nothing but zeros for this long (0 to turn off).
# Off: Windows noise suppression legitimately outputs zeros in a quiet room.
SILENCE_WARN_SEC = 0
DEBUG_LEVELS_SEC = 0         # log mic level + best wake score every N seconds (0 to turn off)
DEBUG_CLIPS = False          # save ~4 s clips of loud sounds to debug_clips/ for troubleshooting
DEBUG_CLIP_LEVEL = 1000      # peak level that counts as "loud"
DEBUG_CLIP_DIR = BASE_DIR / "debug_clips"
# Real-life wake-word examples for retraining: every wake-up (and whether a command followed) and
# every near miss is saved to voice_samples/<wake_word>/live/ (~80 KB each, newest 400 kept)
SAVE_WAKE_CLIPS = SETTINGS.get("save_wake_clips", True)
NEAR_MISS_SCORE = 0.02         # a sentence that scored this much without waking counts as a near miss
WAKE_CLIP_DIR = BASE_DIR / "voice_samples" / WAKE_WORD / "live"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("assistant")


# ---------- actions ----------

monitor = MonitorControl()
# Your websites and nicknames for apps; installed apps are found by themselves (apps.py)
SHORTCUTS_FILE = BASE_DIR / "shortcuts.json"


# ---------- power ----------

class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


_HEY = {"hey", "hay", "hi", "hair", "head", "hit", "here", "he", "hate", "heat", "eh", "a", "hello", "okay", "ok"}


def _tritonish(words):
    s = re.sub(r"[^a-z]", "", "".join(words).lower())
    return len(s) >= 4 and SequenceMatcher(None, s, "triton").ratio() >= 0.6


def strip_wake(text):
    """The command without "Hey Triton" (or how Whisper misheard it: "hair triter", "hey tritan") at its start:
    said in one breath, or said again after the reply. "" when that was all there was."""
    w = text.split()
    for i in range(min(3, len(w))):                  # "um hey triton", "hey hey triton"
        if re.sub(r"[^a-z]", "", w[i].lower()) in _HEY:
            for k in (1, 2):
                if i + 1 + k <= len(w) and _tritonish(w[i + 1:i + 1 + k]):
                    return " ".join(w[i + 1 + k:]).lstrip(",. ")
    if w and len(w[0]) > 4 and _tritonish(w[:1]):   # "triton, open youtube"
        return " ".join(w[1:]).lstrip(",. ")
    return text




def garbled(text):
    """Text that can't be a real request: nothing, Whisper's filler for noise ("Thank you."), or a word stuck
    on repeat ("search search search search"). Asked again instead of being acted on."""
    if not text or re.search(r"[\[(](music|blank|silence|applause|inaudible|noise)", text, re.I):
        return True
    words = re.findall(r"[a-z']+", text.lower())
    if not words or " ".join(words) in HALLUCINATIONS:
        return True
    if any(words[i] == words[i + 1] == words[i + 2] for i in range(len(words) - 2)):
        return True
    return len(words) >= 5 and len(set(words)) / len(words) < 0.5


_NUMS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50,
         "sixty": 60, "a couple of": 2, "a couple": 2, "couple of": 2, "a few": 3, "few": 3}
SNOOZE = re.compile(r"\b(stop|pause|quit|no more) ?listening\b|\bdon'?t listen\b|\bbe quiet\b|\bgo quiet\b|\bshut up\b|"
                    r"\bmute yourself\b|\btake a break\b|\bleave me alone\b|\bstop listening to me\b", re.I)
SNOOZE_FOR = re.compile(r"\bfor (?:the next )?(half an hour|an hour and a half|\d+|" + "|".join(sorted(_NUMS, key=len, reverse=True))
                        + r") ?(seconds?|secs?|minutes?|mins?|hours?|hrs?)?\b", re.I)
RESUME = re.compile(r"\b(start|resume|continue|begin) listening\b|\blisten (again|to me)\b|\bwake up\b|"
                    r"\byou can (talk|listen)\b|\bi'?m back\b|\bunmute yourself\b", re.I)


def snooze_request(text):
    """Seconds to stop listening for ("stop listening for 10 minutes"), math.inf with no time given, else None."""
    if not text or not SNOOZE.search(text):
        return None
    m = SNOOZE_FOR.search(text)
    if not m:
        return math.inf
    amount, unit = m.group(1).lower(), (m.group(2) or "minutes").lower()
    if amount == "half an hour":
        return 1800
    if amount == "an hour and a half":
        return 5400
    n = int(amount) if amount.isdigit() else _NUMS.get(amount, 1)
    return n * (1 if unit.startswith("s") else 3600 if unit.startswith("h") else 60)


def say_duration(seconds):
    if seconds >= 3600:
        h = seconds / 3600
        return "an hour" if h == 1 else f"{h:g} hours"
    if seconds >= 60:
        m = round(seconds / 60)
        return "a minute" if m == 1 else f"{m} minutes"
    return f"{int(seconds)} seconds"


def over_budget(files, megabytes):
    """The oldest of these files (names start with the date) beyond a total of `megabytes`."""
    newest_first = sorted(files, reverse=True)
    total, budget = 0, megabytes * 1024 * 1024
    for i, f in enumerate(newest_first):
        try:
            total += f.stat().st_size
        except OSError:
            continue
        if total > budget:
            return newest_first[i:]
    return []


def on_battery():
    status = SYSTEM_POWER_STATUS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
    return status.ACLineStatus == 0     # 0 = battery, 1 = plugged in, 255 = unknown


# ---------- tray icon ----------

def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    # simple microphone glyph
    d.rounded_rectangle((25, 14, 39, 38), radius=7, fill="white")
    d.arc((19, 24, 45, 46), start=0, end=180, fill="white", width=3)
    d.line((32, 46, 32, 52), fill="white", width=3)
    return img


ICON_LISTENING = make_icon((46, 160, 67))   # green
ICON_PAUSED = make_icon((130, 130, 130))    # grey


# ---------- microphone ----------

def find_mic():
    """Return (device index, sample rate, channels, rank) for the first usable mic in MIC_PREFERENCES.

    rank is the position in MIC_PREFERENCES (0 = most preferred); the default-mic fallback gets
    len(MIC_PREFERENCES).
    """
    # PortAudio only scans devices at startup; re-scan so a replugged webcam shows up
    sd._terminate()
    sd._initialize()
    apis = sd.query_hostapis()
    devices = sd.query_devices()
    for rank, (api_name, mic_name) in enumerate(MIC_PREFERENCES):
        for i, dev in enumerate(devices):
            if (dev["max_input_channels"] == 0 or api_name not in apis[dev["hostapi"]]["name"]
                    or mic_name.lower() not in dev["name"].lower()):
                continue
            channels = dev["max_input_channels"]
            for rate in (SAMPLE_RATE, 48000, int(dev["default_samplerate"])):
                try:
                    sd.check_input_settings(device=i, channels=channels, samplerate=rate, dtype="int16")
                    return i, rate, channels, rank
                except Exception:
                    pass
    dev = sd.query_devices(kind="input")
    return None, int(dev["default_samplerate"]), 1, len(MIC_PREFERENCES)


class Downsampler:
    """Low-pass filter + decimate by an integer factor, keeping filter state across blocks."""

    def __init__(self, in_rate, out_rate):
        if in_rate % out_rate:
            raise ValueError(f"Mic rate {in_rate} Hz is not a multiple of {out_rate} Hz")
        self.factor = in_rate // out_rate
        self.taps = firwin(63, 0.9 / self.factor)
        self.state = lfilter_zi(self.taps, 1.0) * 0

    def __call__(self, block):
        if self.factor == 1:
            return block
        filtered, self.state = lfilter(self.taps, 1.0, block.astype(np.float32), zi=self.state)
        return np.clip(filtered[::self.factor], -32768, 32767).astype(np.int16)


class VoiceGate:
    """Stage 1: lets audio through to the wake-word model only while someone seems to be speaking.

    push(frame) returns (frames to run the wake-word model on, whether the gate just closed).
    """

    def __init__(self):
        self.noise = float(GATE_MIN_RMS) / GATE_NOISE_RATIO
        self.preroll = deque(maxlen=GATE_PREROLL_FRAMES)
        self.hang = 0
        self.is_open = False

    def push(self, frame):
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        speech = rms > max(GATE_MIN_RMS, self.noise * GATE_NOISE_RATIO)
        # Follow the room's background level: drop quickly when it gets quieter, rise slowly
        # (~10 s) so steady noise like a fan stops counting as speech but a sentence doesn't.
        self.noise += (rms - self.noise) * (0.1 if rms < self.noise else 0.008)

        if speech:
            self.hang = GATE_HANG_FRAMES
            if not self.is_open:
                self.is_open = True
                frames = list(self.preroll) + [frame]
                self.preroll.clear()
                return frames, False
            return [frame], False
        if self.is_open:
            self.hang -= 1
            if self.hang <= 0:
                self.is_open = False
                return [frame], True
            return [frame], False
        self.preroll.append(frame)
        return [], False


# ---------- assistant ----------

class Assistant:
    def __init__(self):
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.audio = queue.Queue()
        self.icon = None
        self.overlay = Overlay()
        self.overlay.loading("Triton is starting…")
        events.emit("state", text="starting")
        # Commands typed in the test console (test_console.py) are handled like spoken ones, without the ears
        self.typed = queue.Queue()
        events.listen_for_typed(self.typed.put)
        self.is_ready = threading.Event()
        self.media_playing = False
        threading.Thread(target=self._watch_media, daemon=True).start()
        self.last_reply = ""         # what Triton said last, for the conversation history
        # "Stop listening for 10 minutes": until then only "Hey Triton, start listening" is answered (math.inf = until
        # then, 0 = listening). Different from the tray's Pause, which closes the mic.
        self.snooze_until = 0.0
        # The laptop's screen and sound are on the TV (Triton Screen Share): its audio mustn't become commands
        self.sharing = False
        threading.Thread(target=self._watch_sharing, daemon=True).start()

        sounds.prepare(BASE_DIR)
        log.info("Loading models...")
        # The personal voice check (if trained) runs only when the wake-word model is fairly sure,
        # and its score replaces the wake-word score: only your voice gets past WAKE_THRESHOLD.
        verifier = {WAKE_WORD: str(VOICE_CHECK_MODEL)} if VOICE_CHECK_MODEL.exists() else {}
        self.wake = WakeWordModel(wakeword_models=[WAKE_MODEL], inference_framework="onnx",
                                  custom_verifier_models=verifier, custom_verifier_threshold=VERIFIER_GATE)
        if WAKE_WORD != WANTED_WAKE_WORD:
            log.info("No model for '%s' yet (models/%s.onnx); using '%s' until it's trained",
                     WANTED_WAKE_WORD, WANTED_WAKE_WORD, WAKE_WORD)
        log.info("Wake phrase '%s'%s", WAKE_PHRASE, " with personal voice check" if verifier else "")
        SetLogLevel(-1)
        self.vosk = VoskModel(str(VOSK_MODEL_DIR))
        catalog = Catalog(SHORTCUTS_FILE)
        self.commands = Commands(catalog, monitor)
        self.vocabulary = None       # words the recogniser listens for; built on first use (apps load meanwhile)
        # The local model (brain/llm.py) understands what the rules can't; it starts in the background.
        # Without one (no model file), everything works as before with the rules alone.
        self.llm = LocalLLM.from_settings(SETTINGS.get("brain", {}), BASE_DIR)
        # The laptop's big brain (brain/remote.py -> brain_server.py), if set up: asked about anything the
        # rules aren't sure of, before this device's own model; and it answers questions out loud
        self.remote = RemoteBrain.from_settings(SETTINGS.get("big_brain", {}))
        # Max (the C++ assistant on this laptop, brain = Claude): answers questions out loud, if enabled and running
        self.max = MaxBrain.from_settings(SETTINGS.get("max", {}))
        self.router = Router(self.commands, catalog, self.llm, backup=self.remote is not None)
        self.tools = Tools(catalog, monitor, screenshare.ScreenShare(SETTINGS, BASE_DIR))
        # Triton's voice (speaker.py, Piper): replies are said out loud; loads in the background, and
        # until it's ready (or without a voice file) the chimes stand in as before.
        self.speaker = Speaker.from_settings(SETTINGS.get("voice", {}), BASE_DIR)
        # This PC's own sound (YouTube on the TV, Triton's voice) taken out of the mic (echo.py, WebRTC AEC3).
        # Tested 2026-11-04: TV speech alone -> Whisper heard nothing; your command over it heard intact
        self.echo = EchoCanceller.start(SETTINGS.get("echo_cancel", True))
        # Better ears (transcriber.py, Whisper): re-reads the command when the rules aren't sure, so the
        # model gets a clean sentence; only useful with a model, which is the only thing reading it.
        self.ears = Transcriber.from_settings(SETTINGS.get("ears", {}), BASE_DIR) if self.llm or self.remote else None
        # Second opinion for sentences the fast ears didn't make into a clear command (settings "ears" ->
        # "second_opinion": "whisper-small", "" = off)
        big = SETTINGS.get("ears", {}).get("second_opinion", "whisper-small")
        self.ears_big = Transcriber.from_settings({**SETTINGS.get("ears", {}), "model": big}, BASE_DIR)             if self.ears and big and big != self.ears.name else None
        log.info("Models loaded")
        if self.speaker:
            # Phrases said often, made now so they play instantly (others are kept after their first use)
            self.speaker.prepare([READY_SAY, *WAKE_REPLIES, *ASK_AGAIN, *FILLERS_SAID, "Okay", "Turning it up",
                                  "Turning it down", "Scrolling", "Muted", "Unmuted", "Playing", "Pausing",
                                  "Next one", "Typing it", "Opening Netflix", "Opening YouTube"])
        threading.Thread(target=self._announce_ready, daemon=True).start()

    def _announce_ready(self):
        """Once the voice and this device's model have loaded (a minute on the N95): "Triton's ready." Until
        then the bubble says "Triton is starting…" (the wake word and fixed commands already work)."""
        if self.speaker:
            self.speaker.ready.wait(180)
        if self.llm:
            self.llm.ready.wait(600)
        self.is_ready.set()
        log.info("Triton ready")
        events.emit("state", text="ready")
        self.overlay.done(True, READY_TEXT)
        self._say(READY_SAY)

    def _watch_media(self):
        """Is a video or song playing here? Checked every 2 s (Windows' media controls)."""
        while not self.stopped.is_set():
            playing = actions.something_playing()
            if playing != self.media_playing:
                self.media_playing = playing
                log.info("Media %s", "playing: stricter wake word, no follow-ups" if playing else "stopped")
                events.emit("state", media=playing)
            self.stopped.wait(2)

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            log.warning("Audio status: %s", status)
        block = self.downsample(indata[:, 0])
        if self.echo is not None:
            try:
                block = self.echo.process(block)
            except Exception:
                log.exception("Echo cancellation failed; mic audio as is from now on")
                self.echo = None
        self.audio.put(block)

    def _next_frame(self, timeout=0.5):
        try:
            return self.audio.get(timeout=timeout)
        except queue.Empty:
            return None

    def _clear_audio(self):
        with self.audio.mutex:
            self.audio.queue.clear()

    def _listen_for_command(self, window=COMMAND_WINDOW_SEC):
        """What was said after the wake word, as (grammar text, free text).

        Two recognisers hear the same audio: one limited to the command words (accurate, what the
        rules read) and, when there's a local model, one that hears any word (what the model reads,
        for "click subscribe" or "search for pasta recipes"). Stops as soon as the rules are sure,
        when the free recogniser hears the sentence end, or when time's up. If the rules still aren't
        sure then, Whisper (self.ears) re-reads the recording and its text replaces the free one."""
        if self.vocabulary is None:
            # Any sequence of these words can be recognised; "[unk]" soaks up everything else
            words = self.commands.vocabulary(known=lambda w: self.vosk.vosk_model_find_word(w) >= 0)
            self.vocabulary = json.dumps(words + ["[unk]"])
            log.info("Listening for commands with %d words", len(words))
        rec = KaldiRecognizer(self.vosk, SAMPLE_RATE, self.vocabulary)
        free = KaldiRecognizer(self.vosk, SAMPLE_RATE) if self.llm or self.remote else None
        # With Whisper (self.ears), Whisper hears every command and the command grammar is only a fallback:
        # the grammar knows ~340 words and forces anything else into them ("search on netflix" came out as
        # "silence netflix" -> mute), so it mustn't end listening early or decide what was said.
        # Same in big_brain mode "always".
        stop_on_rules = not self.ears and not (self.remote and self.remote.mode == "always")
        # When the command is over: voice activity (endpoint.py), not a fixed window. `window` is how long you have
        # to start talking; a long sentence may go on (up to 12 s) and ends ~0.8 s after you stop.
        ep = Endpointer(start_window=window, only_wake=lambda text: not strip_wake(WAKE_PREFIX.sub("", text)))
        hard_stop = time.monotonic() + window + ep.max_sec      # in case the mic stops delivering frames
        heard, heard_free = [], []
        partial = ""
        recorded = []
        self.only_wake = False      # all that was said was "Hey Triton" (again)
        self.last_command_audio = None
        # Streaming ears (Moonshine) hear while you talk: the text is ready the moment you stop
        streaming = bool(self.ears and getattr(self.ears, "streaming", False))
        if streaming:
            self.ears.begin()
        # What's on screen, looked up while you talk, so the brain knows where "here" is (screen.py)
        self.screen = screen.look()

        def text_of(result):
            return json.loads(result).get("text", "").replace("[unk]", "").strip()

        while not self.stopped.is_set() and time.monotonic() < hard_stop:
            frame = self._next_frame()
            if frame is None:
                continue
            recorded.append(frame)
            if streaming:
                self.ears.feed(frame)
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            self.overlay.set_level(rms / VOICE_FULL_SCALE)
            if rec.AcceptWaveform(frame.tobytes()):
                part = text_of(rec.Result())
                if part:
                    heard.append(part)
                    # Without Whisper the rules decide: stop as soon as they're sure
                    if stop_on_rules and self.router.rules(" ".join(heard)):
                        ep.reason = "rules sure"
                        break
            if free is not None:
                if free.AcceptWaveform(frame.tobytes()):
                    part = text_of(free.Result())
                    if part:
                        heard_free.append(part)
                    partial = ""
                else:
                    partial = json.loads(free.PartialResult()).get("partial", "")
            # "open ..." / "set the volume to ...": a pause there is mid-sentence, so the endpointer waits longer
            if ep.push(frame, " ".join(heard_free + [partial]) if free is not None else " ".join(heard)):
                break
        heard.append(text_of(rec.FinalResult()))
        if free is not None:
            heard_free.append(text_of(free.FinalResult()))
        heard, heard_free = " ".join(h for h in heard if h), " ".join(h for h in heard_free if h)
        log.info("Listening ended (%s) after %.1f s", ep.reason or "stopped", ep.elapsed)
        span = ep.speech_span()
        streamed = self.ears.finish() if streaming else ""
        if self.ears and recorded and span is None and ep.reason != "rules sure":
            # Nobody spoke (the voice detector heard no speech): don't let Whisper or the grammar invent a command
            log.info("No speech heard (grammar had %r)", heard_free or heard)
            return "", ""
        whisper = ""
        if self.ears and recorded:
            self.overlay.thinking()
            t0 = time.monotonic()
            # Only the speech (plus a little padding) goes to Whisper: less noise to make words out of, and faster
            if span:
                recorded = [np.concatenate(recorded)[span[0]:span[1]]]
            # Kept for a second listen with the bigger ears if nothing in it was understood (_do, "repeat")
            self.last_command_audio = np.concatenate(recorded)
            whisper = streamed if streaming else self.ears.transcribe(np.concatenate(recorded))
            log.info("Ears (%s, %.1f s): %r (grammar heard %r)", self.ears.name, time.monotonic() - t0, whisper, heard)
            if whisper:
                # Who decides: Whisper's words when they make a clear command (best on sentences). Otherwise the
                # grammar's command, but only if the free recogniser (no word list to force things into) heard
                # the same command: the grammar is good at one-word commands Whisper fumbles ("mute" -> "Nute"),
                # and the agreement stops "search on netflix" -> "silence netflix" -> mute. Else the brain gets
                # Whisper's words.
                said = whisper
                whisper = strip_wake(WAKE_PREFIX.sub("", whisper))  # "hey triton, open netflix" in one breath
                self.only_wake = not whisper
                # Not a clear command: hear it again with the bigger ears (whisper-small, ~2x slower, but it got
                # 6 more of the N95's 30 real commands right) before the model or the laptop guesses at it
                # (Streaming ears: not here, where it would cost every unclear command ~2-4 s on the N95, but only
                # after nothing in it was understood: _do's second listen)
                if whisper and self.ears_big and not streaming and not self._clear(whisper):
                    t0 = time.monotonic()
                    again = strip_wake(WAKE_PREFIX.sub("", self.ears_big.transcribe(np.concatenate(recorded))))
                    log.info("Bigger ears (%s, %.1f s): %r", self.ears_big.name, time.monotonic() - t0, again)
                    if again:
                        whisper = again
                if self.only_wake:
                    log.info("Only the wake phrase: %r", said)
                agreed = self.router.rules(heard)
                if self.router.rules(whisper) or not agreed or agreed != self.router.rules(heard_free):
                    heard = whisper
                heard_free = whisper
        if SAVE_COMMAND_CLIPS and recorded and (whisper or heard):
            self._save_command_clip(np.concatenate(recorded), whisper, heard)
        return heard, heard_free

    def _second_listen(self, said):
        """whisper-small on the last command's audio, when the streaming ears' text led nowhere; "" if it heard
        nothing new (or there's no second opinion / no audio)."""
        audio, self.last_command_audio = getattr(self, "last_command_audio", None), None
        if audio is None or not self.ears_big or not getattr(self.ears, "streaming", False):
            return ""
        t0 = time.monotonic()
        again = strip_wake(WAKE_PREFIX.sub("", self.ears_big.transcribe(audio)))
        log.info("Second listen (%s, %.1f s): %r (first heard %r)", self.ears_big.name, time.monotonic() - t0, again, said)
        if not again or garbled(again) or again.lower().strip() == (said or "").lower().strip():
            return ""
        return again

    def _clear(self, text):
        """A command placed without any model: the rules are sure, screen sharing, or a pause request."""
        return bool(self.router.rules(text) or screenshare.intent(text) or snooze_request(text))

    def _save_command_clip(self, audio, whisper, grammar):
        try:
            COMMAND_CLIP_DIR.mkdir(parents=True, exist_ok=True)
            stem = COMMAND_CLIP_DIR / time.strftime("%Y%m%d_%H%M%S")
            with wave.open(str(stem.with_suffix(".wav")), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(audio.astype(np.int16).tobytes())
            stem.with_suffix(".txt").write_text(f"whisper: {whisper}\ngrammar: {grammar}\n", encoding="utf-8")
            for old in over_budget(COMMAND_CLIP_DIR.glob("*.wav"), COMMAND_CLIPS_MB):
                old.unlink(missing_ok=True)
                old.with_suffix(".txt").unlink(missing_ok=True)
        except OSError:
            log.exception("Couldn't save the command clip")

    def _hear_yes(self, question):
        """Ask out loud (and in the bubble) and wait for yes / no. Anything else, or silence, is a no."""
        self.overlay.show(question)
        self._say(question, wait=True)     # finish talking before listening, or the mic hears Triton
        sounds.wake()
        time.sleep(0.3)
        self._clear_audio()
        words = [w for w in sorted(YES_WORDS | NO_WORDS) if self.vosk.vosk_model_find_word(w) >= 0]
        rec = KaldiRecognizer(self.vosk, SAMPLE_RATE, json.dumps(words + ["[unk]"]))
        deadline = time.monotonic() + CONFIRM_WINDOW_SEC
        while time.monotonic() < deadline and not self.stopped.is_set():
            frame = self._next_frame()
            if frame is None:
                continue
            if rec.AcceptWaveform(frame.tobytes()):
                said = set(json.loads(rec.Result()).get("text", "").split())
                if said & NO_WORDS:
                    return False
                if said & YES_WORDS:
                    return True
        said = set(json.loads(rec.FinalResult()).get("text", "").split())
        log.info("Confirmation heard: %s", said or "nothing")
        return bool(said & YES_WORDS) and not said & NO_WORDS

    def run(self):
        # Pick the mic on every (re)start so a replugged webcam is found again. find_mic restarts PortAudio, which
        # kills the speaker's kept-open output: close it first (after any reply in progress)
        if self.speaker:
            self.speaker.release_output()
        mic, mic_rate, channels, rank = find_mic()
        self.downsample = Downsampler(mic_rate, SAMPLE_RATE)
        mic_info = sd.query_devices(mic if mic is not None else sd.default.device[0])
        log.info("Using mic '%s' (%s) at %d Hz", mic_info["name"],
                 sd.query_hostapis(mic_info["hostapi"])["name"], mic_rate)
        self._clear_audio()

        with sd.InputStream(device=mic, samplerate=mic_rate, channels=channels, dtype="int16",
                            blocksize=FRAME_SIZE * self.downsample.factor, callback=self._on_audio):
            log.info("Listening for wake word '%s'", WAKE_WORD)
            last_sound = time.monotonic()
            mic_silent = False
            debug_at, debug_level, debug_score = time.monotonic(), 0, 0.0
            recent = deque(maxlen=50)    # last ~4 s of audio, for debug clips
            clip_due = None
            started = time.monotonic()
            power_checked = started
            gate = VoiceGate()
            self.wake.reset()
            hits = 0                     # frames in a row at/above WAKE_THRESHOLD
            heard_by_model = deque(maxlen=40)    # what the wake model got this time (~3 s), for live clips
            best = 0.0                           # its best score this time someone spoke
            # Leaving this loop closes the stream, which releases the mic while paused
            while not self.stopped.is_set() and not self.paused.is_set():
                if time.monotonic() - power_checked > POWER_CHECK_SEC:
                    power_checked = time.monotonic()
                    if on_battery():
                        return      # run_safely keeps the mic closed until plugged in again

                # On a backup mic, return now and then so run_safely re-picks the preferred one
                if rank > 0 and time.monotonic() - started > FALLBACK_RECHECK_SEC:
                    log.info("On a backup mic; checking whether the preferred mic is back")
                    return

                try:
                    typed = self.typed.get_nowait()
                except queue.Empty:
                    typed = None
                if typed:
                    self._handle_typed(typed)
                    self.wake.reset()
                    gate, hits, best = VoiceGate(), 0, 0.0
                    heard_by_model.clear()
                    self._clear_audio()
                    continue

                frame = self._next_frame()
                if frame is None or time.monotonic() - started < STREAM_WARMUP_SEC:
                    continue

                if frame.any():
                    last_sound = time.monotonic()
                    if mic_silent:
                        mic_silent = False
                        log.info("Mic is delivering audio again")
                        self._set_tray(ICON_LISTENING, "Voice Assistant (listening)")
                elif SILENCE_WARN_SEC and not mic_silent and time.monotonic() - last_sound > SILENCE_WARN_SEC:
                    mic_silent = True
                    log.warning("Mic is delivering pure silence (muted or disconnected?)")
                    self._set_tray(ICON_PAUSED, "Voice Assistant (no sound from mic!)")

                if self.sharing and SHARING_LISTEN == "off":     # settings: deaf while the laptop is on the TV
                    continue
                threshold = self._wake_threshold()
                self.snoozed                                       # (ends a timed "stop listening" when it's up)

                # Stage 1 decides whether stage 2 (the wake-word model) needs to run at all
                frames, gate_closed = gate.push(frame)
                if gate.is_open:
                    sounds.keep_awake()     # wake the TV's audio now, so the chime isn't swallowed
                score = 0.0
                for f in frames:
                    s = self.wake.predict(f).get(WAKE_WORD, 0)
                    heard_by_model.append(f)
                    best = max(best, s)
                    hits = hits + 1 if s >= threshold else 0
                    if hits >= (MEDIA_WAKE_PATIENCE if self.media_playing else WAKE_PATIENCE):
                        score = max(score, s)
                if gate_closed:
                    # (not while the laptop's audio plays here: those clips would teach the wake word its sound)
                    if best >= NEAR_MISS_SCORE and score < threshold and not self.sharing:
                        self._save_wake_clip(heard_by_model, f"near_{best:.2f}")
                    self.wake.reset()   # don't carry stale state into the next time someone speaks
                    hits = 0
                    heard_by_model.clear()
                    best = 0.0

                if DEBUG_LEVELS_SEC:
                    debug_level = max(debug_level, int(abs(frame.astype(np.int32)).max()))
                    debug_score = max(debug_score, score)
                    if time.monotonic() - debug_at >= DEBUG_LEVELS_SEC:
                        log.info("mic peak level %5d / 32767, best wake score %.2f", debug_level, debug_score)
                        debug_at, debug_level, debug_score = time.monotonic(), 0, 0.0

                if DEBUG_CLIPS:
                    recent.append(frame)
                    if clip_due is None and abs(frame.astype(np.int32)).max() >= DEBUG_CLIP_LEVEL:
                        clip_due = time.monotonic() + 2.5   # keep recording so the whole phrase is in the clip
                    elif clip_due is not None and time.monotonic() >= clip_due:
                        self._save_clip(np.concatenate(recent))
                        clip_due = None

                if score < threshold:
                    continue

                log.info("Wake word detected (score %.2f)", score)
                events.emit("wake", score=round(score, 2), media=self.media_playing)
                wake_clip = list(heard_by_model)
                if self.snoozed:
                    # Asked to stop listening: no reply, and only "start listening" is taken
                    self._clear_audio()
                    heard, heard_free = self._listen_for_command()
                    if RESUME.search(f"{heard_free} {heard}"):
                        self.overlay.show()
                        self._unsnooze()
                        self._note(heard_free or heard, "rules", "listening again")
                        self._converse()
                    else:
                        log.info("Not listening (asked to stop); ignored %r", heard_free or heard)
                    self.wake.reset()
                    gate, hits, best = VoiceGate(), 0, 0.0
                    heard_by_model.clear()
                    self._clear_audio()
                    continue
                if self.speaker:
                    self.speaker.stop()     # talking over Triton cuts it short
                self.overlay.show()
                # "Yes sir?" (finished before listening starts, or the mic would hear it as the command);
                # the chime when there's no voice yet
                if WAKE_STYLE == "voice" and WAKE_REPLIES and self._say(random.choice(WAKE_REPLIES), wait=True):
                    # Drop audio from the reply's loud end (the mic hears it too) so it isn't read as a command
                    time.sleep(0.3)
                    self._clear_audio()
                else:
                    # "chime" (default): a short chime, and the audio since the wake word is kept, so "Hey Triton,
                    # open YouTube" works in one breath (strip_wake removes the wake phrase's tail). ~1 s faster.
                    sounds.wake()

                heard, heard_free = self._listen_for_command()
                log.info("Heard command: %r (free: %r)", heard, heard_free)
                events.emit("heard", text=heard_free or heard, grammar=heard, by="voice")
                self.overlay.set_level(0)
                going_on = self._do(heard, heard_free)
                self._remember(heard_free or heard)
                if FOLLOW_UP_SEC and going_on:          # also after "sorry, say that again": listen for it
                    self._converse()
                # Followed by a command: almost surely real. Silence after it: often a false wake-up.
                self._save_wake_clip(wake_clip, f"wake_{score:.2f}_{'cmd' if heard or heard_free else 'nocmd'}")

                self.wake.reset()
                gate = VoiceGate()
                hits = 0
                heard_by_model.clear()
                best = 0.0
                self._clear_audio()

    def _handle_typed(self, text):
        """A command typed in the test console: the same steps as a spoken one, minus the ears."""
        log.info("Typed command: %r", text)
        events.emit("heard", text=text, by="typed")
        self.overlay.show(text)
        self._do(text, text)
        self._remember(text)

    def _doubtful(self, calls, text):
        """Does this device's small model's answer look wrong, so the laptop should be asked instead?
        Its known slips (README "Round 1 verdict"): typing as a fallback for anything ("why is the sky blue"
        -> type it), "play X on youtube" -> just the play key, searches without a search, unknown apps."""
        words = set(re.findall(r"[a-z']+", text.lower()))
        tools = {c["tool"] for c in calls}
        # "open Netflix and <something more>" answered with one action: the rest was dropped
        # (a leading "and" is a follow-up, "and search X there", not a second part)
        if len(calls) == 1 and re.search(r"\w+\s+(and|then)\s+\w+\s+\w+", text.lower()) and "type_text" not in tools:
            return True
        if "type_text" in tools and not words & TYPE_WORDS:
            return True
        if QUESTION.match(text) and tools & {"type_text", "click", "open", "press_keys"}:
            return True
        if words & {"search", "find", "look"} and not tools & {"web_search", "youtube_search"}:
            return True
        if "youtube_search" in tools and "youtube" not in words:       # "open netflix and search for X" -> YouTube
            return True
        if any(c["tool"] == "media" and (c.get("args") or {}).get("action") == "play" for c in calls) \
                and words - MEDIA_ONLY - FILLERS:
            return True
        # An app that doesn't exist ("bring clumps forward" -> maximize "clumps"): misheard
        if any((c["tool"] in ("open", "switch_to") or (c["tool"] in ("window", "close") and (c.get("args") or {}).get("target")))
               and not self.router.catalog.find((c.get("args") or {}).get("target") or "") for c in calls):
            return True
        return any(not self._supported(c, text, words) for c in calls)

    def _mini_doubtful(self, calls, text):
        """Triton-Mini v2+ asks again by itself, so only the calls it can invent from noise are checked (README 5r:
        on 1000 items the full _doubtful threw away 72 right answers to stop 5 wrong ones; power and set_volume were
        the only checks that stopped more than they broke, and power stopped both dangerous ones)."""
        words = set(re.findall(r"[a-z']+", text.lower()))
        for c in calls:
            args = c.get("args") or {}
            # Closing the window / tab in front, or a new tab, needs words for it: on the N95 "Closet" -> close,
            # "clear the search bar" -> close the tab, "do you want the browser" -> new tab (2026-11-06)
            if (c["tool"] == "close" and not args.get("target")) or args.get("action") == "close_tab":
                if not words & {"close", "shut", "exit", "quit", "kill", "rid", "stop", "off", "tab"}:
                    return True
            if c["tool"] == "browser" and args.get("action") == "new_tab" and \
                    not words & {"new", "tab", "tap", "fresh", "another", "blank"}:
                return True
        return any(c["tool"] in MINI_CHECKED and not self._supported(c, text, words) for c in calls)

    @staticmethod
    def _supported(call, text, words):
        """Is there something in the sentence that this call acts on? The small model must always pick a tool, so on
        a misheard bit of noise it invents one: "minute" -> say the time, "hit right hand increase the volume to"
        -> volume 9, "huh" -> cancel. Those go to the laptop instead (which can ask you to say it again)."""
        tool, args = call["tool"], call.get("args") or {}
        low = text.lower()
        if tool == "set_volume":
            level = str(args.get("level", ""))
            return level in re.findall(r"\d+", low) or bool(words & {"max", "maximum", "full", "min", "minimum", "zero",
                                                                    "half", "hundred", "fifty", "twenty", "thirty",
                                                                    "forty", "sixty", "seventy", "eighty", "ninety", "ten"})
        if tool == "info":
            return bool(words & {"time": {"time", "clock", "o'clock"}, "date": {"date", "day", "today", "today's"},
                                 "battery": {"battery", "charge", "charged", "power"}}.get(args.get("what"), set()))
        if tool == "cancel":
            return bool(words & CANCEL_WORDS) or "never mind" in low or "that's all" in low or "forget it" in low
        if tool == "power":
            # "can you stop the assistant running here" -> shut down (2026-11-04): only when the computer is meant
            return bool(words & {"shutdown", "shut", "restart", "reboot", "sleep", "computer", "pc", "nuc", "system"}) \
                and not words & {"assistant", "triton", "listening", "app", "music", "video", "song"}
        if tool in ("web_search", "youtube_search"):
            query = set(re.findall(r"[a-z']+", str(args.get("query", "")).lower()))
            return bool(query) and len(query & words) >= max(1, len(query) // 2)
        if tool == "click" and words & (set(clauses.ORDINALS) | {"last", "top"}):
            return True                     # "open the first video" -> the first result's name, not in the words
        if tool in ("open", "close", "switch_to", "click"):
            target = set(re.findall(r"[a-z']+", str(args.get("target", "")).lower()))
            return not target or bool(target & words)
        return True

    def _converse_with_laptop(self, text):
        """The laptop's conversational brain: {"calls"}, {"say"} or {"repeat"}; None if unreachable. If it takes
        a moment, a short filler is said meanwhile, so it feels like someone thinking instead of dead air."""
        self.overlay.thinking("Thinking…")
        filler = threading.Timer(FILLER_AFTER_SEC, lambda: self._say(random.choice(FILLERS_SAID)))
        filler.start()
        reply = {}
        self.streamed = []
        cut_off = threading.Event()

        def speak(sentence):
            # Answers arrive sentence by sentence while the laptop writes them: start talking at the first one
            if cut_off.is_set() or not sentence:
                return
            filler.cancel()
            if not self.streamed:
                self.overlay.done(True, sentence)
            self.streamed.append(sentence)
            self._say(sentence)

        # What's on screen goes along ("[screen: Netflix - Google Chrome | on screen: Shashank, Kids…]"), so the
        # laptop knows where "here" is and how names are spelled
        look = getattr(self, "screen", None)
        sent = f"{look.context()} {text}".strip() if look else text
        asking = threading.Thread(target=lambda: reply.update(r=self.remote.converse(sent, on_sentence=speak)),
                                  daemon=True)
        asking.start()
        deadline = time.monotonic() + LAPTOP_DEADLINE_SEC
        try:
            # Meanwhile the mic stays on: "Hey Triton" cuts the wait short (the late reply is dropped). A spoken
            # answer that has started may take longer; a decision may not
            busy = lambda: asking.is_alive() and (self.streamed or time.monotonic() < deadline)
            if self._watch_for_wake(busy, timeout=120):
                cut_off.set()
                self.remote.cancel()           # stop the laptop working on it (it'd slow the next request)
                log.info("Cut off while the laptop was thinking about %r", text)
                return {"interrupted": True}
            if asking.is_alive() and not self.streamed:
                cut_off.set()
                self.remote.cancel()
                log.info("Laptop too slow (over %.0f s) for %r; dropped", LAPTOP_DEADLINE_SEC, text)
                return {"slow": True}
            asking.join()
            return reply.get("r")
        finally:
            filler.cancel()

    def _ask_max(self, text):
        """Max answers a question (plain text to say; never commands). {"say"} / {"interrupted"}; None = not
        answered (off, not running, slow, error), so the usual chain carries on."""
        self.overlay.thinking("Thinking…")
        filler = threading.Timer(FILLER_AFTER_SEC, lambda: self._say(random.choice(FILLERS_SAID)))
        filler.start()
        reply = {}
        asking = threading.Thread(target=lambda: reply.update(r=self.max.ask(text, self.max.timeout_s)), daemon=True)
        asking.start()
        try:
            if self._watch_for_wake(lambda: asking.is_alive(), timeout=self.max.timeout_s + 5):
                log.info("Cut off while Max was thinking about %r", text)
                return {"interrupted": True}
            answer = reply.get("r")
            return {"say": answer} if answer else None
        finally:
            filler.cancel()

    def _watch_for_wake(self, busy, timeout=30):
        """While busy() (thinking or talking), keep listening for the wake word only. True = it was said, so
        the caller stops what it's doing. Its own voice can't do this: the personal voice check rejects it."""
        gate, hits = VoiceGate(), 0
        self.wake.reset()
        end = time.monotonic() + timeout
        while busy() and time.monotonic() < end and not self.stopped.is_set():
            frame = self._next_frame(0.1)
            if frame is None:
                continue
            frames, closed = gate.push(frame)
            for f in frames:
                hits = hits + 1 if self.wake.predict(f).get(WAKE_WORD, 0) >= self._wake_threshold() else 0
                if hits >= WAKE_PATIENCE:
                    self.wake.reset()
                    if self.speaker:
                        self.speaker.stop()
                    return True
            if closed:
                self.wake.reset()
                hits = 0
        return False

    def _wake_threshold(self):
        # While the laptop's sound or a video plays here, be stricter (your own takes score 0.84+ with the voice check)
        if self.sharing:
            return max(WAKE_THRESHOLD, SHARING_WAKE_THRESHOLD)
        return max(WAKE_THRESHOLD, MEDIA_WAKE_THRESHOLD) if self.media_playing else WAKE_THRESHOLD

    def _snooze(self, seconds):
        self.snooze_until = time.monotonic() + seconds if seconds != math.inf else math.inf
        until = "" if seconds == math.inf else time.strftime(" until %H:%M", time.localtime(time.time() + seconds))
        log.info("Not listening%s (asked by voice)", until or " until asked")
        self._set_tray(ICON_PAUSED, f"Voice Assistant (not listening{until})")
        self._ok(f"Okay, I'll stop listening for {say_duration(seconds)}." if seconds != math.inf
                 else "Okay, I'll stop listening. Say Hey Triton, start listening, when you need me.")

    def _unsnooze(self, said=True):
        self.snooze_until = 0.0
        log.info("Listening again")
        self._set_tray(ICON_LISTENING, "Voice Assistant (listening)")
        if said:
            self._ok("I'm back. What can I do for you?")

    @property
    def snoozed(self):
        if self.snooze_until and time.monotonic() >= self.snooze_until:
            self._unsnooze(said=False)              # the time is up: quietly listening again
            sounds.wake()
        return bool(self.snooze_until)

    def _watch_sharing(self):
        """Is the laptop's screen (and sound) on the TV right now? Asked of Triton Screen Share every 3 s."""
        while not self.stopped.is_set():
            status = screenshare._send("status", timeout=1) or {}
            sharing = bool(status.get("session"))
            if sharing != self.sharing:
                self.sharing = sharing
                log.info("Screen sharing %s: %s", "on" if sharing else "off",
                         "listening more strictly, no follow-ups" if SHARING_LISTEN != "off" else "not listening")
            self.stopped.wait(3)

    def _understand(self, heard, heard_free):
        """What to do with what was heard: ("act", calls, who), ("say", text, who) or ("repeat", None, who).
        In order, each step only if the ones before can't place it:
          1. nonsense (empty, "search search search", Whisper's "thank you" on noise): ask again
          2. screen sharing phrases, then the fixed rules when they're sure: act at once
          3. this device's fine-tuned model, when its answer passes the checks in _doubtful: act (~1.5 s)
          4. the laptop's conversational big brain: act, talk back, or ask again (a few seconds)
          5. nothing sure enough: ask again. A wrong action is worse than "sorry, say that again", so the
             old fallback to the rules' unsure guess is gone."""
        text = heard_free or heard
        if getattr(self, "only_wake", False):       # "Hey Triton" again (or only that was heard): listen again
            self.only_wake = False
            return "interrupted", None, "you"
        pause = snooze_request(heard_free) or snooze_request(heard)
        if pause:
            return "snooze", pause, "rules"
        if garbled(text):
            log.info("Sounds like nothing / misheard: %r", text)
            return "repeat", None, "ask again"
        if ENOUGH.match(text.strip()):
            return "act", [{"tool": "cancel", "args": {}}], "rules"
        calls = screenshare.intent(heard_free) or screenshare.intent(heard)
        if calls:
            return "act", calls, "screen share"
        remote = self.remote if self.remote and self.remote.available else None
        if remote and remote.mode == "always":
            reply = self._converse_with_laptop(text)
            if reply:
                if reply.get("calls"):
                    reply = {"calls": clauses.fix_calls(reply["calls"], text, getattr(self, "screen", None))}
                return self._laptop_decision(reply)
        calls = self.router.rules(heard)
        if calls:
            return "act", calls, "rules"
        look = getattr(self, "screen", None)
        # The answer to "Which one?" after a click found nothing: just a name ("Shashank")
        options, self.pending_click = getattr(self, "pending_click", None), None
        if options is not None and text.split() and text.split()[0].lower() not in clauses.VERBS:
            name = clauses.closest(clauses.clean(text), options) or clauses.clean(text)
            return "act", [{"tool": "click", "args": {"target": name}}], "rules"
        # Each part on its own ("open Netflix" + "select Shashank"), with the words as heard (brain/clauses.py)
        placed, unclear = clauses.place(text, self.router.rules, look, self.router.catalog)
        if placed and not unclear:
            log.info("Placed part by part: %s", placed)
            return "act", placed, "rules"
        # A question (not a command): Max, the big brain on this laptop, answers; if it can't, the chain goes on
        if self.max and not placed and self.max.is_question(text) and self.max.available():
            reply = self._ask_max(text)
            if reply:
                return self._laptop_decision(reply)
        local = None
        # Part of it was clear and part not: the small model tends to do the clear part and drop the rest silently
        # ("open Netflix and <mumble>" -> just open), so it's skipped; the laptop or "did X, didn't get Y" instead
        if self.llm and self.llm.ready.is_set() and not (remote and remote.mode == "first") and not placed:
            self.overlay.thinking()
            if getattr(self.llm, "mini", False):
                # Triton-Mini v2: acts, talks or asks again by itself, with the screen and the last few turns
                ctx = look.context() if look else ""
                reply = self.llm.decide((ctx + " " if ctx else "") + text, history=list(self._local_history()))
                if reply and reply.get("say"):
                    return "say", reply["say"], "model"
                if reply and reply.get("repeat"):
                    return "repeat", None, "model"
                local = clauses.fix_calls((reply or {}).get("calls"), text, look)
            else:
                local = clauses.fix_calls(self.router.model(text), text, look)
            doubt = self._mini_doubtful if getattr(self.llm, "mini", False) else self._doubtful
            if local and local[0]["tool"] not in ("clarify", "escalate") and not doubt(local, text):
                return "act", local, "model"
            if local and local[0]["tool"] == "clarify" and (local[0].get("args") or {}).get("question"):
                return "say", local[0]["args"]["question"], "model"
            log.info("Own model not sure (%s); %s", local, "asking the laptop" if remote else "no laptop")
        if remote:
            reply = self._converse_with_laptop(text)
            if reply:
                if reply.get("calls"):
                    reply = {"calls": clauses.fix_calls(reply["calls"], text, look)}
                if not (reply.get("slow") and placed):
                    return self._laptop_decision(reply)
        if placed:
            # Some of it was clear: do that, then ask about the rest instead of "say that again" for all of it
            return "partial", (placed, unclear), "rules"
        if local and local[0]["tool"] == "cancel":
            return "act", local, "model"
        if local and local[0]["tool"] == "escalate":
            return "say", "I'd need my big brain on the laptop for that, and I can't reach it right now.", "model"
        return "repeat", None, "ask again"

    @staticmethod
    def _laptop_decision(reply):
        if reply.get("interrupted"):
            return "interrupted", None, "you"
        if reply.get("slow"):
            return "say", random.choice(TOO_SLOW), "laptop (too slow)"
        if "calls" in reply:
            return "act", reply["calls"], "laptop"
        if "say" in reply:
            return "say", reply["say"], "laptop"
        return "repeat", None, "laptop"

    def _say(self, text, wait=False, start_within=0):
        """Say text out loud. False when there's no voice (yet), so the caller plays a chime instead."""
        events.emit("said", text=text)
        return bool(self.speaker) and self.speaker.say(text, wait=wait, start_within=start_within)

    def _ok(self, text):
        self.last_reply = text
        self.overlay.done(True, text)
        if not self._say(text):
            sounds.ok()

    def _fail(self, text):
        log.info("-> %s", text)
        self.last_reply = text
        self.overlay.done(False, text)
        if not self._say(text):
            sounds.fail()

    def _converse(self):
        """Keep listening for follow-ups without the wake word, until a silence, "that's all", or something
        starts playing (its sound would be heard as commands)."""
        misses = 0
        for _ in range(MAX_FOLLOW_UPS):
            if self.sharing or self.snoozed:     # the laptop's sound would be heard as follow-ups
                break
            if self.media_playing or actions.something_playing():
                log.info("Something is playing: no follow-up listening (its voices would be heard as commands)")
                break
            # Don't listen to Triton itself while it talks, but "Hey Triton" cuts it short
            if self.speaker and self._watch_for_wake(lambda: self.speaker.speaking, timeout=60):
                log.info("Cut off while talking")
                self._say(random.choice(WAKE_REPLIES or ["Yes?"]), wait=True)
            time.sleep(0.3)
            self._clear_audio()
            self.overlay.show("Listening…")
            heard, heard_free = self._listen_for_command(window=FOLLOW_UP_SEC)
            if not (heard or heard_free):
                break
            log.info("Follow-up: %r (free: %r)", heard, heard_free)
            events.emit("heard", text=heard_free or heard, grammar=heard, by="voice (follow-up)")
            self.overlay.set_level(0)
            going_on = self._do(heard, heard_free)
            self._remember(heard_free or heard)
            misses = misses + 1 if self.missed else 0
            if not going_on or misses >= 2:
                break
        if self.remote:
            self.remote.forget()                 # the conversation is over
        self._local_history().clear()

    def _note(self, said, who, did):
        """What this device handled by itself goes to the laptop's conversation log too (tray > Show conversation)."""
        if self.remote and who != "laptop":
            self.remote.note(said, who, did)

    def _remember(self, said):
        if self.remote and said:
            self.remote.remember(said, self.last_reply)
        if said:
            self._local_history().append({"user": said[:300], "reply": (self.last_reply or "")[:300]})

    def _local_history(self):
        """This conversation's last turns for the N95's own model (Triton-Mini v2); cleared when it ends."""
        if not hasattr(self, "local_history"):
            self.local_history = deque(maxlen=3)
        return self.local_history

    def _do(self, heard, heard_free="", asked=False):
        """Carry out what was heard, step by step, and say how it went in the bubble. Returns whether
        the conversation can go on (False after "never mind", or once something starts playing)."""
        self.last_reply = ""
        self.missed = False
        said = heard_free or heard
        t0 = time.monotonic()
        kind, payload, who = self._understand(heard, heard_free)
        if kind == "repeat":
            # Nothing understood from the fast ears: hear the same recording again with the bigger ones (whisper-small)
            # and try once more, before asking you to repeat yourself
            again = self._second_listen(said)
            if again:
                heard = heard_free = said = again
                kind, payload, who = self._understand(again, again)
        events.emit("decided", who=who, what=kind, payload=payload, sec=round(time.monotonic() - t0, 2))
        ask_after = None
        if kind == "partial":                  # do the clear part, then ask about the rest (see the end)
            (payload, ask_after), kind = payload, "act"
        if kind == "interrupted":       # "Hey Triton" while it was thinking: drop that, listen again
            self._say(random.choice(WAKE_REPLIES or ["Yes?"]), wait=True)
            return True
        if kind == "snooze":
            self._snooze(payload)
            self._note(said, who, f"stopped listening for {say_duration(payload)}" if payload != math.inf
                       else "stopped listening until asked")
            return False
        if kind == "repeat":
            # Rather than guessing: ask again, and keep listening (the follow-up needs no wake word)
            self.missed = True
            self._fail(random.choice(ASK_AGAIN))
            self._note(said, who, "asked to say it again")
            return True
        if kind == "say":
            if who == "laptop" and getattr(self, "streamed", None):
                self.last_reply = payload               # already being said, sentence by sentence
                self.overlay.done(True, payload)
            else:
                self._ok(payload)
            self._note(said, who, f'said "{payload}"')
            return True
        calls = payload
        if who != "laptop":
            self._note(said, who, "did: " + ", ".join(
                c["tool"] + "".join(f" {v}" for v in (c.get("args") or {}).values()) for c in calls))
        result, shown = None, None
        # Say what's about to happen right away ("Opening YouTube"), while it happens, instead of after.
        # Not when something asks first (confirm), hides the screen (shown_first), or the result is the answer.
        preview = None
        if all(safety.check(c)[0] == "ok" and not shown_first(c) for c in calls):
            preview = say_first(calls)
            # Wait (briefly) until it's audible, so the words come with the action instead of after it
            if preview and self._say(preview, start_within=1.5):
                self.overlay.thinking(preview)
            else:
                preview = None
        for i, call in enumerate(calls):
            tool = call["tool"]
            if tool == "cancel":
                self._ok("Okay")
                return False
            if tool == "escalate":
                # Not a computer command: the laptop's big brain answers it out loud, if it's reachable
                answer = None
                if self.remote and self.remote.available:
                    self.overlay.thinking("Thinking…")
                    answer = self.remote.answer(heard_free or heard)
                if answer:
                    self._ok(answer)
                else:
                    self._fail("I can only control the computer for now" if not self.remote
                               else "My big brain on the laptop isn't reachable")
                return True
            if tool == "clarify":
                if asked:                  # asked once already: don't loop
                    self._fail("Didn't catch that")
                    return True
                return self._clarify(call["args"].get("question") or "Sorry?", heard_free or heard)
            verdict, why = safety.check(call)
            if verdict == "refuse":
                self._fail(why)
                return True
            if verdict == "confirm" and not self._hear_yes(why):
                self._fail("Cancelled")
                return True
            shown = shown_first(call)
            if shown:
                # It hides the screen (monitor off, lock): show the bubble and say it first
                self.overlay.done(True, shown)
                if not self._say(shown, wait=True):
                    sounds.ok()
                    time.sleep(0.6)
            t0 = time.monotonic()
            try:
                result = self.tools.run(call)
            except Failed as e:
                events.emit("did", tool=tool, args=call.get("args"), ok=False, result=str(e))
                if tool == "click":
                    self._which_one(str((call.get("args") or {}).get("target", "")))
                else:
                    self._fail(str(e))
                return True
            except Exception as e:
                log.exception("Tool %s failed", call)
                events.emit("did", tool=tool, args=call.get("args"), ok=False, result=repr(e))
                self._fail(f"Couldn't {tool.replace('_', ' ')}")
                return True
            events.emit("did", tool=tool, args=call.get("args"), ok=True, result=result,
                        sec=round(time.monotonic() - t0, 2))
            log.info("-> %s", result)
            if i < len(calls) - 1:
                self.overlay.thinking(result)      # progress between steps
                time.sleep(0.8)                    # let the app react before the next step
        if preview:
            self.last_reply = f"{preview} ({result})"
            self.overlay.done(True, result)        # already said; just show how it went
        elif not shown:
            self._ok(result)
        if ask_after:
            self._fail(f"I didn't get “{ask_after}”. Say that part again?")
            return True
        # Stop listening for follow-ups once something plays (its sound would be heard as commands) or the
        # screen went off / locked
        playing = any((c["tool"] == "media" and (c.get("args") or {}).get("action") == "play")
                      or (c["tool"] == "click" and "play" in str((c.get("args") or {}).get("target", "")).lower())
                      or (c["tool"] == "screen_share" and (c.get("args") or {}).get("action") in ("mirror", "extend"))
                      for c in calls)
        return not (playing or shown)

    def _which_one(self, target):
        """A click found nothing called `target`: say what IS on screen and take a bare name as the answer
        ("Which one? I see Shashank, Kids…" -> "Shashank"), instead of a generic "say that again"."""
        names = screen.look().names
        self.pending_click = names
        options = [n for n in names if len(n) <= 25][:4]
        self._fail(f"I can't see “{target}”." + (f" I see {', '.join(options)}. Which one?" if options else
                                                  " Say the name as it's written on screen?"))

    def _clarify(self, question, said):
        """Ask the model's question, listen once more, and try again with both answers together."""
        self.overlay.show(question)
        self._say(question, wait=True)
        sounds.wake()
        time.sleep(0.3)
        self._clear_audio()
        heard, heard_free = self._listen_for_command()
        log.info("Answer to %r: %r (free: %r)", question, heard, heard_free)
        answer = heard_free or heard
        if not answer:
            self._fail("Didn't catch that")
            return True
        return self._do(f"{said} {heard}".strip(), f"{said} {answer}".strip(), asked=True)

    def _save_wake_clip(self, frames, label):
        if not SAVE_WAKE_CLIPS or not frames:
            return
        try:
            WAKE_CLIP_DIR.mkdir(parents=True, exist_ok=True)
            path = WAKE_CLIP_DIR / time.strftime(f"%Y%m%d_%H%M%S_{label}.wav")
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(np.concatenate(list(frames)).tobytes())
            for old in over_budget(WAKE_CLIP_DIR.glob("*.wav"), WAKE_CLIPS_MB):
                old.unlink(missing_ok=True)
        except OSError:
            log.exception("Couldn't save wake clip")

    def _save_clip(self, audio):
        DEBUG_CLIP_DIR.mkdir(exist_ok=True)
        path = DEBUG_CLIP_DIR / time.strftime("clip_%H%M%S.wav")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(audio.tobytes())
        for old in sorted(DEBUG_CLIP_DIR.glob("clip_*.wav"))[:-20]:
            old.unlink()
        log.info("Saved debug clip %s", path.name)

    def _set_tray(self, image, title):
        if self.icon:
            self.icon.icon, self.icon.title = image, title

    def run_safely(self):
        # The mic can be briefly unavailable (just after login, or right after another app released it),
        # so keep retrying instead of dying.
        battery_mode = False
        while not self.stopped.is_set():
            if self.paused.is_set():
                self.stopped.wait(0.5)
                continue
            if on_battery():
                if not battery_mode:
                    battery_mode = True
                    log.info("On battery: mic off until plugged in")
                    self._set_tray(ICON_PAUSED, "Voice Assistant (off while on battery)")
                self.stopped.wait(POWER_CHECK_SEC)
                continue
            if battery_mode:
                battery_mode = False
                log.info("Plugged in: listening again")
                self._set_tray(ICON_LISTENING, "Voice Assistant (listening)")
            try:
                self.run()
            except Exception:
                log.exception("Assistant crashed, retrying in 5 s")
                self._set_tray(ICON_PAUSED, "Voice Assistant (error, retrying; see assistant.log)")
                self.stopped.wait(5)
                self._set_tray(ICON_LISTENING, "Voice Assistant (listening)")

    # ---------- tray menu handlers ----------

    def open_console(self, icon=None, item=None):
        """The test console (test_console.py): every step live, and typed commands."""
        if getattr(sys, "frozen", False):          # TritonVoice.exe (launcher build): the exe opens it
            subprocess.Popen([sys.executable, "--console"], cwd=BASE_DIR)
            return
        pyw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(BASE_DIR / "test_console.py")],
                         cwd=BASE_DIR)

    def preview_bubble(self, icon=None, item=None):
        def play():
            self.overlay.show()
            start = time.monotonic()
            while time.monotonic() - start < 2.5:     # fake a voice so the bubble reacts
                t = time.monotonic() - start
                self.overlay.set_level(0.5 + 0.5 * math.sin(t * 7) * math.sin(t * 2.3))
                time.sleep(0.04)
            self.overlay.set_level(0)
            self.overlay.done(True, "This is how Triton looks")

        threading.Thread(target=play, daemon=True).start()

    def toggle_pause(self, icon, item):
        if self.snooze_until:                # paused by voice ("stop listening"): Resume ends it
            self._unsnooze(said=False)
        elif self.paused.is_set():
            self.paused.clear()
            icon.icon, icon.title = ICON_LISTENING, "Voice Assistant (listening)"
        else:
            self.paused.set()
            icon.icon, icon.title = ICON_PAUSED, "Voice Assistant (paused)"

    def quit(self, icon, item):
        log.info("Quit from tray menu")
        self.stopped.set()
        if self.llm:
            self.llm.close()
        icon.stop()


def main():
    if "--console" in sys.argv:            # TritonVoice.exe --console: the test console, not a second assistant
        import test_console
        test_console.Console().run()
        return
    # One copy only (Triton Launcher starts it, start.vbs may too): two would both answer "Hey Triton"
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "TritonVoice.SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:    # ERROR_ALREADY_EXISTS
        log.info("Already running; exiting")
        return

    assistant = Assistant()
    menu = pystray.Menu(
        pystray.MenuItem(f'Say "{WAKE_PHRASE}", then for example:', None, enabled=False),
        *[pystray.MenuItem(f'  "{c.example}"', None, enabled=False) for c in COMMANDS if c.example],
        *([pystray.MenuItem(f'  "{e}"', None, enabled=False) for e in
           ("click subscribe", "open youtube and search lofi", "type hello and press enter")] if assistant.llm else []),
        pystray.MenuItem("Test console", assistant.open_console),
        pystray.MenuItem("Preview bubble", assistant.preview_bubble),
        pystray.MenuItem(lambda item: "Resume" if assistant.paused.is_set() or assistant.snooze_until else "Pause",
                         assistant.toggle_pause),
        pystray.MenuItem("Quit", assistant.quit),
    )
    icon = pystray.Icon("voice_assistant", ICON_LISTENING, "Voice Assistant (listening)", menu)
    assistant.icon = icon

    # Triton Launcher sets this event when it exits, so we quit tidily (no leftover tray icon)
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    quit_event = kernel32.CreateEventW(None, True, False, "TritonVoice.Quit")

    def quit_when_launcher_asks():
        kernel32.WaitForSingleObject(quit_event, 0xFFFFFFFF)
        log.info("Triton Launcher asked us to quit")
        assistant.quit(icon, None)

    threading.Thread(target=quit_when_launcher_asks, daemon=True).start()

    # Some Windows mics (e.g. Intel Smart Sound) refuse to start a stream from a background
    # thread, so audio runs on the main thread and the tray icon gets its own thread.
    threading.Thread(target=icon.run, daemon=True).start()
    try:
        assistant.run_safely()
    finally:
        log.info("Assistant exited")


if __name__ == "__main__":
    main()
