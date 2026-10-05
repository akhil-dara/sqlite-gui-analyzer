"""A window freed by a worker thread must not call Tcl there: its Tk variables are let go of
on the Tk thread when it closes (widgets.release_variables)."""

import gc
import sys
import threading
import tkinter as tk
import unittest

from tests.helpers import tk_root


class Holder(object):
    pass


class ReleaseVariablesTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        self.seen = []
        old = sys.unraisablehook
        sys.unraisablehook = lambda u: self.seen.append(repr(u.exc_value))
        self.addCleanup(setattr, sys, "unraisablehook", old)

    def free_on_worker(self, holder):
        box = [holder]
        del holder

        def run():
            box.pop()
            gc.collect()
        th = threading.Thread(target=run)
        th.start()
        th.join(5)

    def make(self):
        h = Holder()
        h.one = tk.StringVar(self.root, value="x")
        h.flags = {"a": tk.BooleanVar(self.root), "b": tk.IntVar(self.root)}
        h.more = [tk.StringVar(self.root)]
        h.one.trace_add("write", lambda *a: None)
        return h

    def test_released_variables_are_freed_quietly_anywhere(self):
        from widgets import release_variables
        h = self.make()
        names = [h.one._name, h.flags["a"]._name, h.more[0]._name]
        release_variables(h)
        for n in names:
            self.assertFalse(self.root.getboolean(self.root.tk.call("info", "exists", n)))
        self.free_on_worker(h)
        self.assertEqual([s for s in self.seen if "main loop" in s], [])

    def test_the_datamap_windows_release_theirs(self):
        import datamap_ui
        src = open(datamap_ui.__file__, encoding="utf-8").read()
        self.assertIn("release_variables(self)", src.split("class _Window")[1].split(
            "class CopyRelatedWindow")[0])
        self.assertIn("release_variables(self)", src.split("class LimitsWindow")[1])
