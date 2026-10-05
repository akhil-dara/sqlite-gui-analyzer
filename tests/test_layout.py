"""Nothing is clipped at the default window size (1200x750) or the smallest (900x600): every
button, box, check box and label of every tab gets at least the width it asks for and lies
inside the window. Checked by widget introspection (requested vs given size), never by
looking at the screen."""

import os
import shutil
import tempfile
import time
import traceback
import unittest

from tests.helpers import TempDirTest, free_tk
from tests.fixtures import make_fixtures as fx

SIZES = ((1200, 750), (900, 600))
CHECKED = ("TButton", "Button", "TCheckbutton", "Checkbutton", "TRadiobutton", "Radiobutton",
           "TLabel", "Label", "TCombobox", "TMenubutton", "Menubutton", "TEntry")


def walk(w):
    yield w
    for c in w.winfo_children():
        for x in walk(c):
            yield x


def widget_at(parent, path):
    """The widget with this Tk path under parent (found by walking, not by name lookup)."""
    path = str(path)
    for w in walk(parent):
        if str(w) == path:
            return w
    raise KeyError(path)


def in_canvas(w):
    """Inside a scrolled canvas (a list that scrolls): its own scrollbar shows the rest."""
    p = w.master
    while p is not None:
        if p.winfo_class() == "Canvas":
            return True
        p = p.master
    return False


def clipped(top, where):
    """[(what, why)] of the widgets under `where` that are cut."""
    from widgets import ElideLabel
    out = []
    tw = top.winfo_width()
    tx = top.winfo_rootx()
    for w in walk(where):
        try:
            cls = w.winfo_class()
            if cls not in CHECKED or not w.winfo_ismapped() or in_canvas(w):
                continue
            if isinstance(w, ElideLabel):
                continue                # shortens its text on purpose, full text as tooltip
            if cls in ("TLabel", "Label"):
                try:
                    wrap = int(float(str(w.cget("wraplength")) or 0))
                except (ValueError, TypeError):
                    wrap = 0
                if wrap > 0 or not str(w.cget("text")).strip():
                    continue            # wraps its text (or shows none)
            rw, W = w.winfo_reqwidth(), w.winfo_width()
            name = "%s %r" % (cls, str(w.cget("text"))[:40] if cls != "TEntry" and
                              cls != "TCombobox" else w.winfo_name())
            if cls == "TEntry":
                if W < 40:
                    out.append((name, "entry only %d px wide" % W))
                continue
            if W + 2 < rw:
                out.append((name, "%d of %d px" % (W, rw)))
                continue
            x = w.winfo_rootx() - tx
            if x + W > tw + 2 or x < -2:
                out.append((name, "outside the window: x %d..%d of %d" % (x, x + W, tw)))
        except Exception:               # noqa: BLE001 - a widget destroyed meanwhile
            continue
    return out


class LayoutTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_layout_data_")
        os.environ["SGA_DATA_DIR"] = self.data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(os.environ.pop, "SGA_DATA_DIR", None)
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.tags.warn_with_dialogs = False
        self.errors = []
        self.app.report_callback_exception = lambda e, v, tb: self.errors.append(
            "".join(traceback.format_exception(e, v, tb)))

    def settle(self, seconds=0.4):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.01)

    def check(self, path, label, more=(), sizes=SIZES, walls=False):
        """Run inside the Tk main loop (the worker threads call after()). more: databases
        added to the case after the first. walls: also refuse a label of more than two
        lines (a text wall)."""
        app = self.app
        problems = []
        unreachable = []

        def scenario():
            app._open_db(path, wait=True)
            if more:
                app._add_paths(list(more), wait=True)
            self.settle(1.0)
            for w, h in sizes:
                app.state("normal")
                app.geometry("%dx%d+-4000+0" % (w, h))
                self.settle(0.6)
                if app.winfo_width() < w - 40 or app.winfo_height() < h - 40:
                    # the screen cannot hold this window (a CI machine's small screen at
                    # 200%): what is cut there says nothing about the layout
                    unreachable.append((w, h))
                    continue
                problems.extend("%dx%d header: %s: %s" % (w, h, a, b)
                                for a, b in clipped(app, app._header_frame))
                # the header is one line whatever the number of databases
                hh = app._header_frame.winfo_height()
                f = float(app.tk.call("tk", "scaling")) / (96 / 72.0)
                if hh > 52 * max(1.0, f):
                    problems.append("%dx%d header: %d px high (one line expected)" % (w, h, hh))
                if app._navigator.winfo_ismapped():
                    problems.extend("%dx%d navigator: %s: %s" % (w, h, a, b)
                                    for a, b in clipped(app, app._navigator))
                for tab in app._nb.tabs():
                    app._nb.select(tab)
                    self.settle(0.5)
                    frame = widget_at(app._nb, tab)
                    title = app._nb.tab(tab, "text").strip()
                    # the sub-pages of a tab (Forensics, WAL details) are looked at one by one
                    for nb in [x for x in walk(frame) if x.winfo_class() == "TNotebook"]:
                        for sub in nb.tabs():
                            if nb.tab(sub, "state") == "hidden":
                                continue
                            nb.select(sub)
                            self.settle(0.3)
                            problems.extend("%dx%d %s › %s: %s: %s" % (
                                w, h, title, nb.tab(sub, "text").strip(), a, b)
                                for a, b in clipped(app, widget_at(nb, sub)))
                    problems.extend("%dx%d %s: %s: %s" % (w, h, title, a, b)
                                    for a, b in clipped(app, frame))
                    if walls:
                        from tools.ux_probe import text_walls
                        problems.extend("%dx%d %s: text wall: %s" % (w, h, title, t)
                                        for t in text_walls(frame))

        def go():
            try:
                scenario()
            except Exception:           # noqa: BLE001 - reported below
                self.errors.append(traceback.format_exc())
            finally:
                app._close_db(confirm=False)
                app.destroy()
        app.after(100, go)
        app.mainloop()
        if unreachable and len(unreachable) == len(sizes):
            self.skipTest("the screen cannot hold %s windows" % ", ".join(
                "%dx%d" % s for s in unreachable))
        problems = sorted(set(problems))
        self.assertEqual(problems, [], "%s: clipped controls:\n  " % label + "\n  ".join(
            problems))
        self.assertEqual(self.errors, [])

    def test_wal_database(self):
        self.check(fx.wal_states(self.tmp), "wal_states")

    def test_database_with_freed_pages(self):
        self.check(fx.freelist(self.tmp), "freelist")

    def test_case_of_sixteen_databases(self):
        # a phone extraction: one-line header, the navigator, the Overview, nothing cut
        from tests.fixtures import workspace_fixtures as wf
        paths = wf.build(self.tmp, 16)
        self.check(paths[0], "case of sixteen", more=paths[1:])

    def test_case_of_sixteen_at_wide_windows(self):
        # 1600 px and a 2560 px (4K-like) window: nothing cut, no text walls
        from tests.fixtures import workspace_fixtures as wf
        paths = wf.build(self.tmp, 16)
        self.check(paths[0], "case of sixteen, wide", more=paths[1:],
                   sizes=((1600, 900), (2560, 1440)), walls=True)

    def test_case_of_four_databases(self):
        from tests.fixtures import case_fixtures as cf
        folders = [os.path.join(self.tmp, n) for n in ("phone_a", "phone_b")]
        for f in folders:
            os.makedirs(f)
        paths = [cf.messages(folders[0]), cf.contacts(folders[1]), cf.settings(folders[1]),
                 fx.freelist(folders[0])]
        self.check(paths[0], "case of four", more=paths[1:])


