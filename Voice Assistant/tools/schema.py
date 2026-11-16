"""
Every action the assistant can take, described once.

The same list drives the model's prompt, the JSON grammar its output is forced into, the eval set,
and (M2) the handlers and safety checks. A tool is a name, a one-line description, its arguments
and a risk level: "safe" runs at once, "confirm" asks first (tools/safety.py adds checks that depend
on the arguments or the screen, like a click on "Buy now" or typing into a password field).

Arguments: {name: (type, required, description)}; type is "int", "str", "bool" or a list of allowed
strings (an enum).

The model answers with {"calls": [{"tool": ..., "args": {...}}, ...]}: usually one call, a few for
"open youtube and search lofi", or one of the control tools (clarify, escalate, cancel).
"""

from dataclasses import dataclass, field


@dataclass
class Tool:
    name: str
    description: str
    args: dict = field(default_factory=dict)
    risk: str = "safe"


TOOLS = [
    # ----- sound and media -----
    Tool("set_volume", "Set the volume to a level", {"level": ("int", True, "0-100")}),
    Tool("change_volume", "Turn the volume up or down",
         {"direction": (["up", "down"], True, ""), "amount": ("int", False, "percent, only if said")}),
    Tool("mute", "Mute the sound"),
    Tool("unmute", "Turn the sound back on"),
    Tool("media", "Control music/video playback",
         {"action": (["play", "pause", "next", "previous"], True, "")}),

    # ----- apps and windows -----
    Tool("open", "Open an app or website by name", {"target": ("str", True, "app or site name")}),
    Tool("close", "Close an app, site or the current window", {"target": ("str", False, "omit for the current window")}),
    Tool("switch_to", "Bring an open app or site to the front", {"target": ("str", True, "")}),
    Tool("window", "Minimize or maximize a window",
         {"action": (["minimize", "maximize"], True, ""), "target": ("str", False, "omit for the current window")}),
    Tool("show_desktop", "Minimize everything and show the desktop"),
    Tool("go_home", "Go back to the Triton home screen"),
    Tool("show_open_apps", "Show the list of open apps"),

    # ----- browser and web -----
    Tool("browser", "Browser actions in the current window",
         {"action": (["new_tab", "close_tab", "next_tab", "previous_tab", "back", "forward", "reload"], True, "")}),
    Tool("web_search", "Search: in the site or app in front if it has a search box (Netflix, YouTube...), else Google", {"query": ("str", True, "")}),
    Tool("youtube_search", "Search YouTube", {"query": ("str", True, "")}),

    # ----- on-screen control (UI Automation) -----
    Tool("click", "Click a button, link or item on screen by its visible name", {"target": ("str", True, "")}),
    Tool("type_text", "Type text into the focused field", {"text": ("str", True, "")}),
    Tool("press_keys", "Press a key or shortcut", {"keys": ("str", True, "like enter, ctrl+t, alt+f4, space")}),
    Tool("scroll", "Scroll the current window", {"direction": (["up", "down", "top", "bottom"], True, "")}),

    # ----- system -----
    Tool("screen", "Turn the TV picture off or on", {"state": (["off", "on"], True, "")}),
    Tool("lock", "Lock the computer"),
    Tool("power", "Shut down, restart or sleep the computer",
         {"action": (["shutdown", "restart", "sleep"], True, "")}, risk="confirm"),
    Tool("info", "Say the time, date or battery level", {"what": (["time", "date", "battery"], True, "")}),
    Tool("screen_share", "Show the laptop's screen on this TV (mirror or extend), stop it, or share sound, mic, camera",
         {"action": (["mirror", "extend", "stop", "sound_on", "sound_off", "mic_on", "mic_off", "camera_on",
                      "camera_off", "hide", "unhide"], True, "")}),

    # ----- control -----
    Tool("clarify", "Ask a short question when the request is ambiguous", {"question": ("str", True, "")}),
    Tool("escalate", "Not a computer command (general question, chat): pass it to the bigger model"),
    Tool("cancel", "The user called it off (never mind, stop, cancel)"),
]

BY_NAME = {t.name: t for t in TOOLS}


def _arg_schema(spec):
    kind = spec[0]
    if isinstance(kind, list):
        return {"type": "string", "enum": kind}
    return {"type": {"int": "integer", "str": "string", "bool": "boolean"}[kind]}


def json_schema(max_calls=4):
    """JSON schema of a reply; llama.cpp turns it into a grammar so every output is a valid call list."""
    variants = []
    for t in TOOLS:
        props = {n: _arg_schema(s) for n, s in t.args.items()}
        variants.append({
            "type": "object",
            "properties": {
                "tool": {"const": t.name},
                "args": {"type": "object", "properties": props,
                         "required": [n for n, s in t.args.items() if s[1]], "additionalProperties": False},
            },
            "required": ["tool", "args"],
            "additionalProperties": False,
        })
    return {
        "type": "object",
        "properties": {"calls": {"type": "array", "items": {"anyOf": variants}, "minItems": 1, "maxItems": max_calls}},
        "required": ["calls"],
        "additionalProperties": False,
    }


def tool_lines():
    """The tool list as compact prompt lines: name(arg: type, opt?: type) - description."""
    lines = []
    for t in TOOLS:
        args = []
        for n, (kind, required, desc) in t.args.items():
            typ = "|".join(kind) if isinstance(kind, list) else kind
            args.append(f"{n}{'' if required else '?'}: {typ}" + (f" ({desc})" if desc else ""))
        lines.append(f"{t.name}({', '.join(args)}) - {t.description}")
    return "\n".join(lines)
