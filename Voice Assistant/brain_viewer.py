r"""
The live conversation between the N95 and the laptop's big brain, as a chat window.

Opened from the big brain's tray icon ("Show conversation"), or:  .venv\Scripts\pythonw.exe brain_viewer.py
It follows conversation.log (written by brain_server.py), newest at the bottom, with a line for each day
("Today · Wed 4 Nov 2026"):
    You (N95)          what the N95 heard you say
    N95 rules / N95 model / N95 screen share / N95 ask again    what the N95 did by itself
    Laptop             what the big brain answered, did, or asked, and how long it took
The buttons at the top clear old conversation: everything before today, or all of it (also in the tray menu).
"""

import datetime as dt
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

LOG = Path(__file__).resolve().parent / "conversation.log"
BG, FG, DIM = "#141828", "#e6e8f0", "#8a90a8"
BAR_BG, BTN_BG = "#1d2236", "#2a3150"
COLOURS = {"you": "#9ad8ff", "n95": "#34d399", "laptop": "#ffcc78", "again": "#ff546c"}
SHOW_LAST = 400                      # lines shown when the window opens


def tag_for(who, text):
    if who.startswith("You"):
        return "you"
    if "again" in who or "say it again" in text:
        return "again"
    return "laptop" if who.startswith("Laptop") else "n95"


def day_label(day):
    """'Today · Wed 4 Nov 2026', 'Yesterday · Tue 3 Nov 2026', 'Mon 2 Nov 2026'."""
    text = f"{day:%a} {day.day} {day:%b %Y}"
    today = dt.date.today()
    if day == today:
        return "Today · " + text
    if day == today - dt.timedelta(days=1):
        return "Yesterday · " + text
    return text


def clear(keep_today=True):
    """Remove old conversation from conversation.log: everything before today (keep_today) or all of it.
    Returns how many lines were removed. Used by the window's buttons and the big brain's tray menu."""
    if not LOG.exists():
        return 0
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    today = dt.date.today().isoformat()
    kept = [line for line in lines if keep_today and line.startswith(today)]
    tmp = LOG.with_suffix(".tmp")
    tmp.write_text("".join(kept), encoding="utf-8")
    os.replace(tmp, LOG)            # the server opens the log only for each append, so this is safe
    return len(lines) - len(kept)


class Viewer:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Triton: N95 <-> big brain")
        self.root.geometry("760x560")
        self.root.configure(bg=BG)

        bar = tk.Frame(self.root, bg=BAR_BG)
        bar.pack(side="top", fill="x")
        for label, keep in (("Clear all", False), ("Clear older than today", True)):
            tk.Button(bar, text=label, command=lambda k=keep: self._clear(k), bg=BTN_BG, fg=FG, relief="flat",
                      activebackground="#39416a", activeforeground=FG, font=("Segoe UI", 9), padx=10, pady=3,
                      bd=0, cursor="hand2").pack(side="right", padx=(0, 8), pady=6)

        self.text = tk.Text(self.root, bg=BG, fg=FG, insertbackground=FG, relief="flat", wrap="word",
                            font=("Segoe UI", 11), padx=14, pady=10, spacing1=2, spacing3=4)
        scroll = tk.Scrollbar(self.root, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("time", foreground=DIM, font=("Segoe UI", 9))
        self.text.tag_configure("day", foreground=DIM, font=("Segoe UI Semibold", 10), justify="center",
                                spacing1=16, spacing3=6)
        for name, colour in COLOURS.items():
            self.text.tag_configure(name, foreground=colour, font=("Segoe UI Semibold", 11))
        self.text.tag_configure("gap", spacing1=12)
        self._load()
        self.root.after(500, self._poll)

    def _load(self):
        """(Re)fill the window from the log: its last SHOW_LAST lines."""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.day = None                  # the day of the last line shown (a day line goes in when it changes)
        self.pos = 0
        if LOG.exists():
            lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            self.pos = LOG.stat().st_size
            self._add(lines[-SHOW_LAST:])
        self.text.see("end")

    def _clear(self, keep_today):
        what = "everything before today" if keep_today else "the whole conversation"
        if not messagebox.askyesno("Clear conversation", f"Delete {what} from the log?\nThis can't be undone.",
                                   parent=self.root):
            return
        try:
            removed = clear(keep_today)
        except OSError as e:
            messagebox.showerror("Clear conversation", f"Couldn't clear it: {e}", parent=self.root)
            return
        self._load()
        self.root.title(f"Triton: N95 <-> big brain  (cleared {removed} lines)")

    def _add(self, lines):
        at_end = self.text.yview()[1] > 0.98
        self.text.configure(state="normal")
        for line in lines:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) != 3:
                continue
            stamp, who, said = parts
            try:
                day = dt.date.fromisoformat(stamp[:10])
            except ValueError:
                day = None
            if day and day != self.day:
                self.text.insert("end", f"{day_label(day)}\n", "day")
                self.day = day
            tag = tag_for(who, said)
            if tag == "you":
                self.text.insert("end", "\n", "gap")
            self.text.insert("end", stamp[11:] + "  ", "time")
            self.text.insert("end", who + ":  ", tag)
            self.text.insert("end", said + "\n")
        self.text.configure(state="disabled")
        if at_end:
            self.text.see("end")

    def _poll(self):
        try:
            size = LOG.stat().st_size if LOG.exists() else 0
            if size < self.pos:              # trimmed or cleared (here, from the tray, or by the server): start over
                self._load()
            elif size > self.pos:
                with LOG.open("rb") as f:
                    f.seek(self.pos)
                    chunk = f.read()
                cut = chunk.rfind(b"\n") + 1     # only whole lines; the rest next time
                self.pos += cut
                self._add(chunk[:cut].decode("utf-8", "replace").splitlines(keepends=True))
        except OSError:
            pass
        self.root.after(500, self._poll)


if __name__ == "__main__":
    Viewer().root.mainloop()
