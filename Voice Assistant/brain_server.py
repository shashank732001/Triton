r"""
Triton's big brain on the laptop: a bigger model the N95 can ask when its own small one isn't enough.

    .venv\Scripts\python.exe brain_server.py          (or double-click "Start Big Brain.bat")

It keeps a bigger model loaded (default Qwen3.5-9B Q4: 92.6% on the 229 test commands, vs the N95's
fine-tuned 0.6B at 86%) and answers two kinds of request over the home network:
    POST /understand  {"text": "turn it up and play the next one"}  ->  {"calls": [{"tool": ..., "args": ...}]}
        tool calls for a command the N95 wasn't sure about; the N95 still carries them out itself
    POST /answer      {"text": "why is the sky blue"}               ->  {"answer": "Sunlight scatters off..."}
        a short spoken-style answer to a question that isn't a computer command
    GET  /health                                                     ->  {"ok": true, "model": ..., "ready": ...}
Every request needs the header  Authorization: Bearer <key>.  It never runs anything on the laptop:
it only reads text and sends text back.

Secured with HTTPS (TLS 1.2+): on first run it makes its own certificate (big_brain_cert.pem + its private key
big_brain_key.pem, ECDSA P-256, in %USERPROFILE%\TritonModels, outside OneDrive). The N95 doesn't use certificate authorities for it: it
"pins" this exact certificate by its SHA-256 fingerprint, so nobody else on the Wi-Fi can pose as the laptop or
read the commands, and the key only ever travels inside the encrypted connection.

big_brain.json (made on first run, next to this file; laptop only, not bundled for the N95):
    {"port": 8765, "key": "<random>", "model": "...\\Qwen3.5-9B-Q4_K_M.gguf", "threads": 4, "cert_sha256": "<hex>"}
make_n95_bundle.py copies the address, key and cert_sha256 into the N95's settings.json "big_brain" (the
one-time pairing). Still don't open the port to the internet.
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brain import websearch
from brain.llm import STORE, LocalLLM
from brain.max import MaxBrain
from brain.prompt import say_schema

BASE_DIR = Path(__file__).resolve().parent
CONFIG = BASE_DIR / "big_brain.json"
# Outside OneDrive (like the models), so the private key isn't synced to the cloud
CERT, CERT_KEY = STORE / "big_brain_cert.pem", STORE / "big_brain_key.pem"
# Qwen3-4B: ~4 s per reply in /converse (1.4 s for "say that again"). Qwen3.5-9B is a little smarter but ~9 s:
# it's a hybrid model whose prompt cache llama.cpp can't reuse, so it re-reads the tool list on every request.
DEFAULT_MODEL = STORE / "models" / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
MODELS = {"Fast: Qwen3-4B": STORE / "models" / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
          "Smarter, slower: Qwen3.5-9B": STORE / "models" / "Qwen3.5-9B-Q4_K_M.gguf"}
MAX_BODY = 4096          # a spoken command is a few hundred bytes; refuse anything bigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(BASE_DIR / "brain_server.log", encoding="utf-8"),
                              *([logging.StreamHandler(sys.stdout)] if sys.stdout else [])])   # none under pythonw
log = logging.getLogger("assistant")


def make_certificate():
    """A self-signed certificate for this laptop (made once). Its fingerprint is what the N95 pins."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Triton big brain on {socket.gethostname()}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(socket.gethostname())]), critical=False)
            .sign(key, hashes.SHA256()))
    CERT_KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("Made a new certificate: %s", CERT.name)


def cert_fingerprint():
    if not CERT.exists() or not CERT_KEY.exists():
        make_certificate()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(CERT.read_text())).hexdigest()


