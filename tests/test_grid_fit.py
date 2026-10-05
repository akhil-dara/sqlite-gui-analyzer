"""Cell text is cut to fit by adding up character widths (grid.CharWidths), not by asking Tk
to measure whole strings: the cut never overflows the cell, ASCII cuts are the longest that
fit, other scripts and emoji are guessed wide and measured afterwards in short steps, and a
first draw of a wide table makes few Tk measure calls."""

import random
import time
import unittest

from tests.helpers import tk_root, within

SAMPLES = ["hello world", "a" * 300, "WWWWWWWWWWWWWWWWWWWWWWWWW", "iiiiiiiiiiiiiiiiiiiiiiiii",
           "café crème brûlée " * 5, "你好世界" * 10,
           "नमस्ते दुनिया " * 4,
           "\U0001F600\U0001F44D\U0001F3FD party \U0001F389" * 4,
           "Привет мир " * 6,
           "mixed 123 éè 你 \U0001F600 tail text " * 3]


class FitTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        from grid import CharWidths, DataGrid
        self.grid = DataGrid(self.root, frozen=1)
        self.grid.pack()
        self.cw = CharWidths.of(self.grid)

    def test_cut_text_fits_its_cell(self):
        g = self.grid
        rnd = random.Random(3)
        self.cw.measure_now()
        for fi in range(4):
            font = g._fonts[fi]
            for text in SAMPLES:
                for avail in (8, 20, 45, 80, 133, 200, 390):
                    got = g._fit(text, avail, fi)
                    self.assertTrue(text.startswith(got.rstrip("…")), (text, got))
                    if got == text:
                        continue
                    # never wider than the cell (Tk's own measure)
                    self.cw.measure_now()
                    g.widths_learnt()
                    got = g._fit(text, avail, fi)
                    if got:
                        self.assertLessEqual(font.measure(got), avail + 1, (text, avail, got))
                    if text.isascii() and got.endswith("…") and len(got) > 1:
                        # the longest cut that fits: one character more would not
                        more = text[:len(got)] + "…"
                        self.assertGreater(font.measure(more), avail, (text, avail))
            rnd.shuffle(SAMPLES)

    def test_ascii_widths_add_up_here(self):
        g = self.grid
        for fi in range(4):
            if not g._exactw[fi]:
                self.skipTest("this font system kerns: cuts are checked by Tk instead")
            for text in ("The quick brown fox", "0123456789,.;", "MMMM iiii WWWW"):
                self.assertEqual(g._text_width(text, fi), g._fonts[fi].measure(text))

    def test_unknown_characters_are_guessed_wide_then_measured_later(self):
        g = self.grid
        fresh = "ᚠᚡᚢᚣ"          # runic: never measured before here
        for ch in fresh:
            self.cw.tables[0].pop(ch, None)
        calls = []
        real = g._fonts[0].measure
        g._fonts[0].measure = lambda *a: (calls.append(a), real(*a))[1]
        try:
            w = g._text_width(fresh, 0)
        finally:
            g._fonts[0].measure = real
        self.assertEqual(calls, [])                     # nothing measured now
        self.assertEqual(w, 4 * self.cw.guess[0])       # each as wide as the widest
        self.assertTrue(all((0, ch) in self.cw.pending for ch in fresh))
        self.cw.measure_now()
        self.assertTrue(all(ch in self.cw.tables[0] for ch in fresh))
        self.assertEqual(self.cw.pending, {})


class FirstDrawTest(unittest.TestCase):
    """The first draw of a 51-column table of mixed scripts and emoji, and of 250 columns."""

    def setUp(self):
        self.root = tk_root(self)
        self.root.geometry("1400x850")
        self.root.deiconify()
        from grid import DataGrid, warm_fallback_fonts
        warm_fallback_fonts(self.root)      # as the app does at start (its window hidden)
        self.grid = DataGrid(self.root, frozen=1)
        self.grid.pack(fill="both", expand=True)
        self.root.update()

    def source(self, ncols, rows=200):
        from browse_sources import ListSource
        from engine.schema import Locator
        rnd = random.Random(ncols)
        out = []
        for r in range(rows):
            vals = [Locator("rowid", r + 1)]
            for c in range(1, ncols):
                vals.append(rnd.choice(SAMPLES) + " %d" % r if c % 3 else r * 7 + c)
            out.append((vals, set()))
        return ListSource(["_rid"] + ["column_%03d" % c for c in range(1, ncols)], out)

    def draw(self, ncols):
        g = self.grid
        calls = []
        fonts = g._fonts
        reals = [f.measure for f in fonts]
        for f, real in zip(fonts, reals):
            f.measure = lambda *a, _r=real: (calls.append(a), _r(*a))[1]
        try:
            g.set_source(self.source(ncols))
            t0 = time.perf_counter()
            g.redraw_now()
            spent = time.perf_counter() - t0
        finally:
            for f, real in zip(fonts, reals):
                f.measure = real
        return spent, len(calls)

    def test_a_redraw_over_its_budget_goes_on_at_the_next_turn(self):
        g = self.grid
        g.set_source(self.source(51))
        g.redraw_now()
        full = sorted(g.text_items("cells"))
        first, end = g.visible_row_range()
        self.assertGreater(end - first, 3)
        g.scroll_rows(end - first)              # other rows: every cell changes
        g._redraw(budget=1e-9)                  # the budget runs out after the first row
        self.assertEqual(g._rows_cut, 1)
        self.assertEqual(g.frames["cut"], 1)
        self.assertIsNotNone(g._redraw_id)      # it goes on when idle
        for _ in range(200):
            self.root.update()
            if g._redraw_id is None and g._rows_cut is None:
                break
        self.assertIsNone(g._rows_cut)          # the last redraw drew every row
        self.assertEqual(len(g.text_items("cells")), len(full))
        g.redraw_now()                          # redraw_now draws every row at once
        self.assertIsNone(g._rows_cut)
        # however slow Tk is, each continuation draws a row more: it always ends
        g.scroll_rows(end - first)
        passes = 0
        while True:
            g._redraw(budget=1e-9)
            passes += 1
            if g._rows_cut is None:
                break
            self.assertEqual(g._rows_cut, passes)
            self.assertLess(passes, 200)
        self.assertEqual(passes, end - first)
        self.assertEqual(len(g.text_items("cells")), len(full))

    def test_wide_tables_draw_quickly(self):
        for ncols, budget in ((51, 0.25), (250, 0.3)):
            spent, calls = self.draw(ncols)
            # at most a few measure calls per new character, never per cell
            self.assertLess(calls, 150, ncols)
            within(self, spent, budget, "first draw of %d columns" % ncols)
            # the columns drawn fill the grid's width (how many depends on the screen: a
            # small one, as on a CI machine, shows fewer)
            g = self.grid
            shown = g.visible_columns()
            self.assertGreaterEqual(len(shown), 3)
            self.assertGreaterEqual(sum(g._widths[c] for c in shown), g._cv.winfo_width() - 2)


if __name__ == "__main__":
    unittest.main()
