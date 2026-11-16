"""
Max as Triton's big brain for questions: Max (the C++ assistant on this laptop, brain = Claude) answers, Triton speaks.

    maxbrain = MaxBrain.from_settings(SETTINGS.get("max", {}))      # None when off
    if maxbrain and maxbrain.is_question("why is the sky blue"):
        answer = maxbrain.ask("why is the sky blue")                # -> "Sunlight scatters...", or None

settings.json: {"max": {"enabled": true, "url": "http://127.0.0.1:8777", "token_file": "<...>\\ask.token", "timeout_s": 35}}
Max listens only on 127.0.0.1 of the laptop. POST /ask {"text": ...} with "Authorization: Bearer <token>"
-> {"answer": "..."}. The token file is read on every call (Max makes a new one when it starts) and never logged.
The answer is only ever text to say out loud: it is never parsed as commands. None on any failure, so the caller
falls back to Triton's own brains.
"""

import logging
import re
import time
from pathlib import Path

import requests

log = logging.getLogger("assistant")
DEFAULT_URL = "http://127.0.0.1:8777"
DEFAULT_TOKEN_FILE = r"C:\Users\shash\playground\max-cpp\dist\Max\data\ask.token"
CONNECT_TIMEOUT = 1.5
RETRY_AFTER = 20             # after "Max isn't there", don't try again for this long
MAX_ANSWER = 2000

# What makes something a question for Max rather than a command for Triton
_STARTERS = {"what", "what's", "whats", "who", "who's", "whom", "whose", "when", "where", "which", "why", "how",
             "how's", "explain", "tell", "describe", "define", "is", "are", "was", "were", "do", "does", "did",
             "can", "could", "would", "should", "will", "has", "have", "if", "whats"}
_COMMANDISH = re.compile(r"\b(?:open|launch|close|click|scroll|play|pause|mute|unmute|volume|brightness|"
                         r"screenshot|switch to|turn (?:on|off|up|down)|on screen|this page|this video)\b", re.I)


class MaxBrain:
    def __init__(self, url=DEFAULT_URL, token_file=DEFAULT_TOKEN_FILE, timeout_s=35):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.token_file = Path(token_file or DEFAULT_TOKEN_FILE)
        self.timeout_s = float(timeout_s or 35)
        self._down_until = 0.0

    @classmethod
    def from_settings(cls, cfg):
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return None
        log.info("Max is Triton's big brain for questions (%s)", cfg.get("url", DEFAULT_URL))
        return cls(cfg.get("url", DEFAULT_URL), cfg.get("token_file", DEFAULT_TOKEN_FILE), cfg.get("timeout_s", 35))

    def available(self):
        return time.monotonic() >= self._down_until and self.token_file.is_file()

    @staticmethod
    def is_question(text):
        """A knowledge/conversation question, not a command for the computer (those stay with Triton)."""
        words = re.findall(r"[a-z']+", (text or "").lower())
        if len(words) < 3 or _COMMANDISH.search(text):
            return False
        return words[0] in _STARTERS or text.strip().endswith("?")

    def _token(self):
        try:
            return self.token_file.read_text(encoding="ascii").strip() or None
        except (OSError, UnicodeDecodeError):
            return None

    def ask(self, question, timeout=None):
        """Max's answer as plain text, or None (off, no token, busy, not ready, slow, wrong token...)."""
        question = (question or "").strip()
        if not question or not self.available():
            return None
        token = self._token()
        if not token:
            log.warning("Max: no token file at %s", self.token_file)
            return None
        t0 = time.monotonic()
        try:
            r = requests.post(self.url + "/ask", json={"text": question},
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                              timeout=(CONNECT_TIMEOUT, timeout or self.timeout_s))
            if r.status_code != 200:
                log.warning("Max answered HTTP %s", r.status_code)
                return None
            answer = r.json().get("answer")
        except requests.ConnectionError:
            self._down_until = time.monotonic() + RETRY_AFTER
            log.warning("Max isn't running (not at %s); skipped for %d s", self.url, RETRY_AFTER)
            return None
        except (requests.RequestException, ValueError, AttributeError) as e:
            log.warning("Max failed (%s)", type(e).__name__)
            return None
        if not isinstance(answer, str) or not answer.strip():
            return None
        log.info("Max answered %r (%.1f s)", question, time.monotonic() - t0)
        return " ".join(answer.split())[:MAX_ANSWER]