class ScaledLayoutTest(LayoutTest):
    """The same checks at Windows display scaling 125%, 150% and 200% (Tk's scaling set
    before the window is built; the windows stay off the screen): the window's size grows with
    the scaling, and nothing may be cut."""

    SCALES = (1.25, 1.5, 2.0)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sga_test_")    # a window per scaling, below

    def _with_scaling(self, scale, fn):
        os.environ["SGA_TK_SCALING"] = str(96 / 72.0 * scale)
        from tests.helpers import _KEPT
        try:
            LayoutTest.setUp(self)
            fn()
        finally:
            os.environ.pop("SGA_TK_SCALING", None)
            if getattr(self, "app", None) is not None:
                _KEPT.append(self.app)  # freed at exit, on the main thread

    def test_case_of_sixteen_scaled(self):
        from tests.fixtures import workspace_fixtures as wf
        tmp = tempfile.mkdtemp(prefix="sga_scaled_")
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = wf.build(tmp, 16)
        for scale in self.SCALES:
            sizes = ((int(1200 * scale), int(750 * scale)), (int(900 * scale), int(600 * scale)))
            self._with_scaling(scale, lambda: self.check(
                paths[0], "case of sixteen at %d%%" % (scale * 100), more=paths[1:],
                sizes=sizes, walls=True))

    def test_smallest_window_fits_the_screen_at_any_scaling(self):
        """The window's least size grows with the scaling but never beyond the screen (a
        1080p screen at 200% only just held 1800x1000; at 300% it could not). Sizes above the
        screen cannot be laid out here at all: Tk clamps a window to the screen."""
        import tkinter as tk
        seen = []
        screen = []

        def check():
            app = self.app
            w, h = app.minsize()
            sw, sh = screen[-1] if screen else (app.winfo_screenwidth(),
                                                 app.winfo_screenheight())
            self.assertLessEqual(w, sw)
            self.assertLessEqual(h, sh)
            seen.append((w, h))
            app.destroy()               # a main window left would keep later main loops going
        for scale in (1.0, 2.0):
            self._with_scaling(scale, check)
        self.assertLess(seen[0][0], seen[1][0])           # grows with the scaling
        # a screen smaller than 900x500 scaled (a 1280x720 screen at 200%: 1800x1000 wanted)
        real = tk.Misc.winfo_screenwidth, tk.Misc.winfo_screenheight
        screen.append((1280, 720))
        tk.Misc.winfo_screenwidth = lambda self: 1280
        tk.Misc.winfo_screenheight = lambda self: 720
        try:
            self._with_scaling(2.0, check)
        finally:
            tk.Misc.winfo_screenwidth, tk.Misc.winfo_screenheight = real
        self.assertEqual(seen[-1], (1240, 620))

    def test_wal_database(self):
        pass                            # covered at 100% by LayoutTest

    def test_database_with_freed_pages(self):
        pass

    def test_case_of_sixteen_databases(self):
        pass

    def test_case_of_sixteen_at_wide_windows(self):
        pass

    def test_case_of_four_databases(self):
        pass


if __name__ == "__main__":
    unittest.main()
