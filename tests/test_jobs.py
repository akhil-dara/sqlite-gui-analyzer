"""jobs.Job: on_poll hands results over as the work goes, and nothing handed over at the very
end is left behind; the progress window shows only for work that takes a while; wait()."""

import threading
import time
import unittest

from tests.helpers import tk_root


class JobTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        self.root._jobs = []

    def pump(self, cond, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.root.update()
            if cond():
                return True
            time.sleep(0.005)
        return False

    def test_the_last_item_handed_over_is_taken(self):
        from jobs import Job
        ready, taken, order = [], [], []
        go = threading.Event()

        def work(job):
            go.wait(5)
            ready.append("last")            # handed over just as the job ends

        def on_poll(job):
            taken.extend(ready)
            del ready[:]
            if not go.is_set():
                go.set()                    # the work ends while this poll runs
                job.thread.join(5)
            order.append("poll")

        def done(result, error, cancelled):
            order.append("done")
            self.assertEqual(taken, ["last"])
        Job(self.root, "t", work, done, show=False, on_poll=on_poll)
        self.assertTrue(self.pump(lambda: "done" in order))
        self.assertEqual(taken, ["last"])
        self.assertEqual(order[-1], "done")
        self.assertEqual(self.root._jobs, [])

    def test_window_only_after_a_while_and_wait(self):
        import tkinter as tk
        from jobs import Job
        stop = threading.Event()

        def windows():
            return [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        quick = Job(self.root, "quick", lambda job: 1, lambda *a: None, show_after=0.3)
        self.assertTrue(self.pump(lambda: quick.finished and quick not in self.root._jobs))
        self.assertEqual(windows(), [])
        slow = Job(self.root, "slow", lambda job: stop.wait(5), lambda *a: None,
                   show_after=0.2)
        self.assertEqual(windows(), [])
        self.assertTrue(self.pump(lambda: windows(), 5))
        got = []
        stop.set()
        slow._on_done = lambda r, e, c: got.append(r)
        slow.wait()
        self.assertEqual(got, [True])
        self.assertEqual(windows(), [])
