"""Background work gives way while the user interface thread is busy.

Python runs one thread at a time. While a worker thread reads rows in a Python loop, every
call the Tk thread makes into Tk hands the interpreter over and must wait for it back (up to
the switch interval, 5 ms), so a tab switch or a window made of a few hundred Tk calls took
several times as long while links were being checked or rows counted.

The app calls beat() from a short timer on its Tk thread. A worker calls pause() in its
loops: when the Tk thread has not beaten for a while it is busy (drawing, laying out, running
a callback), and the worker sleeps a moment, leaving the interpreter to it. Without a beat
(no user interface, or it has ended) pause() does nothing. Code on the Tk thread that waits
for workers calls beat() as it waits, so the workers it waits for do not slow down.

No Tk here.
"""

import threading
import time

BUSY_AFTER = 0.03       # the Tk thread counts as busy when it has not beaten for this long
NAP = 0.003             # how long a worker sleeps at a pause point while it is busy
MOST = 0.25             # a worker never waits longer than this in a row (the UI may hang)

_state = {"last": 0.0, "on": False, "naps": 0}
_main = threading.main_thread()


def beat():
    """The Tk thread is serving events (called from its timer and while it waits)."""
    _state["last"] = time.monotonic()
    _state["on"] = True


def stop():
    """The user interface ended: workers no longer give way."""
    _state["on"] = False


def pause():
    """In a worker's loop: sleep while the Tk thread is busy (at most MOST seconds after its
    last beat: a Tk thread that long without a beat is waiting, not drawing)."""
    if not _state["on"] or threading.current_thread() is _main:
        return
    while _state["on"]:
        since = time.monotonic() - _state["last"]
        if since < BUSY_AFTER or since > MOST:
            return
        _state["naps"] += 1
        time.sleep(NAP)


def naps():
    """How many times workers gave way (tests)."""
    return _state["naps"]
