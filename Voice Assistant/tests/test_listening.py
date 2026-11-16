"""Offline tests of continuous listening (audio_hub.py, the decision made while listening) and the false-wake checks
(wake_check.py). No mic, no models. Run: .venv\\Scripts\\python.exe tests\\test_listening.py"""
import re
import sys
import threading
import time
import unittest
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wake_check  # noqa: E402
from audio_hub import AudioHub  # noqa: E402


def frame(i):
    return np.full(1280, i, dtype=np.int16)


class HubTest(unittest.TestCase):
    def test_readers_are_independent_and_nothing_is_lost(self):
        hub = AudioHub(keep_sec=10)
        a, b = hub.reader(), hub.reader()
        for i in range(5):
            hub.push(frame(i))
        self.assertEqual([int(a.next(0)[0]) for _ in range(5)], [0, 1, 2, 3, 4])
        self.assertEqual(int(b.next(0)[0]), 0)            # b reads at its own pace
        self.assertIsNone(a.next(0.01))                    # nothing new yet

    def test_rewind_seek_and_skip(self):
        hub = AudioHub(keep_sec=10)
        r = hub.reader()
        for i in range(10):
            hub.push(frame(i))
        r.skip_to_now()
        r.rewind(0.24)                                     # 3 frames of 80 ms
        self.assertEqual(int(r.next(0)[0]), 7)
        r.seek(2)
        self.assertEqual(int(r.next(0)[0]), 2)

    def test_reader_that_fell_behind_jumps_to_the_oldest_kept(self):
        hub = AudioHub(keep_sec=0.8)                       # 10 frames kept
        r = hub.reader()
        for i in range(25):
            hub.push(frame(i))
        self.assertEqual(int(r.next(0)[0]), 15)
        r.seek(0)                                          # long gone: clamped to the oldest kept
        self.assertEqual(int(r.next(0)[0]), 15)

    def test_waits_for_the_next_frame(self):
        hub = AudioHub()
        r = hub.reader()
        threading.Timer(0.05, lambda: hub.push(frame(9))).start()
        self.assertEqual(int(r.next(1)[0]), 9)

    def test_clear_keeps_numbering(self):
        hub = AudioHub()
        for i in range(3):
            hub.push(frame(i))
        hub.clear()
        r = hub.reader(at=0)
        hub.push(frame(7))
        self.assertEqual(int(r.next(0)[0]), 7)


class SpeechCheckTest(unittest.TestCase):
    def test_real_wake_has_speech(self):
        probs = [0.0] * 20 + [0.9] * 6                     # 0.48 s of speech, like the live clips
        self.assertTrue(wake_check.speech_check(probs)[0])

    def test_noise_burst_is_not_a_voice(self):
        self.assertFalse(wake_check.speech_check([0.0] * 20 + [0.7, 0.2, 0.6])[0])   # 0.16 s
        self.assertFalse(wake_check.speech_check([])[0])

    def test_only_the_last_two_seconds_count(self):
        probs = [0.9] * 10 + [0.0] * 25                    # speech, then 2 s of nothing before the hit
        self.assertFalse(wake_check.speech_check(probs)[0])


def old_strip_wake(text):
    """assistant.strip_wake before it moved to wake_check.py, to check it behaves the same."""
    hey = {"hey", "hay", "hi", "hair", "head", "hit", "here", "he", "hate", "heat", "eh", "a", "hello", "okay", "ok"}

    def tritonish(words):
        s = re.sub(r"[^a-z]", "", "".join(words).lower())
        return len(s) >= 4 and SequenceMatcher(None, s, "triton").ratio() >= 0.6
    w = text.split()
    for i in range(min(3, len(w))):
        if re.sub(r"[^a-z]", "", w[i].lower()) in hey:
            for k in (1, 2):
                if i + 1 + k <= len(w) and tritonish(w[i + 1:i + 1 + k]):
                    return " ".join(w[i + 1 + k:]).lstrip(",. ")
    if w and len(w[0]) > 4 and tritonish(w[:1]):
        return " ".join(w[1:]).lstrip(",. ")
    return text


class StripWakeTest(unittest.TestCase):
    CASES = ["Hey Triton, open YouTube", "hair triter open netflix", "um hey triton", "Triton, mute", "hey tri ton",
             "open chrome", "hey there", "", "Hey Tritan. Next song.", "a triton volume up", "hate item play music"]

    def test_same_as_before(self):
        for c in self.CASES:
            self.assertEqual(wake_check.strip_wake(c), old_strip_wake(c), c)

    def test_examples(self):
        self.assertEqual(wake_check.strip_wake("Hey Triton, open YouTube"), "open YouTube")
        self.assertEqual(wake_check.strip_wake("hey triton"), "")
        self.assertEqual(wake_check.strip_wake("open chrome"), "open chrome")


class DecisionTest(unittest.TestCase):
    """_Decision and _say's hold, on a stand-in for the assistant (no models)."""

    @classmethod
    def setUpClass(cls):
        import assistant
        cls.A = assistant

    def make(self, decide):
        me = self.A.Assistant.__new__(self.A.Assistant)
        me._tl, me._hold, me.remote = threading.local(), None, None
        me.said = []
        me.speaker = type("S", (), {"say": lambda s, text, wait=False, start_within=0: me.said.append(text) or True})()
        me._decide = decide
        return me

    def test_speech_waits_for_release_then_plays(self):
        def decide(heard, free):
            me._say("Hmm.")                                 # e.g. the laptop's first sentence
            return heard, free, "say", "hi", "laptop", 0.0
        me = self.make(decide)
        job = self.A._Decision(me, "hello", "hello")
        time.sleep(0.1)
        self.assertEqual(me.said, [])                       # held: you may still be talking
        job.release()
        self.assertEqual(job.wait(2)[2], "say")
        self.assertEqual(me.said, ["Hmm."])
        self.assertFalse(job.quick)                         # the laptop's answer is held for the grace time

    def test_abandoned_decision_says_nothing(self):
        def decide(heard, free):
            me._say("Opening Netflix")
            return heard, free, "act", [], "model", 0.0
        me = self.make(decide)
        job = self.A._Decision(me, "open netflix", "open netflix")
        time.sleep(0.05)
        job.abandon()                                       # you went on: "...and select Shashank"
        job.wait(2)
        self.assertEqual(me.said, [])

    def test_rules_answer_is_quick(self):
        me = self.make(lambda h, f: (h, f, "act", [{"tool": "media", "args": {}}], "rules", 0.0))
        job = self.A._Decision(me, "next song", "next song")
        job.wait(2)
        self.assertTrue(job.quick)

    def test_after_own_voice_keeps_what_came_after(self):
        me = self.make(None)
        me.audio = AudioHub()
        for i in range(30):                                 # 2.4 s of mic
            me.audio.push(frame(i))
        me._tl.reader = me.audio.reader(at=0)
        me.speaker = type("S", (), {"quiet_for": 1.0})()    # its voice ended 1.0 s ago
        me._after_own_voice()
        # 1.0 s - 0.15 s guard = 0.85 s kept: the last 10-11 frames are still to be read
        self.assertIn(me._reader().behind, (10, 11))


if __name__ == "__main__":
    unittest.main()
