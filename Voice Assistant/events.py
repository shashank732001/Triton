"""
Triton's live event stream, for the test console (test_console.py).

    events.emit("heard", text="scroll down", by="voice")     # one JSON line in live.jsonl
    events.listen_for_typed(queue.put)                        # commands typed in the console arrive here
    events.send_typed("open youtube")                         # (the console's side)

Kinds: state (starting / ready / media playing), wake, heard, decided (who: rules / model / laptop / ask again),
did (a tool ran: ok or the error), said. live.jsonl stays under ~1 MB (the older half moves to live.old.jsonl).
Typed commands come in over 127.0.0.1 only: nothing outside this PC can send them.
"""

import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("assistant")
# Next to the assistant (next to TritonVoice.exe in the launcher's frozen build)
FILE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent / "live.jsonl"
MAX_BYTES = 1_000_000
INBOX_PORT = 8767
_lock = threading.Lock()


def emit(kind, **data):
    line = json.dumps({"t": time.time(), "kind": kind, **data}, ensure_ascii=False, default=str)
    with _lock:
        try:
            if FILE.exists() and FILE.stat().st_size > MAX_BYTES:
                FILE.replace(FILE.with_suffix(".old.jsonl"))
            with FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def listen_for_typed(on_text):
    """Each connection to 127.0.0.1:INBOX_PORT sends one command as text; it goes to on_text(text)."""
    def serve():
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("127.0.0.1", INBOX_PORT))
            server.listen(4)
        except OSError as e:
            log.warning("Test console inbox not available (%s)", e)
            return
        while True:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                try:
                    text = conn.recv(2000).decode("utf-8", "replace").strip()
                except OSError:
                    continue
            if text:
                on_text(text)
    threading.Thread(target=serve, name="typed-commands", daemon=True).start()


def send_typed(text):
    """Hand a typed command to the running assistant. False if it isn't running."""
    try:
        with socket.create_connection(("127.0.0.1", INBOX_PORT), timeout=1) as s:
            s.sendall(text.encode("utf-8"))
        return True
    except OSError:
        return False
