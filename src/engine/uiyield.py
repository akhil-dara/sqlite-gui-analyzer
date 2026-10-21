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

The interpreter itself also hands over more often while the user interacts: a thread that
wants the interpreter gets it after the switch interval, 5 ms by default, and a redraw is
hundreds of Tk calls, each losing the interpreter to a busy worker in turn. active() sets
the interval to FAST for ACTIVE_SECONDS (every input event and every grid draw call it);
beat() puts the default back once that passes, so several CPU-bound workers never thrash
on it while the user only watches (measured: a 40-database timeline build 14.5 s at the
default, over 180 s when FAST stayed on). serve() starts it.

No Tk here.
"""

import sys
import threading
import time

FAST = 0.0002               # switch interval while the user interacts (seconds)
ACTIVE_SECONDS = 0.6        # how long after the last input / draw the fast interval lasts
SWITCH_INTERVAL = FAST      # (kept for callers that read it)

BUSY_AFTER = 0.03       # the Tk thread counts as busy when it has not beaten for this long
NAP = 0.003             # how long a worker sleeps at a pause point while it is busy
MOST = 0.25             # a worker never waits longer than this in a row (the UI may hang)

_state = {"last": 0.0, "on": False, "naps": 0, "fast_until": 0.0, "fast": False,
          "default": None}
_main = threading.main_thread()


def serve():
    """The user interface starts: it must stay responsive while workers run."""
    if _state["default"] is None:
        try:
            _state["default"] = sys.getswitchinterval()
        except AttributeError:
            _state["default"] = 0.005
    active()
    beat()


def active(seconds=ACTIVE_SECONDS):
    """The user is interacting (an input event) or a grid is drawing: the interpreter
    switches finely for the next `seconds`, so the window never waits on a worker."""
    now = time.monotonic()
    _state["fast_until"] = max(_state["fast_until"], now + seconds)
    if not _state["fast"]:
        try:
            sys.setswitchinterval(FAST)
            _state["fast"] = True
        except (ValueError, AttributeError):
            pass


def _relax():
    """Back to the default interval (the user is only watching)."""
    if _state["fast"] and _state["default"]:
        try:
            sys.setswitchinterval(_state["default"])
        except (ValueError, AttributeError):
            pass
        _state["fast"] = False


def fast():
    """True while the fine switch interval is on (tests)."""
    return _state["fast"]


def beat():
    """The Tk thread is serving events (called from its timer and while it waits)."""
    now = time.monotonic()
    _state["last"] = now
    _state["on"] = True
    if _state["fast"] and now > _state["fast_until"]:
        _relax()


def stop():
    """The user interface ended: workers no longer give way."""
    _state["on"] = False
    _relax()


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
