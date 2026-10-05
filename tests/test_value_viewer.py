"""Big text values: size notes in grid cells, tooltip previews and the value viewer."""

import json
import time
import unittest

from tests.helpers import tk_root
from engine import limits
from engine.fileformat.record import InvalidText
from engine.schema import Locator


class HelpersTest(unittest.TestCase):
    def tearDown(self):
        limits.reset()

    def test_size_badge(self):
        from grid import size_badge, cell_text
        self.assertEqual(size_badge("short"), "")
        self.assertEqual(size_badge(5), "")
        self.assertEqual(size_badge("a\nb\nc"), "↵ 3 lines · 5 chars")
        self.assertEqual(size_badge("x" * 12345), "12,345 chars")
        limits.load({"limits": {"cell_draw_chars": 20}})
        self.assertEqual(size_badge("y" * 25), "25 chars")
        self.assertEqual(len(cell_text("y" * 25)[0]), 20)
        self.assertEqual(cell_text("a\nb")[0], "a↵b")

    def test_tip_preview(self):
        from grid import tip_preview
        limits.load({"limits": {"cell_tip_lines": 3, "cell_tip_chars": 100}})
        text = "\n".join("line %d" % i for i in range(10))
        out = tip_preview(text)
        self.assertTrue(out.startswith("line 0\nline 1\nline 2\n…"), out)
        self.assertIn("more characters", out)
        self.assertEqual(tip_preview("abc"), "abc")
        self.assertEqual(len(tip_preview("z" * 1000).split("\n")[0]), 100)

    def test_pretty(self):
        from value_viewer import pretty, looks_structured, value_text
        self.assertEqual(pretty('{"a":[1,2]}')[1], json.dumps({"a": [1, 2]}, indent=2))
        kind, out = pretty("<a><b>x</b></a>")
        self.assertEqual(kind, "xml")
        self.assertIn("  <b>x</b>", out)
        for bad in ("{not json", "<a><b></a>", "plain",
                    '<!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>', "[" * 100000):
            with self.assertRaises(ValueError):
                pretty(bad)
        self.assertIsNone(looks_structured("hello"))
        self.assertEqual(value_text(None), "NULL")
        self.assertEqual(value_text(InvalidText(b"a\xff")), "a�")
        self.assertIsNone(value_text(b"\x00"))
        self.assertEqual(value_text(1.5), "1.5")


class ViewerTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)

    def tearDown(self):
        limits.reset()

    def open(self, value):
        from value_viewer import ValueViewer
        v = ValueViewer(self.root, value, "t")
        v.withdraw()
        self.pump(v)
        return v

    def pump(self, v, timeout=60):
        """Run the event loop until the viewer is filled; returns the longest gap between two
        event-loop turns (how long the window could not respond)."""
        deadline, worst, last = time.time() + timeout, 0.0, time.time()
        while time.time() < deadline:
            self.root.update()
            now = time.time()
            worst, last = max(worst, now - last), now
            if not v.loading():
                return worst
        self.fail("viewer did not finish loading")

    def test_counts_find_wrap_copy(self):
        text = "alpha\nBeta beta\n" * 10 + "tail"
        v = self.open(text)
        self.assertIn("%s chars" % len(text), v.info_text())
        self.assertIn("21 lines", v.info_text())
        v.find_var.set("beta")
        self.assertEqual(v.find_next(), text.lower().find("beta"))
        self.assertEqual(v.match_count(), 20)
        self.assertEqual(v.find_info.cget("text"), "1 of 20")
        v.find_prev()
        self.assertEqual(v.find_info.cget("text"), "20 of 20")
        self.assertEqual(v.text.get("cur.first", "cur.last").lower(), "beta")
        v.find_var.set("nothing here")
        v.find_next()
        self.assertEqual(v.find_info.cget("text"), "no matches")
        v.wrap_var.set(False)
        v._wrap_toggled()
        self.assertEqual(v.text.cget("wrap"), "none")
        v.lines_var.set(True)
        v._lines_toggled()
        self.root.update()
        self.assertEqual(v.copy(), text)
        self.assertEqual(self.root.clipboard_get(), text)
        self.assertEqual(v.text.get("1.0", "end-1c"), text)
        v.destroy()

    def test_pretty_print_toggle(self):
        v = self.open('{"k": [1, 2, {"z": "é"}]}')
        v.pretty_var.set(True)
        v._pretty_toggled()
        self.pump(v)
        self.assertIn('\n  "k": [', v.shown_text())
        self.assertIn("pretty-printed", v.info_text())
        v.pretty_var.set(False)
        v._pretty_toggled()
        self.pump(v)
        self.assertEqual(v.shown_text(), '{"k": [1, 2, {"z": "é"}]}')
        bad = self.open("{broken")
        bad.pretty_var.set(True)
        bad._pretty_toggled()
        self.pump(bad)
        self.assertFalse(bad.pretty_var.get())
        self.assertIn("not pretty-printed", bad.info_text())
        plain = self.open("just words")
        self.assertIn("disabled", plain.pretty_cb.state())

    def test_long_lines_are_shown_in_pieces_but_copied_whole(self):
        limits.load({"limits": {"value_view_line_chars": 100}})
        text = "head\n" + " ".join("w%04d" % i for i in range(2000)) + "\ntail"
        v = self.open(text)
        self.assertIn("shown in pieces", v.info_text())
        self.assertGreater(int(v.text.index("end-1c").split(".")[0]), 100)
        v.text.tag_add("sel", "2.0", "end-1c")
        self.assertEqual(v.copy(), text[5:])
        self.assertEqual(v._line_label(1), "1")
        self.assertEqual(v._line_label(2), "2")
        self.assertEqual(v._line_label(3), "↪")
        self.assertEqual(v._line_label(int(v.text.index("end-1c").split(".")[0])), "3")
        v.find_var.set("w1999")
        off = v.find_next()
        self.assertEqual(off, text.find("w1999"))
        self.assertEqual(v.text.get("cur.first", "cur.last"), "w1999")

    def test_layout_display(self):
        from value_viewer import layout_display
        text = "ab\n" + "x" * 25 + "\n\nc d e f g h i j k l m"
        shown, starts, cont = layout_display(text, 10)
        self.assertTrue(all(len(line) <= 10 for line in shown.split("\n")))
        self.assertEqual(shown.replace("\n", ""), text.replace("\n", ""))
        for i, line in enumerate(shown.split("\n")):
            self.assertEqual(text[starts[i]:starts[i] + len(line)], line)
        self.assertIsNone(layout_display("short\nlines", 10)[2])

    def test_cut_says_so(self):
        limits.load({"limits": {"value_view_chars": 1000}})
        v = self.open("q" * 5000)
        self.assertEqual(len(v.shown_text()), 1000)
        self.assertIn("value_view_chars", v.info_text())

    def test_multi_megabyte_stays_responsive(self):
        text = ("0123456789 abcdefghij " * 5 + "\n") * 60000       # ~6.6 MB, 60k lines
        from value_viewer import ValueViewer
        t0 = time.time()
        v = ValueViewer(self.root, text, "big")
        v.withdraw()
        opened = time.time() - t0
        worst = self.pump(v)
        self.assertLess(opened, 1.0)
        self.assertLess(worst, 1.0, "the window stopped responding for %.2fs" % worst)
        v.find_var.set("abcdefghij")
        v.find_next()
        self.assertEqual(v.match_count(), 300000)
        for _ in range(3):
            v.find_prev()
        self.assertEqual(v.find_info.cget("text"), "299,998 of 300,000")
        v.destroy()
        self.root.update()

    def test_close_while_loading(self):
        from value_viewer import ValueViewer
        v = ValueViewer(self.root, "x" * 3000000, "big")
        v.withdraw()
        self.root.update()
        v.destroy()
        for _ in range(20):
            self.root.update()

    def test_minimizes_with_parent(self):
        """The preview is a proper dialog of the main window: it hides when the main
        window is minimized and comes back on restore, never left behind."""
        from value_viewer import ValueViewer
        self.root.deiconify()
        v = ValueViewer(self.root, "hello", "t")
        self.pump(v)
        try:
            self.root.update()
            self.assertEqual(str(v.tk.call("wm", "transient", v)), str(self.root))
            self.assertTrue(v.winfo_viewable())
            # the <Unmap>/<Map> path
            v._on_owner_unmap(None)
            self.assertFalse(v.winfo_viewable())
            self.assertTrue(v._hidden_with_owner)
            v._on_owner_map(None)
            self.root.update()
            self.assertTrue(v.winfo_viewable())
            self.assertFalse(v._hidden_with_owner)
            # the watchdog path, from the owner's real mapped state
            self.root.withdraw()
            self.root.update()
            v._watch_min()
            self.assertFalse(v.winfo_viewable())
            self.root.deiconify()
            self.root.update()
            v._watch_min()
            self.assertTrue(v.winfo_viewable())
            self.assertFalse(v._hidden_with_owner)
        finally:
            v.destroy()
            for _ in range(10):
                self.root.update()


class GridBigTextTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        self.root.geometry("900x400+-4000+0")
        from grid import DataGrid
        from browse_sources import ListSource
        self.long = "long text " * 3000
        self.multi = "\n".join("line %d" % i for i in range(50))
        rows = [([Locator("rowid", i + 1), self.long if i % 2 else self.multi, "s%d" % i], set())
                for i in range(20)]
        self.grid = DataGrid(self.root)
        self.grid.pack(fill="both", expand=True)
        self.grid.set_source(ListSource(["_rid", "body", "short"], rows))
        self.root.deiconify()
        self.root.update()
        self.grid.redraw_now()
        self.root.update()

    def test_badges_tooltip_and_view_value(self):
        g = self.grid
        # badges render async; poll (flaky in full-suite under load, window off-screen)
        badges = {}
        for _ in range(60):
            g.redraw_now()
            self.root.update()
            badges = g.badge_items()
            if (0, 1) in badges:
                break
            import time; time.sleep(0.05)
        self.assertEqual(badges[(0, 1)], "↵ 50 lines · %s chars" % format(len(self.multi), ","))
        self.assertEqual(badges[(1, 1)], "%s chars" % format(len(self.long), ","))
        self.assertNotIn((0, 2), badges)
        tip = g.show_cell_tip(0, 1, 0, 0)
        self.assertTrue(tip.startswith("line 0\nline 1\n"))
        self.assertIn("View value", tip)
        g._tip_hide()
        g.set_current_cell(1, 1)
        g._cv.focus_force()
        g._cv.event_generate("<Shift-Return>")
        self.root.update()
        viewers = [w for w in g.winfo_children() if type(w).__name__ == "ValueViewer"]
        self.assertEqual(len(viewers), 1)
        v = viewers[0]
        while v.loading():
            self.root.update()
        self.assertEqual(v.shown_text(), self.long)
        self.assertIn("row 2", v.title())
        v.destroy()
        m = g.build_context_menu(0, 1)
        labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)
                  if m.type(i) == "command"]
        self.assertIn("View value…", labels)
        self.assertIsNone(g.view_value(0, 0))                   # the locator column

    def test_scrolling_multi_megabyte_cells_stays_fast(self):
        from browse_sources import ListSource
        g = self.grid
        huge = [("x" * 2000000 + "\n") * 2 for _ in range(3)]
        rows = [([Locator("rowid", i + 1), huge[i % 3], i], set()) for i in range(5000)]
        g.set_source(ListSource(["_rid", "body", "n"], rows))
        self.root.update()
        worst = 0.0
        for i in range(120):
            t0 = time.time()
            g.scroll_rows(7 if i % 10 else -40)
            g.redraw_now()
            self.root.update_idletasks()
            worst = max(worst, time.time() - t0)
        self.assertLess(worst, 0.5, "a redraw took %.2fs" % worst)
        self.assertTrue(all(b.startswith("↵ 3 lines") for b in g.badge_items().values()))


if __name__ == "__main__":
    unittest.main()
