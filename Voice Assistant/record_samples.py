"""
Records the voice samples used to train the personal voice check ("only respond to me") and,
for a custom wake word, the cluster training.

Walks through prompts in a small window, for this device's wake word (settings.json), or another
one with --word, e.g.  python record_samples.py --word hey_triton
  - 20x the wake word in different styles         -> voice_samples/<wake_word>/positive/
  - similar-sounding phrases, normal sentences,
    and a few seconds of room noise               -> voice_samples/<wake_word>/negative/
--more records a second round focused on quiet and far-away speech.
--far records from across the room: put the laptop where the assistant's mic will be, press Start,
walk away, and say the phrase after each high beep (no need to read the screen; it's always the same).
Everything is saved locally as 16 kHz mono WAV files; nothing leaves the laptop.
"""

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
import winsound

import numpy as np
import sounddevice as sd

from assistant import BASE_DIR, FRAME_SIZE, SAMPLE_RATE, Downsampler, find_mic
from assistant import WANTED_WAKE_WORD as WAKE_WORD      # the name chosen in settings.json, even before it's trained

if "--word" in sys.argv:
    WAKE_WORD = sys.argv[sys.argv.index("--word") + 1]
PHRASE = WAKE_WORD.replace("_", " ").title()         # "hey_echo" -> "Hey Echo"
SAMPLES_DIR = BASE_DIR / "voice_samples" / WAKE_WORD
FAR = "--far" in sys.argv
ANY_MIC = "--any-mic" in sys.argv      # default: refuse to record unless the Rapoo (the N95's mic) is found
MIN_PEAK = 60 if FAR else 200   # anything quieter is probably a missed take (soft takes peak around 800)

WAKE_STYLES = [
    "normally", "normally", "normally", "normally", "a bit quieter", "a bit louder",
    "quickly", "slowly", "while looking away from the webcam", "from a step further back",
    "normally", "casually, like talking to a friend", "a bit quieter", "normally",
    "quickly", "normally", "slowly", "from a step further back", "normally", "normally",
]
# Sound-alikes that must NOT wake it (the same lists the cluster training uses)
SIMILAR = {
    "hey_echo": ["Hey Eric", "Hey, Eko", "Echo", "Hey, let's go", "Hey Becca", "Hey, ego"],
    "hey_triton": ["Hey Tristan", "Hey Brighton", "Triton", "Hey, try it on", "Hey Titan", "Hey, tighten",
                   "Hey, trite one", "Hey Crayton", "Hey, brighten", "Hey, tritium"],
}.get(WAKE_WORD, ["Hey there", "Hey you", "Okay then", "Hey, what's up", "Hello", "Hey, listen"])
SENTENCES = [
    "What's the weather like today?",
    "I'll be back in about twenty minutes.",
    "Can you send me that file later?",
    "The quick brown fox jumps over the lazy dog.",
    "Let's order some food for dinner tonight.",
    "I think the meeting moved to Thursday afternoon.",
    "Turn the music down a little, please.",
    "Remind me to call my parents tomorrow.",
]

# "python record_samples.py --more": a second round focused on soft and far-away speech
MORE_STYLES = (
    ["quietly"] * 6 + ["very softly, almost a whisper"] * 4 + ["from across the room"] * 4
    + ["quietly, from across the room"] * 3 + ["normally"] * 5 + ["quickly and quietly"] * 2
    + ["with some music or a video playing"] * 3 + ["lazily, like you just woke up"] * 3
)
MORE_SENTENCES = [
    "I'm going to make some tea now.",
    "Did you see the message I sent?",
    "Hold on, give me a second.",
    "Nothing much, just working today.",
    "Okay, see you later then.",
    "No, I'll do that tomorrow morning.",
]

# "python record_samples.py --far": from where you'll actually be when talking to it.
# Takes are 4 s (walking back is fine, the trim finds the phrase); a low double beep marks the sound-alikes.
FAR_STYLES = (
    ["from across the room, normally"] * 12 + ["from across the room, a bit quieter"] * 8
    + ["from where you usually sit"] * 8 + ["across the room, facing away from it"] * 6
    + ["across the room, with music or a video playing"] * 6
)

# "python record_samples.py --test": a mixed round saved to test/ instead of positive/. Keep test/ on the
# laptop (don't upload it): the model never trains on it, so bench\wake_bench.py --test is an honest score.
TEST = "--test" in sys.argv
TEST_STYLES = (["normally"] * 8 + ["quietly"] * 4 + ["from across the room"] * 6 + ["quickly"] * 3
               + ["with music or a video playing"] * 5 + ["lazily, like you just woke up"] * 2 + ["a bit louder"] * 2)

