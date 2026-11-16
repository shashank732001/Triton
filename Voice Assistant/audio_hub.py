"""
The microphone, kept: every 80 ms frame of the last KEEP_SEC, numbered, readable by any number of listeners at once.

Before, mic frames went into one queue that only one loop could read, and it was emptied after every reply and every
turn ("so the mic doesn't hear Triton"), which threw away whatever you said while Triton was thinking or talking.
That made Triton work in separate steps: listen, stop, think, talk, then listen again from scratch. Max keeps the
microphone stream running the whole time and every part reads it at its own pace; this is the same idea.

    hub = AudioHub()
    hub.push(frame)                         # the audio callback, for every frame
    r = hub.reader()                        # a listener starting now (each thread uses its own)
    frame = r.next(timeout=0.5)             # the next frame, or None
    r.rewind(0.3)                           # go back 0.3 s (e.g. to the start of the speech that interrupted)
    r.skip_to_now(); r.seek(index)
"""

import threading
from collections import deque

RATE = 16000
FRAME = 1280                 # 80 ms
KEEP_SEC = 30


class AudioHub:
    def __init__(self, keep_sec=KEEP_SEC, frame=FRAME, rate=RATE):
        self.frame_sec = frame / rate
        self._frames = deque(maxlen=max(1, int(keep_sec / self.frame_sec)))
        self._next = 0                     # index the next pushed frame gets
        self._cond = threading.Condition()

    @property
    def now(self):
        """Index of the next frame to arrive."""
        with self._cond:
            return self._next

    @property
    def oldest(self):
        with self._cond:
            return self._next - len(self._frames)

    def push(self, frame):
        with self._cond:
            self._frames.append(frame)
            self._next += 1
            self._cond.notify_all()

    def clear(self):
        """Forget everything kept (a new mic stream); readers continue from the next frame."""
        with self._cond:
            self._frames.clear()
            self._cond.notify_all()

    def get(self, index, timeout):
        """(frame, index it really had); index jumps forward if that frame is no longer kept. (None, index) on timeout."""
        with self._cond:
            if index >= self._next and not self._cond.wait_for(lambda: index < self._next, timeout):
                return None, index
            first = self._next - len(self._frames)
            index = max(index, first)
            return self._frames[index - first], index

    def frames(self, start, end=None):
        """The kept frames from index start to end (default: now), as a list."""
        with self._cond:
            first = self._next - len(self._frames)
            end = self._next if end is None else min(end, self._next)
            return [self._frames[i - first] for i in range(max(start, first), end)]

    def reader(self, at=None):
        return Reader(self, self.now if at is None else at)


class Reader:
    """One listener's position in the hub."""

    def __init__(self, hub, position):
        self.hub, self.position = hub, position

    def next(self, timeout=0.5):
        frame, index = self.hub.get(self.position, timeout)
        if frame is not None:
            self.position = index + 1
        return frame

    @property
    def behind(self):
        """Frames waiting to be read."""
        return max(0, self.hub.now - self.position)

    def skip_to_now(self):
        self.position = self.hub.now

    def seek(self, index):
        self.position = max(index, self.hub.oldest)

    def rewind(self, seconds):
        self.seek(self.position - int(round(seconds / self.hub.frame_sec)))
