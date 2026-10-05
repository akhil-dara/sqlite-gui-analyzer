"""Regression tests for the Timeline tab's silent-failure fixes (2026-10-02):

1. start_build() while detection is still running must queue the build
   (via _build_after_detect) instead of silently returning with no result.
2. start_build() while another job runs must say so in the status line.
3. _run()'s finished() must clear the busy state even when the database
   changed mid-job (a stuck busy state blocked every later build).
"""
import unittest

from tests.helpers import tk_root

try:
    import timeline_tab
    from timeline_tab import TimelineTab
except ImportError:  # pragma: no cover - import failure surfaces as test error
    timeline_tab = None
    TimelineTab = None


class _Status(object):
    def __init__(self):
        self.text = ""

    def set(self, text, details=None):
        self.text = text

    def configure(self, **kw):
        pass


class _Bar(object):
    def configure(self, **kw):
        pass


class _Top(object):
    def show(self, widget, flag):
        pass


class _Db(object):
    ok = True


def bare_tab():
    """A TimelineTab with __init__ skipped and only the attributes the
    fixed code paths touch."""
    tab = TimelineTab.__new__(TimelineTab)
    tab._busy = None
    tab._gen = 0
    tab._build_after_detect = False
    tab._stale_scope = False
    tab.app = type("App", (), {"db": _Db()})()
    tab.detection = object()
    tab.status = _Status()
    tab.bar = _Bar()
    tab.top = _Top()
    tab.stop_btn = object()
    tab._progress_after = object()  # skip the progress poller
    return tab


@unittest.skipIf(TimelineTab is None, "no timeline_tab")
class BuildWhileDetectingTest(unittest.TestCase):
    def test_build_queues_while_detecting(self):
        tab = bare_tab()
        tab._busy = "detect"
        tab.start_build()
        self.assertTrue(tab._build_after_detect,
                        "the build must be queued for when detection finishes")
        self.assertIn("date columns", tab.status.text)

    def test_build_while_other_job_says_so(self):
        tab = bare_tab()
        tab._busy = "build"
        tab.start_build()
        self.assertFalse(tab._build_after_detect)
        self.assertIn("Still working", tab.status.text)


@unittest.skipIf(TimelineTab is None, "no timeline_tab")
class RunFinishedGenMismatchTest(unittest.TestCase):
    def test_finished_clears_busy_on_gen_mismatch(self):
        tab = bare_tab()
        seen = {}

        class Runner(object):
            def submit(self, what, fn, finished):
                seen["finished"] = finished

        tab._runner = Runner()
        tab._stop = [False]
        tab._run("detect", lambda cancel, progress: "res",
                 lambda res, seconds: None)
        self.assertEqual(tab._busy, "detect")
        tab._gen += 1  # the database was closed or changed meanwhile
        seen["finished"]("res", None)
        self.assertIsNone(tab._busy,
                          "a dropped job must not leave the tab stuck busy")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(TimelineTab is None, "no timeline_tab")
class BuildAfterDetectFlagTest(unittest.TestCase):
    def test_stop_cancels_queued_build(self):
        tab = bare_tab()
        tab._stop = [False]

        class Runner(object):
            def cancel(self):
                pass

            def running_thread(self):
                return None

        tab._runner = Runner()
        tab._build_after_detect = True
        tab.stop()
        self.assertFalse(tab._build_after_detect,
                         "Stop must cancel a build queued while detecting")

    def test_build_when_ready_does_not_queue_during_build(self):
        tab = bare_tab()
        tab._busy = "build"
        tab.build_when_ready()
        self.assertFalse(tab._build_after_detect,
                         "no stale flag may survive a build already running")

    def test_build_when_ready_queues_during_detect(self):
        tab = bare_tab()
        tab._busy = "detect"
        tab.detection = None
        tab.build_when_ready()
        self.assertTrue(tab._build_after_detect)