# (kind, file prefix, what to say, hint, seconds)
if TEST:
    STEPS = [("positive", WAKE_WORD, f'"{PHRASE}"', f"Say it {style}", 4.0) for style in TEST_STYLES]
elif FAR:
    STEPS = (
        [("positive", WAKE_WORD, f'"{PHRASE}"', f"Say it {style}", 4.0) for style in FAR_STYLES]
        + [("negative", "similar", f'"{p}"', "Not the wake word, from across the room", 4.0) for p in SIMILAR]
        + [("negative", "speech", f'"{s}"', "Not the wake word, from across the room", 5.0) for s in MORE_SENTENCES]
    )
elif "--more" in sys.argv:
    STEPS = (
        [("positive", WAKE_WORD, f'"{PHRASE}"', f"Say it {style}", 3.0) for style in MORE_STYLES]
        + [("negative", "speech", f'"{s}"', "Not the wake word: say it quietly", 5.0) for s in MORE_SENTENCES]
    )
else:
    STEPS = (
        [("positive", WAKE_WORD, f'"{PHRASE}"', f"Say it {style}", 3.0) for style in WAKE_STYLES]
        + [("negative", "similar", f'"{p}"', "Not the wake word: helps it tell similar words apart", 3.0) for p in SIMILAR]
        + [("negative", "speech", f'"{s}"', "Read this out naturally", 5.0) for s in SENTENCES]
        + [("negative", "background", "(stay quiet)", "Recording a few seconds of the room", 5.0)] * 2
    )

SPEAK_SCRIPT = ("Add-Type -AssemblyName System.Speech; $t = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$t.Speak($args[0])")

BG, FG, DIM = "#101018", "#f2f2f7", "#8a8aa0"


def speak(text):
    """Read a prompt out loud (--far: you're too far away to read the screen). Returns once it's said."""
    subprocess.run(["powershell", "-NoProfile", "-Command", f"& {{{SPEAK_SCRIPT}}} '{text.replace(chr(39), '')}'"],
                   capture_output=True)
COLORS = {"ready": "#3a3a52", "recording": "#e5484d", "done": "#2ea043", "retry": "#d29922"}