def load_config():
    cfg =json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    changed = not CONFIG.exists()
    if cfg.get("cert_sha256") != cert_fingerprint():
        cfg["cert_sha256"] = cert_fingerprint()
        changed = True
    for k, v in (("port", 8765), ("key", None), ("model", str(DEFAULT_MODEL)), ("threads", 4)):
        if k not in cfg:
            cfg[k] = secrets.token_hex(16) if k == "key" else v
            changed = True
    if changed:
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def lan_addresses():
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                       if not a[4][0].startswith("127.")})
    except OSError:
        return []


# What the N95 and the laptop said to each other, one line each: "time<TAB>who<TAB>text" (brain_viewer.py shows it live)
CONVERSATION = BASE_DIR / "conversation.log"
_convo_lock = threading.Lock()


def converse_log(who, text):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{who}\t{' '.join(str(text).split())}\n"
    with _convo_lock:
        try:
            if CONVERSATION.exists() and CONVERSATION.stat().st_size > 2_000_000:      # keep the newest ~1 MB
                CONVERSATION.write_text(CONVERSATION.read_text(encoding="utf-8")[-1_000_000:], encoding="utf-8")
            with CONVERSATION.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def _max_brain():
    """Max (this laptop's own assistant, localhost only) if settings.json has "max": {"enabled": true, ...}."""
    try:
        cfg = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8")).get("max", {})
    except (OSError, ValueError, AttributeError):
        return None
    return MaxBrain.from_settings(cfg)


MAX = _max_brain()


