"""
The N95's line to the laptop's big brain (brain_server.py): a bigger model over the home network.

    remote = RemoteBrain.from_settings(SETTINGS.get("big_brain", {}))      # None when not set up
    remote.understand("bring up spotify and skip this song")   # -> tool calls, or None
    remote.answer("why is the sky blue")                        # -> "Sunlight scatters...", or None

settings.json: {"big_brain": {"url": "https://192.168.0.204:8765", "key": "<from big_brain.json on the laptop>",
                              "cert_sha256": "<the laptop's certificate fingerprint>",
                              "urls": ["https://<laptop name>:8765"], "mode": "first", "timeout": 25}}
  (make_n95_bundle.py writes all of it.) HTTPS with the laptop's own certificate pinned: the connection is
  refused unless the laptop shows exactly that certificate, so the key and commands only go to the real laptop,
  encrypted. "urls" are tried after "url" (the laptop's name, for when its address changed).
  mode "first":    ask the laptop before anything else (the fixed rules and the N95's own model are the backup)
  mode "fallback": only when the N95's own rules and model can't place a command
If the laptop doesn't answer (off, asleep, other network) it's skipped for a minute, so a dead laptop costs
~1.5 s once, not on every command.
"""

import json
import logging
import threading
import time
from collections import deque

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("assistant")
CONNECT_TIMEOUT = 1.5        # the laptop is on the same network: it answers in milliseconds or not at all
RETRY_AFTER = 60             # after a failure, don't try again for this long
HISTORY_TURNS = 6            # earlier turns of this conversation sent with each request


class PinnedCertificate(HTTPAdapter):
    """Trust exactly one certificate (by SHA-256 fingerprint) instead of certificate authorities: the laptop's
    certificate is its own, self-made one. urllib3 checks the fingerprint during the handshake, before any
    request (and its key) is sent."""

    def __init__(self, sha256):
        self.sha256 = sha256.replace(":", "").lower()
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_fingerprint"] = self.sha256
        super().init_poolmanager(*args, **kwargs)


