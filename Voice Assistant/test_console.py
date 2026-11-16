r"""
Triton test console: watch every step live, and type commands to test them without the ears.

    .venv\Scripts\pythonw.exe test_console.py        (or the tray icon > Test console, or "Test Console.bat")

Each request shows as a block, newest at the bottom:
    🎤 heard      what the ears made of it (or ⌨ what you typed)
    🧠 decided    who placed it (fixed rules / this PC's model / the laptop / ask again), what, and how long it took
    ⚙ did         each action and whether it worked (✓ / ✗ with the reason)
    🔊 said       what Triton said
It follows live.jsonl, which assistant.py writes (events.py). Typed commands go to the running assistant over
127.0.0.1 and are handled exactly like spoken ones (same rules, models, laptop and actions).
"""

import json
import time
import tkinter as tk
from tkinter import font as tkfont

import events

BG, PANEL, FG, DIM = "#141828", "#1d2236", "#e6e8f0", "#8a90a8"
COLOURS = {"heard": "#9ad8ff", "decided": "#c4a7ff", "ok": "#34d399", "fail": "#ff546c", "said": "#ffcc78",
           "state": "#8a90a8", "wake": "#5eead4"}
WHO = {"rules": "fixed rules", "model": "this PC's model", "laptop": "laptop big brain", "ask again": "ask again",
       "screen share": "screen share", "you": "you"}
QUICK = ["scroll down", "scroll up", "search for lofi music", "type hello world", "click subscribe",
         "open youtube", "volume up", "what time is it", "why is the sky blue"]
SHOW_LAST = 80


def short(calls):
    out = []
    for c in calls or []:
        args = " ".join(str(v) for v in (c.get("args") or {}).values())
        out.append(f"{c.get('tool')} {args}".strip())
    return ", ".join(out)


