"""
Is a list of tool calls the right answer? Shared by bench.py (laptop) and training/evaluate.py
(cluster), so both score exactly the same way. Plain Python, no Windows-only imports.
"""

import re


def norm(value):
    s = re.sub(r"[^\w+ ]", " ", str(value).lower())
    s = " ".join(s.split())
    return s[4:] if s.startswith("the ") else s


def norm_keys(value):
    s = norm(value).replace(" plus ", "+").replace("control", "ctrl").replace("escape", "esc")
    return re.sub(r"\s*\+\s*", "+", s).replace(" ", "+")


def arg_ok(tool, name, want, got):
    if want == "*":
        return got not in (None, "")
    if isinstance(want, list):
        return any(arg_ok(tool, name, w, got) for w in want)
    if isinstance(want, int):
        try:
            return int(got) == want
        except (TypeError, ValueError):
            return False
    if tool == "press_keys":
        return norm_keys(want) == norm_keys(got)
    return norm(want) == norm(got)


def call_ok(want, got):
    if want["tool"] != got.get("tool"):
        return False
    w, g = want["args"], got.get("args") or {}
    return set(w) == set(g) and all(arg_ok(want["tool"], k, w[k], g[k]) for k in w)


def calls_ok(want, got):
    return len(want) == len(got) and all(call_ok(w, g) for w, g in zip(want, got))


def score(item, calls):
    """(exactly right, right tools) for an eval item and a model's calls."""
    options = [item["expected"], *item.get("also_ok", [])]
    names = [c.get("tool") for c in calls]
    return (any(calls_ok(o, calls) for o in options),
            any([c["tool"] for c in o] == names for o in options))