class Recorder:
    def __init__(self):
        self.audio = queue.Queue()
        self.level = 0.0
        self.redo = threading.Event()
        self.running = False

        self.root = tk.Tk()
        self.root.title("Voice Assistant: record your voice")
        self.root.configure(bg=BG)
        self.root.geometry("680x380")
        self.root.attributes("-topmost", True)

        self.progress = tk.Label(self.root, text="", bg=BG, fg=DIM, font=("Segoe UI", 11))
        self.progress.pack(pady=(18, 0))
        self.say = tk.Label(self.root, text="Ready when you are", bg=BG, fg=FG,
                            font=("Segoe UI Semibold", 30), wraplength=620)
        self.say.pack(pady=(26, 6))
        self.hint = tk.Label(self.root, bg=BG, fg=DIM, font=("Segoe UI", 13), wraplength=620,
                             text="About 8 minutes. Put the laptop where the assistant's mic will be, press Start "
                                  "and walk across the room; it reads every prompt out loud." if FAR else
                                  "About 3 minutes. Sit where you normally sit. Speak when the bar turns red.")
        self.hint.pack()
        self.meter = tk.Canvas(self.root, width=560, height=18, bg=BG, highlightthickness=0)
        self.meter.pack(pady=26)
        self.bar = self.meter.create_rectangle(0, 0, 560, 18, fill=COLORS["ready"], width=0)
        self.fill = self.meter.create_rectangle(0, 0, 0, 18, fill=FG, width=0)

        buttons = tk.Frame(self.root, bg=BG)
        buttons.pack()
        self.start_btn = tk.Button(buttons, text="Start", width=12, command=self.start)
        self.start_btn.pack(side="left", padx=6)
        self.redo_btn = tk.Button(buttons, text="Redo last", width=12, state="disabled", command=self.redo.set)
        self.redo_btn.pack(side="left", padx=6)

        self.root.after(50, self._draw_level)

    # ---------- UI (Tk thread only) ----------

    def _ui(self, **kw):
        self.root.after(0, lambda: self._apply(**kw))

    def _apply(self, say=None, hint=None, progress=None, state=None, finished=False):
        if say is not None:
            self.say.config(text=say)
        if hint is not None:
            self.hint.config(text=hint)
        if progress is not None:
            self.progress.config(text=progress)
        if state is not None:
            self.meter.itemconfig(self.bar, fill=COLORS[state])
        if finished:
            self.redo_btn.config(state="disabled")
            self.start_btn.config(text="Close", state="normal", command=self.root.destroy)

    def _draw_level(self):
        self.meter.coords(self.fill, 0, 6, int(560 * min(1.0, self.level)), 12)
        self.root.after(50, self._draw_level)

    # ---------- recording (worker thread) ----------

    def start(self):
        self.start_btn.config(state="disabled")
        self.redo_btn.config(state="normal")
        threading.Thread(target=self._session, daemon=True).start()

    def _on_audio(self, indata, frames, time_info, status):
        frame = self.downsample(indata[:, 0])
        self.level = float(np.abs(frame).max()) / 12000
        if self.running:
            self.audio.put(frame)

    def _record(self, seconds):
        with self.audio.mutex:
            self.audio.queue.clear()
        self.running = True
        chunks, needed = [], int(seconds * SAMPLE_RATE)
        while sum(len(c) for c in chunks) < needed:
            chunks.append(self.audio.get())
        self.running = False
        return np.concatenate(chunks)[:needed]

    def _save(self, path, audio):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(audio.astype(np.int16).tobytes())

    def _session(self):
        mic, rate, channels, rank = find_mic()
        # Only the Rapoo (MIC_PREFERENCES[0]): it's the N95's mic, and training on another mic's sound
        # teaches the model the wrong thing. --any-mic allows the fallbacks.
        if rank != 0 and not ANY_MIC:
            self._ui(say="Rapoo mic not found", hint="Plug in the Rapoo webcam (check it shows up in Windows sound "
                     "settings), then start this again. Nothing was recorded.", progress="", state="retry", finished=True)
            print("Rapoo mic not found: nothing recorded. Plug it in, or use --any-mic to record with another mic.")
            return
        print(f"Recording with: {sd.query_devices(mic)['name'] if mic is not None else 'default mic'}")
        self.downsample = Downsampler(rate, SAMPLE_RATE)
        # Continue numbering after files already there, so a second round never overwrites the first
        counters = {}
        for _, prefix, *_ in STEPS:
            existing = [int(p.stem.rsplit("_", 1)[1]) for p in SAMPLES_DIR.glob(f"*/{prefix}_*.wav")
                        if p.stem.rsplit("_", 1)[1].isdigit()]
            counters[prefix] = max(existing, default=0)
        saved = []      # (index into STEPS, path) so "Redo last" can go back one step
        with sd.InputStream(device=mic, samplerate=rate, channels=channels, dtype="int16",
                            blocksize=FRAME_SIZE * self.downsample.factor, callback=self._on_audio):
            i, spoken = 0, None
            while i < len(STEPS):
                kind, prefix, say, hint, seconds = STEPS[i]
                self._ui(say=say, hint=hint, progress=f"{i + 1} / {len(STEPS)}", state="ready")
                if FAR and kind == "positive" and hint != spoken:
                    speak(f"Next few: {hint}. Say {PHRASE} after each high beep.")
                elif FAR and kind == "negative":
                    speak(f"Not the wake word. Say: {say.strip(chr(34))}")
                spoken = hint
                time.sleep(1.3)
                # Audible cue for when you can't see the screen: high = the wake word, two low = anything else
                if kind == "positive":
                    winsound.Beep(1200, 150)
                else:
                    winsound.Beep(500, 120), winsound.Beep(500, 120)
                time.sleep(0.15)                      # let the beep die down before recording
                self._ui(state="recording")
                audio = self._record(seconds)

                if self.redo.is_set() and saved:
                    # The user asked to redo the previous take: drop it and step back
                    self.redo.clear()
                    i, path = saved.pop()
                    path.unlink(missing_ok=True)
                    counters[path.stem.rsplit("_", 1)[0]] -= 1
                    continue
                self.redo.clear()

                if kind == "positive" and np.abs(audio).max() < MIN_PEAK:
                    self._ui(hint="That was very quiet. Let's try that one again, a little louder.", state="retry")
                    time.sleep(1.5)
                    continue

                counters[prefix] = counters.get(prefix, 0) + 1
                path = SAMPLES_DIR / ("test" if TEST else kind) / f"{prefix}_{counters[prefix]:02d}.wav"
                self._save(path, audio)
                saved.append((i, path))
                self._ui(state="done")
                time.sleep(0.4)
                i += 1

        self._ui(say="All done!", hint=f"Saved {len(saved)} clips to {SAMPLES_DIR}. "
                 "You can close this window.", progress="", state="done", finished=True)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Recorder().run()