class Console:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Triton test console")
        self.root.geometry("820x640")
        self.root.configure(bg=BG)
        base = tkfont.nametofont("TkDefaultFont").actual()["family"]

        top = tk.Frame(self.root, bg=PANEL)
        top.pack(fill="x")
        self.status = tk.Label(top, text="Waiting for Triton…", bg=PANEL, fg=DIM, font=(base, 11, "bold"),
                               padx=14, pady=8, anchor="w")
        self.status.pack(side="left")
        self.media = tk.Label(top, text="", bg=PANEL, fg=COLOURS["said"], font=(base, 10), padx=14)
        self.media.pack(side="right")

        self.text = tk.Text(self.root, bg=BG, fg=FG, relief="flat", wrap="word", font=("Segoe UI", 11),
                            padx=14, pady=10, spacing1=1, spacing3=3)
        bar = tk.Scrollbar(self.root, command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set, state="disabled")
        for tag, colour in COLOURS.items():
            self.text.tag_configure(tag, foreground=colour)
        self.text.tag_configure("time", foreground=DIM, font=("Segoe UI", 9))
        self.text.tag_configure("dim", foreground=DIM)

        bottom = tk.Frame(self.root, bg=PANEL, padx=10, pady=8)
        bottom.pack(side="bottom", fill="x")
        row = tk.Frame(bottom, bg=PANEL)
        row.pack(fill="x")
        self.entry = tk.Entry(row, bg=BG, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 12))
        self.entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.entry.bind("<Return>", lambda e: self.send())
        tk.Button(row, text="Send", command=self.send, bg="#3b4470", fg=FG, relief="flat", padx=16).pack(
            side="left", padx=(8, 0))
        quick = tk.Frame(bottom, bg=PANEL)
        quick.pack(fill="x", pady=(6, 0))
        for q in QUICK:
            tk.Button(quick, text=q, command=lambda q=q: self.send(q), bg=BG, fg=DIM, relief="flat",
                      font=("Segoe UI", 9), padx=6).pack(side="left", padx=2, pady=2)
        tk.Label(bottom, text="Typed commands act on the window in front: this console hides for 2 s so the window "
                              "behind it (e.g. YouTube) gets the command.", bg=PANEL, fg=DIM, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x", pady=(4, 0))

        bar.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)

        self.pos = 0
        self.size = 0
        self._load_recent()
        self.root.after(200, self.poll)

    # ---------- sending ----------

    def send(self, text=None):
        text = (text or self.entry.get()).strip()
        if not text:
            return
        self.entry.delete(0, "end")
        # Step aside so the window behind gets the command (this console has the keyboard right now)
        self.write([("time", time.strftime("%H:%M:%S  ")), ("dim", f"sending “{text}”…\n")])
        self.root.iconify()
        self.root.after(700, lambda: self._deliver(text))

    def _deliver(self, text):
        if not events.send_typed(text):
            self.root.deiconify()
            self.write([("fail", "Triton isn't running (start it first).\n")])
            return
        self.root.after(4000, self.root.deiconify)

    # ---------- following live.jsonl ----------

    def _load_recent(self):
        if not events.FILE.exists():
            return
        lines = events.FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-SHOW_LAST:]:
            self.show(line)
        self.pos = self.size = events.FILE.stat().st_size

    def poll(self):
        try:
            size = events.FILE.stat().st_size if events.FILE.exists() else 0
            if size < self.size:            # rolled over to live.old.jsonl
                self.pos = 0
            self.size = size
            if size > self.pos:
                with events.FILE.open("rb") as f:
                    f.seek(self.pos)
                    data = f.read()
                self.pos += len(data)
                for line in data.decode("utf-8", "replace").splitlines():
                    self.show(line)
        except OSError:
            pass
        self.root.after(200, self.poll)

    def write(self, parts):
        at_end = self.text.yview()[1] > 0.98
        self.text.configure(state="normal")
        for tag, s in parts:
            self.text.insert("end", s, tag)
        self.text.configure(state="disabled")
        if at_end:
            self.text.see("end")

    def show(self, line):
        try:
            e = json.loads(line)
        except ValueError:
            return
        when = time.strftime("%H:%M:%S  ", time.localtime(e.get("t", 0)))
        kind = e.get("kind")
        if kind == "state":
            if e.get("text") == "starting":
                self.status.configure(text="⏳ Triton is starting…", fg=COLOURS["said"])
            elif e.get("text") == "ready":
                self.status.configure(text="✓ Triton is ready", fg=COLOURS["ok"])
            if "media" in e:
                self.media.configure(text="▶ media playing: stricter wake word" if e["media"] else "")
            return
        if kind == "wake":
            self.write([("time", "\n" + when), ("wake", f"👂 Hey Triton ({e.get('score')})\n")])
        elif kind == "heard":
            typed = e.get("by") == "typed"
            icon = "⌨ typed" if typed else "🎤 heard"
            extra = "" if typed or e.get("grammar") in (None, "", e.get("text")) else f"   (quick ears: “{e['grammar']}”)"
            self.write([("time", ("\n" if typed else "") + when), ("heard", f"{icon}   “{e.get('text')}”"),
                        ("dim", extra + "\n")])
        elif kind == "decided":
            who, what, payload = WHO.get(e.get("who"), e.get("who")), e.get("what"), e.get("payload")
            if what == "act":
                desc = short(payload)
            elif what == "say":
                desc = "talk back"
            elif what == "repeat":
                desc = "didn't get it: asks you to say it again"
            else:
                desc = what
            self.write([("time", when), ("decided", f"🧠 {who}: {desc}"), ("dim", f"   {e.get('sec', 0):.1f} s\n")])
        elif kind == "did":
            ok = e.get("ok")
            args = " ".join(str(v) for v in (e.get("args") or {}).values())
            self.write([("time", when), ("ok" if ok else "fail",
                        f"⚙ {e.get('tool')} {args}  {'✓' if ok else '✗'} {e.get('result')}"),
                        ("dim", f"   {e['sec']:.1f} s\n" if "sec" in e else "\n")])
        elif kind == "said":
            self.write([("time", when), ("said", f"🔊 “{e.get('text')}”\n")])

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Console().run()