def max_answer(text, on_wait=None):
    """Max's spoken answer to a question, or None (not a question, Max off or failed): the caller then uses
    the local model as before. Max only answers; it never produces commands."""
    if not MAX or not MAX.is_question(text) or not MAX.available():
        return None
    out = {}
    worker = threading.Thread(target=lambda: out.update(a=MAX.ask(text, MAX.timeout_s)), daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(1.0)
        if on_wait and worker.is_alive():
            try:
                on_wait()                      # keeps the N95's connection open; raises if it hung up
            except OSError:
                return None
    return out.get("a")


def describe(reply):
    """A big-brain reply as one readable line for the conversation log."""
    if reply.get("repeat"):
        return "asks you to say it again"
    if "search" in reply:
        return f'searches the web for "{reply["search"]}"'
    if "say" in reply:
        return f'says: "{reply["say"]}"'
    if "answer" in reply:
        return f'says: "{reply["answer"]}"'
    return "does: " + ", ".join(c["tool"] + (" " + " ".join(str(v) for v in c["args"].values()) if c.get("args") else "")
                                for c in reply.get("calls") or [])


def searched(llm, text, query, history, on_sentence=None):
    """The model asked for a web search: search, then ask it again with the results to read (it can only talk
    now). The search query is the only thing that leaves the laptop."""
    converse_log("Laptop", f'searches the web for "{query}"')
    results = websearch.search(query)
    if not results:
        reply = {"say": "I couldn't reach the internet just now, sorry."}
        if on_sentence:
            on_sentence(reply["say"])
        return reply
    today = datetime.date.today().strftime("%A %d %B %Y")
    question = (f"{text}\n\n[Web search results for \"{query}\" (today is {today}):\n"
                f"{websearch.as_context(results)}]\n"
                "These results are newer than what you remember: trust them over your memory (anything dated "
                "before today has already happened). Answer from them in one or two short spoken sentences. "
                "If they don't say, say so.")
    if on_sentence:
        return llm.converse_stream(question, on_sentence, wait=60, history=history, schema=say_schema())
    return llm.converse(question, wait=60, history=history, schema=say_schema())


class Brain:
    """The model behind the server; the tray can stop it (frees ~8 GB), start it and restart it."""

    def __init__(self, cfg):
        self.cfg, self.llm, self.lock = cfg, None, threading.Lock()
        self.model = Path(cfg["model"])
        self.server = STORE / "llama.cpp" / "llama-server.exe"

    @property
    def state(self):
        llm = self.llm
        return "stopped" if llm is None else "ready" if llm.ready.is_set() else "loading"

    def start(self):
        with self.lock:
            if self.llm is None:
                # Stock model: full prompt (tool list + examples). One slot, so every request reuses the cached prompt.
                self.llm = LocalLLM(self.server, self.model,
                                    prompt="compact" if self.model.name.startswith("triton") else "full",
                                    threads=self.cfg["threads"], ctx=4096, slots=1, converse=True,
                                    checkpoints=True)
                llm = self.llm
                threading.Thread(target=lambda: (llm.ready.wait(), log.info("Big brain ready")), daemon=True).start()
                log.info("Loading %s", self.model.name)

    def stop(self):
        with self.lock:
            if self.llm is not None:
                self.llm.close()
                self.llm = None
                log.info("Big brain stopped")

    def restart(self):
        self.stop()
        self.start()

    def use_model(self, path):
        """Switch models (tray menu): saved in big_brain.json, then reloaded."""
        if Path(path) == self.model:
            return
        self.model = Path(path)
        self.cfg["model"] = str(path)
        CONFIG.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        log.info("Model changed to %s", self.model.name)
        converse_log("Laptop", f"switching to {self.model.stem}")
        self.restart()


class Handler(BaseHTTPRequestHandler):
    brain = None
    key = ""
    lock = threading.Lock()          # one request at a time: the model is the bottleneck anyway
    timeout = 20                     # drop connections that stall (handshake or request)
    # HTTP/1.1: the N95 keeps its encrypted connection open between requests (no new TLS handshake each time),
    # and streamed answers are sent in chunks, each sentence the moment it's written
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self):
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if hmac.compare_digest(given.encode(), self.key.encode()):
            return True
        log.warning("Refused %s: wrong or missing key", self.client_address[0])
        self._send(401, {"error": "wrong or missing key"})
        return False

    def _converse_streamed(self, llm, text, history, t0):
        """/converse with "stream": one JSON line per finished spoken sentence ({"say_part": ...}) as the model
        writes it, then {"final": reply, "ms": ...}. The N95 starts talking after the first sentence."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        first = []
        write_lock, working = threading.Lock(), threading.Event()

        def line(obj):
            data = (json.dumps(obj) + "\n").encode()
            with write_lock:
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

        def keep_alive():
            # A web search plus reading the results can take longer than the N95 waits in silence (25 s):
            # a small {"wait": 1} line every 5 s keeps the connection open (the N95 skips lines it doesn't know)
            while not working.wait(5):
                try:
                    line({"wait": 1})
                except OSError:
                    return

        def on_sentence(s):
            if not first:
                first.append(time.monotonic() - t0)
            line({"say_part": s})

        beat = [time.monotonic()]

        def heartbeat():
            # A tiny line every second while the model writes: if the N95 hung up (it gave up or was cut off), the
            # write fails and the model stops at once, instead of finishing a reply nobody waits for while the next
            # request queues behind it (was ~9 s late; the N95 skips {"wait"} lines)
            if time.monotonic() - beat[0] >= 1.0:
                beat[0] = time.monotonic()
                line({"wait": 1})

        reply = None
        answer = max_answer(text, lambda: line({"wait": 1}))
        if answer:                                       # Max answered a question: spoken as is
            on_sentence(answer)
            reply = {"say": answer}
            converse_log("Max", answer)
        else:
            with self.lock:
                reply = llm.converse_stream(text, on_sentence, wait=60, history=history, on_progress=heartbeat)
                if reply and "search" in reply:
                    on_sentence("Let me look that up.")       # said while it searches and reads
                    threading.Thread(target=keep_alive, daemon=True).start()
                    try:
                        reply = searched(llm, text, reply["search"], history, on_sentence)
                    finally:
                        working.set()
        ms = round((time.monotonic() - t0) * 1000)
        if reply is None:
            converse_log("Laptop", f"couldn't answer (model {self.brain.state})")
            line({"error": "model not ready or failed"})
        else:
            lead = f", first sentence after {first[0]:.1f} s" if first else ""
            converse_log("Laptop", f"{describe(reply)}  ({ms / 1000:.1f} s{lead})")
            line({"final": reply, "ms": ms})
        self.wfile.write(b"0\r\n\r\n")             # end of the chunks
        self.wfile.flush()

    def do_GET(self):
        if not self._authorised():
            return
        if self.path == "/health":
            self._send(200, {"ok": True, "model": self.brain.model.name, "ready": self.brain.state == "ready",
                             "state": self.brain.state})
        else:
            self._send(404, {"error": "unknown path"})

    def do_POST(self):
        if not self._authorised():
            return
        if self.path not in ("/converse", "/understand", "/answer", "/note"):
            self._send(404, {"error": "unknown path"})
            return
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BODY:
            self._send(413, {"error": f"body must be 1-{MAX_BODY} bytes"})
            return
        try:
            body = json.loads(self.rfile.read(size))
            if self.path == "/note":
                # The N95 tells what it handled by itself (rules, its own model, "say that again"): for the log only
                converse_log("You (N95)", str(body.get("heard", ""))[:300])
                converse_log(f"N95 {str(body.get('by', ''))[:20]}", str(body.get("did", ""))[:300])
                self._send(200, {"ok": True})
                return
            text = str(body["text"]).strip()
            # Earlier turns of this conversation from the N95 (optional): [{"user": ..., "reply": ...}, ...]
            history = [{"user": str(t["user"])[:300], "reply": str(t["reply"])[:300]}
                       for t in (body.get("history") or [])[-6:] if isinstance(t, dict)]
        except (ValueError, KeyError, TypeError, AttributeError):
            self._send(400, {"error": 'send JSON like {"text": "..."}'})
            return
        if not text:
            self._send(400, {"error": "empty text"})
            return
        llm = self.brain.llm
        if llm is None:
            self._send(503, {"error": "big brain stopped"})
            return
        converse_log("You (N95)", text)
        t0 = time.monotonic()
        if self.path == "/converse" and body.get("stream"):
            self._converse_streamed(llm, text, history, t0)
            return
        answer = max_answer(text) if self.path == "/converse" else None
        if answer:
            converse_log("Max", answer)
        with self.lock:
            if answer:
                reply = {"say": answer}
            elif self.path == "/converse":
                reply = llm.converse(text, wait=60, history=history)
                if reply and "search" in reply:
                    reply = searched(llm, text, reply["search"], history)
            elif self.path == "/understand":
                result = llm.ask(text, wait=60, history=history)
                reply = {"calls": result} if result is not None else None
            else:
                result = llm.answer(text, wait=60, history=history)
                reply = {"answer": result} if result else None
        ms = round((time.monotonic() - t0) * 1000)
        if reply is None:
            converse_log("Laptop", f"couldn't answer (model {self.brain.state})")
            self._send(503, {"error": "model not ready or failed"})
            return
        converse_log("Laptop", f"{describe(reply)}  ({ms / 1000:.1f} s)")
        reply["ms"] = ms
        self._send(200, reply)


class ExclusiveServer(ThreadingHTTPServer):
    # On Windows, address reuse lets a second server bind the same port silently: two 9B models in 16 GB,
    # and requests landing on either. Fail instead.
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # A client that doesn't trust us (wrong pinned certificate) or a port scan: one line, not a traceback
        err = sys.exc_info()[1]
        if isinstance(err, (ssl.SSLError, ConnectionError, TimeoutError)):
            log.warning("%s dropped the connection (%s)", client_address[0], type(err).__name__)
        else:
            super().handle_error(request, client_address)


def open_viewer():
    """The live conversation window (brain_viewer.py), as its own windowless process."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), str(BASE_DIR / "brain_viewer.py")],
                     cwd=str(BASE_DIR), creationflags=subprocess.CREATE_NO_WINDOW)


