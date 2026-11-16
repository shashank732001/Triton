"""
What may run straight away, what needs a spoken "yes" first, and what never runs by voice.

check(call) -> ("ok", None) | ("confirm", question) | ("refuse", reason)

- the tool's own risk level (tools/schema.py): "confirm" for shutting down / restarting
- clicks on things that spend money, send, delete or install: ask first
- typing while a password field has focus: never (passwords aren't said out loud to a TV)
- a fine-tuned model can only produce valid tools (the grammar), but its arguments are still checked
"""

import re

from tools import uia
from tools.schema import BY_NAME

RISKY_CLICK = re.compile(r"\b(buy|purchase|pay|checkout|check out|order|subscribe now|send|delete|remove|"
                         r"uninstall|install|format|erase|reset|sign out|log out|confirm|accept and pay)\b", re.I)
RISKY_KEYS = {"alt+f4", "ctrl+w", "delete", "shift+delete"}


def check(call):
    tool, args = call["tool"], call.get("args") or {}
    spec = BY_NAME.get(tool)
    if spec is None:
        return "refuse", f"I don't know how to {tool}"
    if tool == "type_text" and uia.focused_is_password():
        return "refuse", "I won't type into a password box"
    if tool == "click" and RISKY_CLICK.search(args.get("target", "")):
        return "confirm", f"Click “{args['target']}”?"
    if tool == "press_keys" and args.get("keys", "").lower().replace(" ", "") in RISKY_KEYS:
        return "confirm", f"Press {args['keys']}?"
    if spec.risk == "confirm":
        action = args.get("action", tool)
        return "confirm", {"shutdown": "Shut down the computer?", "restart": "Restart the computer?",
                           "sleep": "Put the computer to sleep?"}.get(action, f"{tool.replace('_', ' ')}?")
    return "ok", None
