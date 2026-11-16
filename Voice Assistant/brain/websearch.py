"""
Web search for the laptop's big brain (brain_server.py), so it can answer about things newer than its training.

    results = search("who won the 2026 world cup final")     # [{"title", "body", "href"}, ...] or [] on failure
    context = as_context(results)                             # short numbered snippets for the model to read

DuckDuckGo through the ddgs package: no account or key. Only the search words leave the laptop (the model
writes a short query from what you said; your voice and the conversation stay here). Laptop only: the N95
never imports this (pip install ddgs in the laptop's .venv).
"""

import logging
import time

log = logging.getLogger("assistant")
# Kept short: every word of the results is read by the model before it answers (~3 s per 100 words on the laptop)
MAX_RESULTS = 3
SNIPPET_CHARS = 160


def search(query, n=MAX_RESULTS):
    try:
        from ddgs import DDGS
    except ImportError:
        log.warning("Web search unavailable: pip install ddgs")
        return []
    t0 = time.monotonic()
    try:
        results = DDGS(timeout=6).text(query, max_results=n) or []
    except Exception as e:                      # offline, rate-limited, blocked...
        log.warning("Web search for %r failed: %s", query, e)
        return []
    log.info("Web search %r: %d results (%.1f s)", query, len(results), time.monotonic() - t0)
    return results


def as_context(results):
    return "\n".join(f"{i}. {r.get('title', '')[:80]}:{r.get('body', '')[:SNIPPET_CHARS]}"
                     for i, r in enumerate(results, 1))