def run_tray(brain):
    """Tray icon: green = ready, amber = loading, grey = stopped. Menu: conversation, log, start/stop/restart, quit."""
    import pystray
    from PIL import Image, ImageDraw

    colours = {"ready": (52, 211, 153), "loading": (255, 204, 120), "stopped": (140, 140, 150)}

    def image(state):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=colours[state] + (255,))
        d.ellipse((20, 20, 44, 44), fill=(20, 24, 40, 255))
        return img

    def title():
        return f"Triton big brain: {brain.state} ({brain.model.stem})"

    def clear_old(icon):
        # Same as the conversation window's "Clear older than today" (an open window reloads by itself)
        from brain_viewer import clear
        with _convo_lock:
            try:
                removed = clear(keep_today=True)
            except OSError as e:
                icon.notify(f"Couldn't clear it: {e}", "Triton big brain")
                return
        icon.notify(f"Removed {removed} older lines; today's conversation is kept.", "Triton big brain")

    icon = pystray.Icon("TritonBigBrain", image(brain.state), title(), menu=pystray.Menu(
        pystray.MenuItem(lambda item: title(), None, enabled=False),
        pystray.MenuItem("Show conversation", lambda icon, item: open_viewer(), default=True),
        pystray.MenuItem("Open full log", lambda icon, item: os.startfile(BASE_DIR / "brain_server.log")),
        pystray.MenuItem("Clear old conversation (keep today)", lambda icon, item: clear_old(icon)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start", lambda icon, item: brain.start(), visible=lambda item: brain.state == "stopped"),
        pystray.MenuItem("Stop (frees its memory)", lambda icon, item: brain.stop(),
                         visible=lambda item: brain.state != "stopped"),
        pystray.MenuItem("Restart", lambda icon, item: threading.Thread(target=brain.restart, daemon=True).start()),
        pystray.MenuItem("Model", pystray.Menu(*[
            pystray.MenuItem(name, (lambda p: lambda icon, item: threading.Thread(
                target=brain.use_model, args=(p,), daemon=True).start())(path),
                checked=(lambda p: lambda item: brain.model == p)(path), radio=True)
            for name, path in MODELS.items() if path.exists()])),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit big brain", lambda icon, item: icon.stop()),
    ))

    def watch():                                     # keep the colour and tooltip in step with the model
        last = None
        while True:
            state = brain.state
            if state != last:
                icon.icon, icon.title, last = image(state), title(), state
                icon.update_menu()
            time.sleep(1)

    threading.Thread(target=watch, daemon=True).start()
    icon.run()


def main():
    cfg = load_config()
    brain = Brain(cfg)
    if not brain.model.exists() or not brain.server.exists():
        sys.exit(f"Missing model {brain.model} or llama-server {brain.server}")
    Handler.brain, Handler.key = brain, cfg["key"]
    try:
        httpd = ExclusiveServer(("0.0.0.0", cfg["port"]), Handler)
    except OSError:
        sys.exit(f"Port {cfg['port']} is already in use: is another big brain running? Quit it from its tray icon.")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(CERT, CERT_KEY)
    # The TLS handshake runs on each request's own thread (first read), so a slow client can't hold up accept()
    httpd.socket = tls.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    brain.start()
    print(f"\nTriton big brain: {brain.model.name}, https port {cfg['port']}")
    print(f"  laptop addresses: {', '.join(lan_addresses()) or '?'}   (use the one on the N95's network)")
    print(f"  certificate SHA-256 (the N95 pins it): {cfg['cert_sha256']}")
    print("Loading the model (about a minute)... Tray icon: conversation, start/stop, quit.\n", flush=True)
    converse_log("Laptop", f"big brain started ({brain.model.stem})")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        if "--no-tray" in sys.argv:
            while True:
                time.sleep(3600)
        else:
            run_tray(brain)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        brain.stop()
        converse_log("Laptop", "big brain quit")


if __name__ == "__main__":
    main()
