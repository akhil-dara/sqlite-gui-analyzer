"""Filter popover placement (multi-monitor) and minimize behaviour.

- _place() must clamp to the virtual screen (all monitors), not the primary
  screen, or the popover lands on the wrong monitor.
- The popover must close when the main window is minimized, including on
  Windows where a minimized window still reports winfo_ismapped() as true
  (wm_state() == "iconic" is checked too).
"""
import re
import unittest

from tests.helpers import TempDirTest

try:
    import tkinter as tk
    _tk_error = None
except ImportError as e:        # pragma: no cover - Python built without Tk
    tk, _tk_error = None, e


class StubGrid(tk.Frame):
    """A real Tk widget standing in for DataGrid: FilterPopover is a Toplevel and
    needs a real widget master; only the grid callbacks it reaches are stubbed."""
    def __init__(self, master):
        tk.Frame.__init__(self, master)
        self.header_xy = (100, 100)
        self.closed = []

    def header_cell_root(self, c):
        return self.header_xy

    def popover_closed(self, pop):
        self.closed.append(pop)


@unittest.skipIf(tk is None, "no tkinter: %s" % _tk_error)
class PopoverPlacementTest(TempDirTest):
    def setUp(self):
        super(PopoverPlacementTest, self).setUp()
        from tests.helpers import tk_root
        self.root = tk_root(self)
        self.root.geometry("800x600+0+0")
        self.root.update()

    def make_popover(self, grid=None):
        from colfilter import FilterPopover
        if grid is None:
            grid = StubGrid(self.root)
            grid.pack()
        pop = FilterPopover(grid)
        self.addCleanup(lambda: (pop._stop(), pop.destroy()))
        # where _place() puts the window, as asked (a withdrawn test window reports 0 from
        # winfo_x on Windows, and the window manager may move a window off its monitors)
        real = pop.geometry
        pop.placed = []

        def geometry(spec=None):
            if spec is not None:
                m = re.match(r"^(?:\d+x\d+)?([+-]-?\d+)([+-]-?\d+)$", str(spec))
                if m:
                    pop.placed.append((int(m.group(1)), int(m.group(2))))
            return real(spec) if spec is not None else real()
        pop.geometry = geometry
        return pop

    def test_virtual_screen_covers_primary(self):
        pop = self.make_popover()
        vx, vy, vw, vh = pop._virtual_screen()
        self.assertGreaterEqual(vw, pop.winfo_screenwidth())
        self.assertGreaterEqual(vh, pop.winfo_screenheight())

    def test_place_keeps_popover_near_header_on_second_monitor(self):
        # Simulate a two-monitor desktop (primary 0..1919, second 1920..3839) with
        # the grid on the second monitor: the popover must open near the header,
        # not clamped into the primary screen.
        pop = self.make_popover()
        pop._virtual_screen = lambda: (0, 0, 3840, 1080)
        pop.grid_.header_xy = (2500, 300)
        pop.c = 0
        pop._place()
        self.root.update()
        x = pop.placed[-1][0]
        self.assertGreaterEqual(x, 2000,
                                "popover at x=%d, header was at 2500" % x)
        self.assertLessEqual(x, 2500)

    def test_place_clamps_popover_inside_virtual_screen(self):
        # A header past the right edge still lands the popover on a monitor.
        pop = self.make_popover()
        pop._virtual_screen = lambda: (-1920, 0, 4480, 1440)
        pop.grid_.header_xy = (5000, 300)
        pop.c = 0
        pop._place()
        self.root.update()
        x = pop.placed[-1][0]
        vx, vy, vw, vh = -1920, 0, 4480, 1440
        self.assertGreaterEqual(x, vx)
        self.assertLessEqual(x + pop.WIDTH, vx + vw)

    def test_tl_hidden_states(self):
        pop = self.make_popover()
        pop._tl = self.root
        self.root.deiconify()
        self.root.update()
        self.assertFalse(pop._tl_hidden())
        # Windows reports a minimized window as still mapped: simulate that by
        # forcing wm_state() to "iconic" (a real iconify needs a window manager,
        # which the headless test display does not run).
        orig_state = self.root.wm_state
        self.root.wm_state = lambda *a: "iconic"
        try:
            self.assertTrue(pop._tl_hidden(),
                            "minimized main window must count as hidden")
        finally:
            self.root.wm_state = orig_state
        self.root.withdraw()
        self.root.update()
        self.assertTrue(pop._tl_hidden())
        self.root.deiconify()
        self.root.update()

    def test_watch_min_closes_popover_when_minimized(self):
        pop = self.make_popover()
        pop.alive = True
        pop._stop_watch = lambda: None     # keep the watchdog single-shot in the test
        orig_mapped, orig_state = self.root.winfo_ismapped, self.root.wm_state
        # the Windows case: still mapped, but minimized
        self.root.winfo_ismapped = lambda: 1
        self.root.wm_state = lambda *a: "iconic"
        pop._tl = self.root
        try:
            pop._watch_min()
            self.root.update()
        finally:
            self.root.winfo_ismapped, self.root.wm_state = orig_mapped, orig_state
        self.assertFalse(pop.alive, "popover must close when main window minimized")
        self.assertEqual(pop.grid_.closed, [pop])


if __name__ == "__main__":
    unittest.main()
