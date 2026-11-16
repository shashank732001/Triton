r"""
Tier 2 of the router: a small local model in llama.cpp's llama-server, kept running in the background.

    llm = LocalLLM.from_settings(settings.get("brain", {}), base_dir)   # None if no model is set up
    calls = llm.ask("click the subscribe button")    # [{"tool": "click", "args": {"target": "subscribe"}}]

settings.json, "brain" (all optional):
    "model":   path to a .gguf (default: models\triton-mini.gguf next to the assistant, else
               %USERPROFILE%\TritonModels\models\Qwen3-1.7B-Q4_K_M.gguf)
    "prompt":  "full" for a stock model (tool list + examples), "compact" for a fine-tuned Triton-Mini,
               "converse-mini" for Triton-Mini v2, "converse-full" for a stock model deciding act / say / repeat
    "checkpoints": true for hybrid models (Qwen3.5, LFM3), so the system prompt stays cached
    "threads": CPU threads (default 4)
    "server":  path to llama-server.exe (default: llama.cpp\ next to the assistant, else TritonModels)
The answer is forced into the tool-call grammar, so it's always a valid list of calls. The server
starts in the background and reads the system prompt once (cached), so later requests are quick.
"""

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests

from brain.prompt import COMPACT_CONVERSE, converse_prompt, converse_schema, mini_schema, system_prompt
from tools.schema import json_schema

log = logging.getLogger("assistant")
STORE = Path(os.environ.get("TRITON_MODELS", Path.home() / "TritonModels"))
# For answer(): it's read out loud, so short and plain
ANSWER_PROMPT = ("You are Triton, a voice assistant on a TV computer. Answer in one or two short spoken sentences, "
                 "the answer first: no lists, no markdown, no emojis, no offers of further help. "
                 "If you don't know, say so briefly.")


