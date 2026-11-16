"""
Talks to Triton Launcher, when it is running, through its command pipe (one text command per line,
e.g. "open chrome", "home"). send() returns False when the launcher isn't running.
"""

import ctypes
import logging
import time

log = logging.getLogger("assistant")

PIPE = r"\\.\pipe\TritonLauncher"


def send(command):
    # The launcher serves one client at a time and re-opens the pipe after each, so retry briefly
    for _ in range(5):
        try:
            # Let the launcher bring its window (or the app it opens) to the front
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
            with open(PIPE, "w", encoding="utf-8") as pipe:
                pipe.write(command + "\n")
            log.info("Sent to Triton Launcher: %s", command)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            time.sleep(0.1)
    return False