class RemoteBrain:
    def __init__(self, url, key, mode="first", timeout=25, cert_sha256=None, urls=()):
        self.urls = [u.rstrip("/") for u in [url, *urls] if u]
        self.mode, self.timeout = mode, timeout
        self.headers = {"Authorization": f"Bearer {key}"}
        self.session = requests.Session()
        self.pinned = bool(cert_sha256)
        if self.pinned:
            self.session.mount("https://", PinnedCertificate(cert_sha256))
        elif any(u.startswith("http://") for u in self.urls):
            log.warning("Big brain over plain HTTP: commands and the key cross the Wi-Fi unencrypted")
        self._down_until = 0.0
        # This conversation so far (what you said, what Triton did or answered), sent along so follow-ups
        # make sense: "how far is the moon" ... "and mars?"; cleared when the conversation ends
        self.history = deque(maxlen=HISTORY_TURNS)

    def remember(self, said, reply):
        self.history.append({"user": said[:300], "reply": (reply or "")[:300]})

    def forget(self):
        self.history.clear()

    @classmethod
    def from_settings(cls, cfg):
        if not cfg.get("url") or not cfg.get("key") or not cfg.get("enabled", True):
            return None
        if cfg["url"].startswith("https://") and not cfg.get("cert_sha256"):
            log.error("Big brain: https without cert_sha256 can't be checked; rebuild the bundle on the laptop")
            return None
        log.info("Big brain on the laptop: %s (%s%s)", cfg["url"], cfg.get("mode", "first"),
                 ", pinned certificate" if cfg.get("cert_sha256") else "")
        return cls(cfg["url"], cfg["key"], cfg.get("mode", "first"), cfg.get("timeout", 25),
                   cfg.get("cert_sha256"), cfg.get("urls", ()))

    @property
    def available(self):
        return time.monotonic() >= self._down_until

    def _post(self, path, text):
        if not self.available:
            return None
        t0 = time.monotonic()
        reply = None
        for url in list(self.urls):
            try:
                # verify=False: CA checks off, because the pinned fingerprint replaces them (PinnedCertificate)
                r = self.session.post(url + path, json={"text": text, "history": list(self.history)},
                                      headers=self.headers, timeout=(CONNECT_TIMEOUT, self.timeout),
                                      verify=not self.pinned)
                if r.status_code == 401:
                    log.error("Big brain refused the key: rebuild the N95 bundle on the laptop")
                r.raise_for_status()
                reply = r.json()
            except requests.exceptions.SSLError as e:
                log.error("Big brain at %s showed the wrong certificate (%s): not sending anything", url, e)
                continue
            except requests.ConnectionError as e:
                log.warning("Big brain not at %s (%s)", url, type(e).__name__)
                continue
            except (requests.RequestException, ValueError) as e:
                log.warning("Big brain at %s failed (%s)", url, type(e).__name__)
                reply = None
                break
            if url != self.urls[0]:              # the laptop moved: try this address first from now on
                self.urls.remove(url)
                self.urls.insert(0, url)
            break
        if reply is None:
            self._down_until = time.monotonic() + RETRY_AFTER
            log.warning("Big brain unreachable; using the N95's own brain for %d s", RETRY_AFTER)
            return None
        log.info("Big brain %s %r -> %s (%.1f s)", path, text, reply, time.monotonic() - t0)
        return reply

    def cancel(self):
        """Give up on the request in flight. Only a flag: the reading thread sees it at the next line (the server sends
        a {"wait"} line every second while it writes) and hangs up itself; the laptop's next write then fails and its
        model stops, so it's free for the next request.
        Never close the connection from here: closing a TLS socket while another thread is reading it corrupted the
        heap and crashed the assistant (pythonw 0xc0000374, 2026-11-04 11:35:54)."""
        self._cancelled = True

    def _post_stream(self, path, text, on_sentence):
        """POST with "stream": each {"say_part"} line goes to on_sentence as it arrives; returns the final reply."""
        if not self.available:
            return None
        t0 = time.monotonic()
        url = self.urls[0]
        spoken = []
        self._cancelled = False
        try:
            with self.session.post(url + path, json={"text": text, "history": list(self.history), "stream": True},
                                   headers=self.headers, timeout=(CONNECT_TIMEOUT, self.timeout),
                                   verify=not self.pinned, stream=True) as r:
                self._live = r
                if r.status_code == 401:
                    log.error("Big brain refused the key: rebuild the N95 bundle on the laptop")
                r.raise_for_status()
                for line in r.iter_lines(chunk_size=None, decode_unicode=True):    # each chunk as it arrives
                    if self._cancelled:
                        break                            # leaving the with-block closes it, in this thread
                    if not line:
                        continue
                    msg = json.loads(line)
                    if "say_part" in msg:
                        spoken.append(msg["say_part"])
                        on_sentence(msg["say_part"])
                    elif "final" in msg:
                        log.info("Big brain %s %r -> %s (%.1f s, streamed)", path, text, msg["final"], time.monotonic() - t0)
                        return msg["final"]
                    elif "say" in msg or "calls" in msg or "repeat" in msg:
                        return msg                       # an older server that doesn't stream
        except requests.exceptions.SSLError as e:
            log.error("Big brain at %s showed the wrong certificate (%s): not sending anything", url, e)
        except Exception as e:
            if self._cancelled:                  # we hung up on purpose: not a failure, and don't ask again
                log.info("Big brain request cancelled after %.1f s", time.monotonic() - t0)
                return None
            if not isinstance(e, (requests.RequestException, ValueError)):
                raise
            log.warning("Big brain at %s failed (%s)", url, type(e).__name__)
        finally:
            self._live = None
        if self._cancelled:
            return None
        if spoken:                               # broke off mid-answer: keep what was said, don't say it twice
            return {"say": " ".join(spoken)}
        # Not reached that way (another address): try the normal request, which also tries the other addresses
        # and marks the laptop as away for a minute if it really is
        return self._post(path, text)

    def converse(self, text, on_sentence=None):
        """The laptop decides: {"calls": [...]} (do it), {"say": "..."} (talk) or {"repeat": true} (misheard).
        With on_sentence, a spoken reply arrives sentence by sentence while it's written (the returned "say" is
        then the whole of it, already passed on). None when it can't be reached."""
        reply = self._post_stream("/converse", text, on_sentence) if on_sentence else self._post("/converse", text)
        if not reply:
            return None
        if isinstance(reply.get("calls"), list) and reply["calls"]:
            return {"calls": reply["calls"]}
        if isinstance(reply.get("say"), str) and reply["say"].strip():
            return {"say": reply["say"].strip()}
        return {"repeat": True}

    def note(self, heard, by, did):
        """Tell the laptop what this device handled by itself, for its conversation log (fire and forget)."""
        if not self.available or not heard:
            return

        def send():
            try:
                self.session.post(self.urls[0] + "/note", json={"heard": heard, "by": by, "did": did},
                                  headers=self.headers, timeout=(CONNECT_TIMEOUT, 5), verify=not self.pinned)
            except requests.RequestException:
                pass
        threading.Thread(target=send, daemon=True).start()

    def understand(self, text):
        reply = self._post("/understand", text)
        calls = reply.get("calls") if reply else None
        return calls if isinstance(calls, list) and calls else None

    def answer(self, text):
        reply = self._post("/answer", text)
        return (reply or {}).get("answer") or None
