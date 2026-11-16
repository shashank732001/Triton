r"""
Triton Screen Share by voice: "extend my laptop screen here", "mirror my laptop", "share my mic", "stop sharing".

intent(text) spots these sentences instantly (no model needed, and the fine-tuned brain was never taught them)
and returns the screen_share tool call. ScreenShare.run(action) carries it out by talking to the Triton Screen
Share bubble on this PC through its control pipe (\\.\pipe\TritonScreenShare, this user only; launcher.cpp
OnControl): one line in ("show extend"), one JSON line back (mode, picture, sound, mic, camera, paired PCs).

On the TV the laptop's screen comes HERE ("show"): the bubble starts "Show here", the laptop shares by itself
(it's set to "share automatically") or is asked to (pull), and paired PCs need no click. settings.json
"screen_share": {"role": "share"} flips it for an assistant running on the laptop: "share my screen" then
sends this screen to the TV.
"""

import json
import logging
import re
import subprocess
import time
from pathlib import Path

from commands import Failed

log = logging.getLogger("assistant")
PIPE = "//./pipe/TritonScreenShare"
ACTIONS = ["mirror", "extend", "stop", "sound_on", "sound_off", "mic_on", "mic_off", "camera_on", "camera_off",
           "hide", "unhide"]

# Whisper's spellings of the same words
_SPELLING = [(r"\bmike\b", "mic"), (r"\bmicro phone\b", "microphone"), (r"\bweb cam\b", "webcam"),
             (r"\blap top\b", "laptop"), (r"\bscreenshare\b", "screen share"), (r"\bscreen-share\b", "screen share"),
             (r"\bdon'?t\b", "dont")]
OFF = {"stop", "off", "disable", "end", "mute", "unshare", "dont", "quit", "close", "cut", "kill", "remove",
       "disconnect", "cancel"}
ON = {"share", "sharing", "on", "start", "enable", "use", "turn", "switch", "send", "give", "connect", "unmute",
      "activate", "open", "put"}
MIC = {"mic", "microphone"}
CAMERA = {"camera", "webcam", "cam"}
SOUND = {"sound", "audio"}
CONTEXT = {"laptop", "share", "sharing", "shared", "mirror", "mirroring", "extend", "extended", "extending", "cast",
           "casting", "screen"}
STOP_WORDS = {"stop", "end", "disconnect", "quit", "close", "cancel", "finish"}
STOP_WHAT = {"share", "sharing", "mirror", "mirroring", "extend", "extending", "extended", "cast", "casting",
             "laptop", "showing"}
MIRROR = {"mirror", "mirroring", "duplicate", "duplicating", "cast", "casting"}
EXTEND = {"extend", "extended", "extending"}
SECOND = re.compile(r"\b(second|dual|another|extra) (screen|monitor|display)\b")
SHOW_LAPTOP = re.compile(r"\b(show|put|bring|connect|display|open|get|see|view|start|cast)\b.*\blaptop\b")
SHARE_SCREEN = re.compile(r"\b(screen share|screen sharing|share (my |the |this |your )?(laptop|screen|desktop|display)"
                          r"|sharing (my |the |this )?(laptop|screen|desktop))\b")
UNHIDE = re.compile(r"\b(unhide|bring\b.*\bback|show\b.*\bagain)\b")


def _words(text):
    t = text.lower()
    for pattern, repl in _SPELLING:
        t = re.sub(pattern, repl, t)
    t = " ".join(re.sub(r"[^a-z' ]+", " ", t).split())
    return t, set(t.split())


def intent(text):
    """The screen_share call for a sentence about screen sharing, else None."""
    if not text:
        return None
    t, w = _words(text)

    def call(action):
        return [{"tool": "screen_share", "args": {"action": action}}]

    off = bool(w & OFF)
    for device, name in ((MIC, "mic"), (CAMERA, "camera")):
        if w & device and (w & ON or off):
            return call(name + ("_off" if off else "_on"))
    if (w & SOUND and w & (CONTEXT - {"screen"})) or (w & {"mute", "unmute"} and "laptop" in w):
        return call("sound_off" if off and "unmute" not in w else "sound_on")
    if "hide" in w and w & (CONTEXT | {"picture"}):
        return call("hide")
    if UNHIDE.search(t) and w & (CONTEXT | {"picture"}):
        return call("unhide")
    if w & STOP_WORDS and (w & STOP_WHAT or "screen share" in t):
        return call("stop")
    if w & EXTEND or SECOND.search(t):
        return call("extend")
    if w & MIRROR or SHOW_LAPTOP.search(t) or SHARE_SCREEN.search(t):
        return call("mirror")
    return None


# ---------- talking to the bubble ----------