def partial_say(content):
    """The text of {"say": "..."} written so far, while the JSON is still being written; None for other replies."""
    m = re.match(r'\s*\{\s*"say"\s*:\s*"', content)
    if not m:
        return None
    out, i = [], m.end()
    while i < len(content):
        c = content[i]
        if c == '"':
            break
        if c == "\\":
            if i + 1 >= len(content):
                break                                          # escape not finished yet
            nxt = content[i + 1]
            if nxt == "u":
                if i + 6 > len(content):
                    break
                out.append(chr(int(content[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append({"n": " ", "t": " "}.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def sentences_done(text):
    """Where the last finished sentence in text ends (0 if none yet): a . ! or ? followed by a space."""
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", text)]
    return ends[-1] if ends else 0


class LocalLLM:
    def __init__(self, server, model, prompt="full", threads=4, ctx=2048, slots=1, converse=False, draft=None,
                 checkpoints=False):
        self.server, self.model = Path(server), Path(model)
        # "converse-mini": Triton-Mini v2, trained to act / talk / ask again (decide()); else tool calls only (ask())
        # "converse-full": the same act / say / repeat decision for a stock model that was never trained on it, so it
        # gets the laptop's full instructions + examples (2026-11-06: to compare new base models, e.g. LFM3, Qwen3.5)
        self.mini = prompt in ("converse-mini", "converse-full")
        self.mini_system = converse_prompt() if prompt == "converse-full" else COMPACT_CONVERSE
        self.system = self.mini_system if self.mini else system_prompt(prompt)
        self.schema = json_schema()
        # converse (brain_server.py): the one-request conversational brain (act / say / repeat) is warmed up
        # instead of the tool-only prompt
        self.converse_mode = converse
        # draft: a small model with the same vocabulary that guesses a few tokens ahead, which the big one checks
        # in one go (speculative decoding: same answers, written faster)
        self.draft = Path(draft) if draft else None
        # checkpoints: save the model's state every 256 tokens while it reads the prompt. Hybrid models (Qwen3.5)
        # can't rewind their memory, so without these they re-read the whole ~1200-token system prompt on every
        # request (~70 s for the 9B); with them only the new words are read
        self.checkpoints = checkpoints
        self.converse_system, self.converse_schema = converse_prompt(), converse_schema()
        # slots > 1 (brain_server.py): commands and free-text answers each keep their own cached prompt,
        # instead of re-reading the long tool prompt after every question. ctx is shared between slots.
        self.threads, self.ctx, self.slots = threads, ctx * slots, slots
        self.ready = threading.Event()
        self.proc = None
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._start, name="llm", daemon=True).start()

    @classmethod
    def from_settings(cls, cfg, base_dir):
        def first(*paths):
            return next((Path(p) for p in paths if p and Path(p).exists()), None)

        model = first(cfg.get("model"), base_dir / "models" / "triton-mini.gguf",
                      STORE / "models" / "Qwen3-1.7B-Q4_K_M.gguf")
        server = first(cfg.get("server"), base_dir / "llama.cpp" / "llama-server.exe",
                       STORE / "llama.cpp" / "llama-server.exe")
        if not model or not server:
            log.info("No local model (model %s, server %s): only the rule commands work", model, server)
            return None
        prompt = cfg.get("prompt", "converse-mini" if "converse" in model.name else
                         "compact" if model.name.startswith("triton") else "full")
        log.info("Local model %s (%s prompt)", model.name, prompt)
        # checkpoints: needed by hybrid models (Qwen3.5, LFM3) to keep the system prompt cached between requests
        return cls(server, model, prompt, cfg.get("threads", 4), checkpoints=bool(cfg.get("checkpoints")))

    def _start(self):
        t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [str(self.server), "-m", str(self.model), "-t", str(self.threads), "-c", str(self.ctx), "-np", str(self.slots),
             "--port", str(self.port), "--jinja", "--no-webui",
             *(["-md", str(self.draft), "--spec-draft-n-max", "8", "--spec-draft-n-min", "1"] if self.draft else []),
             *(["-ctxcp", "32", "-cms", "256"] if self.checkpoints else [])],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # The laptop's big brain runs below normal priority: when the voice assistant runs on the same PC, its
            # ears and small model must win the 4 cores (2026-11-03: with both busy, Whisper took 6.7 s instead
            # of 1.6 s and the small model 15 s instead of 0.5 s). It still gets every idle cycle.
            creationflags=subprocess.CREATE_NO_WINDOW | (subprocess.BELOW_NORMAL_PRIORITY_CLASS if self.converse_mode else 0))
        while self.proc.poll() is None:
            try:
                if requests.get(self.url + "/health", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            log.error("llama-server exited (code %s); the model is unavailable", self.proc.returncode)
            return
        # Read and cache the system prompt (and the answer prompt, in the other slot). A big model on this CPU
        # needs a minute or more for the full prompt, so give it time; if it still fails, the first real request
        # pays instead, but the model must never stay "not ready" because of the warm-up.
        try:
            if self.converse_mode:
                self._converse("hello", timeout=600)
            elif self.mini:
                self._decide("hello")
            else:
                self._ask("hello", timeout=600)
                if self.slots > 1:
                    self._chat("hello", ANSWER_PROMPT, 1)
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Warm-up failed (%s); going on without it", e)
        log.info("Local model ready in %.1f s", time.monotonic() - t0)
        self.ready.set()

    @staticmethod
    def _messages(system, text, history=()):
        """System prompt, earlier turns of this conversation ({"user", "reply"}), then what was just said."""
        msgs = [{"role": "system", "content": system}]
        for turn in history:
            msgs += [{"role": "user", "content": turn["user"]}, {"role": "assistant", "content": turn["reply"]}]
        return msgs + [{"role": "user", "content": text}]

    def _ask(self, text, history=(), timeout=60):
        body = {
            "messages": self._messages(self.system, text, history),
            "temperature": 0, "max_tokens": 200, "cache_prompt": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "calls", "schema": self.schema}},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        r = requests.post(self.url + "/v1/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])["calls"]

    def ask(self, text, wait=20, history=()):
        """Tool calls for `text`, or None if the model isn't ready or something went wrong."""
        if not self.ready.wait(timeout=wait):
            log.warning("Local model not ready yet")
            return None
        t0 = time.monotonic()
        try:
            calls = self._ask(text, history)
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Local model failed: %s", e)
            return None
        log.info("Model: %r -> %s (%.0f ms)", text, json.dumps(calls), (time.monotonic() - t0) * 1000)
        return calls

    def _chat(self, text, system, max_tokens, history=()):
        body = {"messages": self._messages(system, text, history),
                "temperature": 0.3, "max_tokens": max_tokens, "cache_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False}}
        r = requests.post(self.url + "/v1/chat/completions", json=body, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def _converse(self, text, history=(), timeout=90, schema=None):
        body = {
            "messages": self._messages(self.converse_system, text, history),
            "temperature": 0.2, "max_tokens": 160, "cache_prompt": True,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "reply", "schema": schema or self.converse_schema}},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        r = requests.post(self.url + "/v1/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])

    def converse_stream(self, text, on_sentence, wait=20, history=(), schema=None, on_progress=None):
        """Like converse(), but when the reply is {"say": ...} each finished sentence goes to on_sentence(s) the
        moment it's written, so the N95 can start talking while the rest is still being written."""
        if not self.ready.wait(timeout=wait):
            log.warning("Local model not ready yet")
            return None
        t0 = time.monotonic()
        body = {
            "messages": self._messages(self.converse_system, text, history),
            "temperature": 0.2, "max_tokens": 160, "cache_prompt": True, "stream": True,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "reply", "schema": schema or self.converse_schema}},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        content, sent = "", 0
        try:
            with requests.post(self.url + "/v1/chat/completions", json=body, timeout=90, stream=True) as r:
                r.raise_for_status()
                r.encoding = "utf-8"           # llama-server sends UTF-8 without saying so: "—" came out as "â€”"
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    content += json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
                    if on_progress:
                        on_progress()          # brain_server: notices a client that hung up (raises), which stops this
                    said = partial_say(content)
                    if said is not None:
                        done = sentences_done(said)
                        if done > sent:
                            on_sentence(said[sent:done].strip())
                            sent = done
            reply = json.loads(content)
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            log.warning("Local model failed to converse: %s", e)
            return None
        if "say" in reply and reply["say"][sent:].strip():
            on_sentence(reply["say"][sent:].strip())         # the last sentence (no full stop needed)
        log.info("Converse (streamed): %r -> %s (%.0f ms)", text, json.dumps(reply), (time.monotonic() - t0) * 1000)
        return reply

    def converse(self, text, wait=20, history=(), schema=None):
        """One of {"calls": [...]}, {"say": "..."}, {"repeat": true} or {"search": "..."}; None if not ready or
        failed. schema: allow only some forms (say_schema() after a search)."""
        if not self.ready.wait(timeout=wait):
            log.warning("Local model not ready yet")
            return None
        t0 = time.monotonic()
        try:
            reply = self._converse(text, history, schema=schema)
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Local model failed to converse: %s", e)
            return None
        log.info("Converse: %r -> %s (%.0f ms)", text, json.dumps(reply), (time.monotonic() - t0) * 1000)
        return reply

    def decide(self, text, history=(), wait=20):
        """Triton-Mini v2 (settings brain "prompt": "converse-mini"): {"calls"}, {"say"} or {"repeat"} for what was
        said (screen line first, as for the laptop). history: [{"user", "reply"}], earlier turns; replies go in as
        {"say": ...}, exactly as in training. None if not ready or failed."""
        if not self.ready.wait(timeout=wait):
            return None
        return self._decide(text, history)

    def _decide(self, text, history=()):
        msgs = [{"role": "system", "content": self.mini_system}]
        for turn in history:
            msgs += [{"role": "user", "content": turn["user"]},
                     {"role": "assistant", "content": json.dumps({"say": turn["reply"]}, separators=(",", ":"))}]
        msgs.append({"role": "user", "content": text})
        body = {"messages": msgs, "temperature": 0, "max_tokens": 160, "cache_prompt": True,
                "response_format": {"type": "json_schema", "json_schema": {"name": "reply", "schema": mini_schema()}},
                "chat_template_kwargs": {"enable_thinking": False}}
        t0 = time.monotonic()
        try:
            r = requests.post(self.url + "/v1/chat/completions", json=body, timeout=60)
            r.raise_for_status()
            reply = json.loads(r.json()["choices"][0]["message"]["content"])
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Local model failed to decide: %s", e)
            return None
        log.info("Mini: %r -> %s (%.0f ms)", text, json.dumps(reply), (time.monotonic() - t0) * 1000)
        return reply

    def answer(self, text, system=ANSWER_PROMPT, max_tokens=90, wait=20, history=()):
        """A short free-text reply (no tool grammar), for questions that aren't commands. None on failure."""
        if not self.ready.wait(timeout=wait):
            return None
        t0 = time.monotonic()
        try:
            reply = self._chat(text, system, max_tokens, history)
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Local model failed to answer: %s", e)
            return None
        log.info("Answer: %r -> %r (%.0f ms)", text, reply, (time.monotonic() - t0) * 1000)
        return reply

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
