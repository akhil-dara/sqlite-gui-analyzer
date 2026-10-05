"""Timers never outlive their widgets: no 'invalid command name ..._poll' on window close."""

import time
import unittest

from tests.helpers import tk_root


class TimerTest(unittest.TestCase):
    def test_runner_poll_is_cancelled_with_its_widget(self):
        import tkinter as tk
        from grid import Runner
        root = tk_root(self)
        frame = tk.Frame(root)
        runner = Runner(frame, "t")
        runner.submit("k", lambda: time.sleep(0.05), lambda r, e: None)
        self.assertIsNotNone(runner._poll_id)
        pending = root.tk.splitlist(root.tk.call("after", "info"))
        self.assertIn(runner._poll_id, pending)
        frame.destroy()
        self.assertIsNone(runner._poll_id)
        self.assertTrue(runner._closed)
        pending = root.tk.splitlist(root.tk.call("after", "info"))
        self.assertEqual(len(pending), 0)
        for t in runner.threads():
            t.join(2)

    def test_cancel_all_afters_keeps_commands_to_their_widgets(self):
        import tkinter as tk
        from widgets import cancel_all_afters
        root = tk_root(self)
        frame = tk.Frame(root)
        frame.after(10000, lambda: None)
        root.after(10000, lambda: None)
        self.assertEqual(cancel_all_afters(root), 2)
        self.assertEqual(root.tk.splitlist(root.tk.call("after", "info")), ())
        frame.destroy()                  # its command is still its own to delete: no error

    def test_tabs_cancel_their_progress_poll(self):
        from forensics_tab import ForensicsTab
        root = tk_root(self)
        tab = ForensicsTab.__new__(ForensicsTab)
        import tkinter.ttk as ttk
        ttk.Frame.__init__(tab, root)
        tab._busy = True
        tab._progress = (0, 0)
        tab.bar = ttk.Progressbar(tab)
        tab._poll_progress()
        self.assertIsNotNone(tab._progress_after)
        tab.destroy()
        self.assertEqual(root.tk.splitlist(root.tk.call("after", "info")), ())


if __name__ == "__main__":
    unittest.main()