def _send(line, timeout=6.0):
    """One command over the control pipe -> its JSON reply. None when Triton Screen Share isn't running."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(PIPE, "r+b", buffering=0) as f:
                f.write((line + "\n").encode())
                return json.loads(f.readline().decode("utf-8", "replace") or "{}")
        except FileNotFoundError:
            return None
        except OSError:                      # busy with another command: it answers within milliseconds
            if time.monotonic() > deadline:
                return None
            time.sleep(0.1)


def _start_bubble(exe):
    """Start Triton Screen Share (bubble only) if it isn't running, and wait for its pipe."""
    if not exe or not Path(exe).exists():
        return False
    log.info("Starting %s", exe)
    subprocess.Popen([str(exe), "--background"], cwd=str(Path(exe).parent), creationflags=subprocess.DETACHED_PROCESS)
    for _ in range(40):
        time.sleep(0.25)
        if _send("status", timeout=1) is not None:
            return True
    return False


PROBLEMS = {
    "not_showing": "Nothing from the laptop is on screen right now",
    "sharing_here": "This PC is sharing its own screen right now",
    "showing_here": "This PC is showing another screen right now",
    "no_screen": "I can't find the TV. Open Show here on it first",
    "no_extend_driver": "Extending needs the display driver on this PC",
    "not_running": "Screen sharing isn't on",
    "mic_unavailable": "The laptop can't take a mic yet. It needs VB-Cable installed",
    "camera_unavailable": "The laptop can't take a camera yet. Run Install-Everything there",
    "no_mic": "There's no microphone on this PC",
    "no_camera": "There's no camera on this PC",
    "waiting_for_answer": "Screen share is waiting for an answer on screen",
    "busy": "Screen share didn't answer",
}

PREVIEW = {"mirror": "Showing your laptop here", "extend": "Extending your laptop screen here",
           "stop": "Stopping screen sharing", "sound_on": "Turning the laptop sound on",
           "sound_off": "Turning the laptop sound off", "mic_on": "Sharing your mic", "mic_off": "Stopping your mic",
           "camera_on": "Sharing your camera", "camera_off": "Stopping your camera", "hide": "Hiding the laptop screen",
           "unhide": "Bringing the laptop screen back"}
DONE = {"sound_on": "Laptop sound on", "sound_off": "Laptop sound off", "mic_on": "Sharing your mic",
        "mic_off": "Stopped sharing your mic", "camera_on": "Sharing your camera",
        "camera_off": "Stopped sharing your camera", "hide": "Hid the laptop screen", "unhide": "Laptop screen is back"}


class ScreenShare:
    def __init__(self, settings, base_dir):
        cfg = settings.get("screen_share", {})
        self.role = cfg.get("role", "show")         # "show": the laptop comes here; "share": this screen goes out
        self.exe = cfg.get("exe") or Path(base_dir).parent / "display" / "TritonScreenShare.exe"
        self.wait = float(cfg.get("connect_wait", 20))

    def _command(self, line):
        reply = _send(line)
        if reply is None and _start_bubble(self.exe):
            reply = _send(line)
        if reply is None:
            raise Failed("Triton Screen Share isn't running")
        log.info("Screen share %r -> %s", line, {k: reply.get(k) for k in ("ok", "error", "mode", "session", "extended")})
        if not reply.get("ok"):
            raise Failed(PROBLEMS.get(reply.get("error"), "Screen share couldn't do that"))
        return reply

    def _wait_for(self, done):
        """Poll until done(status) or the wait runs out; returns the last status."""
        deadline = time.monotonic() + self.wait
        status = {}
        while time.monotonic() < deadline:
            status = _send("status") or {}
            if done(status) or status.get("mode") == "idle":
                break
            time.sleep(0.5)
        return status

    def run(self, action):
        if action in ("mirror", "extend"):
            if self.role == "share":
                self._command(f"share {action}")
                return "Sharing this screen" if action == "mirror" else "Extending this screen"
            self._command(f"show {action}")
            s = self._wait_for(lambda s: s.get("session") and not s.get("waiting"))
            if not s.get("session"):
                if not s.get("paired_nearby"):
                    raise Failed("I can't find your laptop. Is Triton Screen Share open on it?")
                raise Failed("Your laptop hasn't connected yet")
            if action == "extend" and not s.get("extended"):
                return "Your laptop is on screen, but it can't extend. It needs the display driver"
            return "Your laptop is extended onto this screen" if action == "extend" else "Your laptop is on screen"
        if action == "unhide" and not (_send("status") or {}).get("session"):
            return self.run("mirror")           # "show my laptop again" with nothing on screen
        if action == "stop":
            self._command("stop")
            return "Stopped screen sharing"
        thing, _, state = action.partition("_")
        self._command(action if action in ("hide", "unhide") else f"{thing} {state}")
        return DONE[action]
