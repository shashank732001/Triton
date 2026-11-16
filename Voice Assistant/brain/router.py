"""
Decides who understands a sentence: the rules (instant) or the local model (about a second).

    router.rules(text)  -> calls if the rule parser is confident, else None
    router.model(text)  -> calls from the local model, else None (no model, not ready, failed)

The assistant tries rules on what the command-grammar recogniser heard, and only if that isn't
confident shows "Thinking…" and asks the model about what the free recogniser heard.
"""

import logging

from brain.rules import confident, known_words, to_calls

log = logging.getLogger("assistant")


class Router:
    def __init__(self, commands, catalog, llm=None, backup=False):
        # backup: something smarter than the rules exists even without a local model (the laptop's big brain),
        # so unsure rule matches are handed on instead of trusted
        self.commands, self.catalog, self.llm, self.backup = commands, catalog, llm, backup
        self._known = None

    def rules(self, text):
        if not text:
            return None
        cmd, req = self.commands.understand(text)
        if not cmd:
            return None
        if self._known is None and self.catalog.ready.is_set():
            self._known = known_words(self.catalog)
        calls = to_calls(cmd, req)
        if self.llm is None and not self.backup:          # nothing better to ask: keep today's behaviour
            return calls or None
        if confident(calls, req, self.catalog, self._known or set()):
            return calls
        log.info("Rules not sure about %r (%s); asking the model", text, calls)
        return None

    def model(self, text):
        if self.llm is None or not text:
            return None
        return self.llm.ask(text)
