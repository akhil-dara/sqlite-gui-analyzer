"""DataGrid behaviour, checked by widget introspection (the window is placed off-screen and
never captured)."""
import json
import threading
import time
import unittest

from tests.helpers import TempDirTest
from engine.fileformat.record import InvalidText
from engine.schema import Locator

try:
    import tkinter as tk
    _root_error = None
except ImportError as e:        # pragma: no cover - Python built without Tk
    tk, _root_error = None, e


def rows_for(n, ncols=6, flags_every=0):
    out = []
    for r in range(n):
        vals = [Locator("rowid", r + 1)]
        for c in range(1, ncols):
            k = (r + c) % 5
            vals.append(None if k == 0 else r * 10 + c if k == 1 else "text %d/%d" % (r, c)
                        if k == 2 else (r + c) / 4.0 if k == 3 else b"\x89PNG\r\n\x1a\n" + bytes(r % 7))
        flags = set(("damaged_record",)) if flags_every and r % flags_every == 0 else set()
        out.append((vals, flags))
    return out


class GenSource(object):
    """Rows made on demand: any number of rows and columns without holding them."""

    def __init__(self, n, ncols):
        self.n, self.ncols = n, ncols

    def columns(self):
        return ["_rid"] + ["col%d" % i for i in range(1, self.ncols)]

    def row_count(self):
        return self.n

    def rows(self, start, count):
        return [([Locator("rowid", r + 1)] + ["v%d.%d" % (r, c) for c in range(1, self.ncols)],
                 set()) for r in range(start, min(self.n, start + count))]


class SlowSource(object):
    """A threaded source: rows() runs on the grid's worker thread and can be held back."""
    threaded = True

    def __init__(self, n, ncols=4, count_known=True):
        self.n, self.ncols, self.count_known = n, ncols, count_known
        self.gate = threading.Event()
        self.gate.set()
        self.threads = set()
        self.calls = []
        self.released = []

    def columns(self):
        return ["_rid"] + ["c%d" % i for i in range(1, self.ncols)]

    def row_count(self):
        return self.n if self.count_known else None

    def rows(self, start, count):
        self.threads.add(threading.current_thread().name)
        self.calls.append(start)
        self.gate.wait(10)
        return [([Locator("rowid", r + 1)] + [r * c for c in range(1, self.ncols)], set())
                for r in range(start, min(self.n, start + count))]

    def release_thread(self):
        self.released.append(threading.current_thread().name)


@unittest.skipIf(tk is None, "no tkinter")
class GridTest(TempDirTest):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest("no display: %s" % e)
        from widgets import setup_theme
        setup_theme(cls.root)
        cls.root.geometry("900x500+-4000+0")

    @classmethod
    def tearDownClass(cls):
        import gc
        from widgets import cancel_all_afters
        cancel_all_afters(cls.root)
        cls.root.destroy()
        from tests.helpers import _KEPT
        _KEPT.append(cls.root)          # freed at exit, on the main thread: see free_tk
        del cls.root
        gc.collect()

    def setUp(self):
        TempDirTest.setUp(self)
        from grid import DataGrid
        self.opened, self.blobs, self.sorted, self.filtered = [], [], [], []
        self.grid = DataGrid(self.root, frozen=1, filter_delay=10,
                             on_open_row=lambda r, v: self.opened.append((r, v)),
                             on_open_blob=lambda r, c, v: self.blobs.append((r, c, v)),
                             on_sort=lambda c, d: self.sorted.append((c, d)),
                             on_filter=lambda e, g: self.filtered.append((e, g)))
        self.grid.pack(fill="both", expand=True)
        self.root.update()

    def tearDown(self):
        src = self.grid.source
        if isinstance(src, SlowSource):
            src.gate.set()              # a read held back must not outlive the test
        workers = self.grid.worker_threads()
        self.grid.destroy()
        for t in workers:
            t.join(5)                   # a worker must not free the grid (and Tk) later
        TempDirTest.tearDown(self)

    def show(self, source):
        self.grid.set_source(source)
        return self.pump()

    def pump(self, until=None, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.root.update()
            if (until() if until else not self.grid.loading()):
                self.root.update()
                return True
            time.sleep(0.005)
        return False

    def list_source(self, n=1000, ncols=6, **kw):
        from browse_sources import ListSource
        return ListSource(["_rid"] + ["col%d" % i for i in range(1, ncols)],
                          rows_for(n, ncols, **kw))

    def test_draws_only_the_rows_and_columns_in_view(self):
        g = self.grid
        self.show(GenSource(100000, 250))
        first, end = g.visible_row_range()
        vis = g.visible_columns()
        self.assertEqual(first, 0)
        self.assertTrue(10 < end < 40, end)
        self.assertTrue(2 < len(vis) < 30, vis)
        self.assertLessEqual(len(g.text_items("cells")), (end - first) * len(vis))
        self.assertEqual(len(g.text_items("header")), len(vis))
        self.assertEqual(len(g.text_items("frozen")), end - first)
        g.yview_moveto(0.5)
        g.xview_moveto(0.5)
        self.pump()
        first2, end2 = g.visible_row_range()
        self.assertEqual(first2, 50000)
        self.assertGreater(min(g.visible_columns()), 100)
        self.assertLessEqual(len(g.text_items("cells")), (end2 - first2) * len(g.visible_columns()))
        texts = [t for _i, t in g.text_items("frozen")]
        self.assertIn("50001", texts)                        # the locator column stays in view

    def test_filter_entries_follow_their_columns(self):
        g = self.grid
        self.show(self.list_source(50, ncols=40))
        self.assertTrue(g.inline_filters())                 # the filter row is on by default

        def check(label):
            g.redraw_now()
            self.root.update_idletasks()
            for c in g.visible_columns():
                e = g.filter_entry(c)
                self.assertEqual((e.winfo_x(), e.winfo_width()), (g.column_x(c), g.column_width(c)),
                                 "%s: column %d" % (label, c))
        check("start")
        g.scroll_x(237)
        check("scrolled")
        self.assertLessEqual(g.column_x(g.visible_columns()[0]), 0)  # partly scrolled out
        c = g.visible_columns()[1]
        g.set_column_width(c, g.column_width(c) + 55)
        check("resized")
        g.xview_moveto(1.0)
        check("far right")

    def test_sort_by_header_and_back_to_natural_order(self):
        from engine.session import sort_key
        g = self.grid
        self.show(self.list_source(30))
        g.sort_by(1)
        self.pump()
        vals = [g.row_data(r)[0][1] for r in range(30)]
        self.assertEqual(vals, sorted(vals, key=sort_key))
        self.assertIsNone(vals[0])
        self.assertIsInstance(vals[-1], bytes)              # NULL < numbers < text < BLOB
        self.assertEqual(self.sorted[-1], ("col1", False))
        self.assertIn("▲", "".join(t for _i, t in g.text_items("header")))
        g.sort_by(1)                                        # again: descending
        self.pump()
        self.assertEqual(g.sort_state(), ("col1", True))
        g.sort_by(0)                                        # the locator: natural order
        self.pump()
        self.assertEqual([str(g.row_data(r)[0][0]) for r in range(3)], ["1", "2", "3"])

    def test_column_filters_and_errors(self):
        g = self.grid
        self.show(self.list_source(200))
        self.assertEqual(g.row_count(), 200)
        g.set_filter_text(3, ">40")                         # col3 holds numbers and text
        self.pump()
        self.assertEqual(self.filtered[-1], ({"col3": ">40"}, ""))
        kept = g.row_count()
        self.assertTrue(0 < kept < 200, kept)
        g.set_filter_text(3, "/[/")                          # invalid: marked, never raised
        self.pump()
        self.assertIn("col3", g.filter_errors())
        self.assertEqual(str(g.filter_entry(3).cget("style")), "GridFilterBad.TEntry")
        self.assertTrue(g.filter_tip(3).startswith("Cannot use this filter"))
        self.assertEqual(g.row_count(), 200)                # the bad filter is not applied
        g.set_filter_text(3, "")
        g.set_global_filter("TEXT 1/", apply=True)          # two words: 'text' and '1/'
        self.pump()
        self.assertEqual(g.row_count(), 20)                 # r = 1, 11, 21, ..., 191
        g.set_global_filter('"TEXT 1/"', apply=True)        # one phrase, any case
        self.pump()
        self.assertEqual(g.row_count(), 1)
        self.assertEqual(str(g.row_data(0)[0][0]), "2")
        # every word somewhere in the row, a quoted phrase is one word: each row r holds one
        # text 'text r/c'; both words occur for r = 1, 11, 101, 111, ..., 191
        g.set_global_filter('"text 1" 1/1', apply=True)
        self.pump()
        self.assertEqual([g.row_data(r)[0][0].value for r in range(g.row_count())],
                         [2, 12] + list(range(102, 193, 10)))
        self.assertFalse(g.filter_errors())
        g.clear_filters()
        self.pump()
        self.assertEqual(g.row_count(), 200)

    def test_filters_are_easy_to_find(self):
        """Each column filter says 'filter…' while empty, an active one is highlighted, and a
        line above the grid names the filters in force with Clear all."""
        g = self.grid
        self.show(self.list_source(200))
        g.set_inline_filters(True)
        g.redraw_now()
        e = g.filter_entry(2)
        self.assertEqual(e.placeholder.full_text(), "filter…")
        self.assertTrue(e.placeholder.winfo_manager())
        self.assertEqual(g.filter_bar_text(), "")
        corner = g._corner                  # the frozen column's filter row names the row
        self.assertIn("Filter ▸", [corner.itemcget(i, "text") for i in corner.find_all()
                                   if corner.type(i) == "text"])
        self.assertIn(">5", g.filter_tip(2))
        g.set_filter_text(3, ">40")
        g.set_global_filter("text", apply=True)
        self.pump()
        self.assertFalse(g.filter_entry(3).placeholder.winfo_manager())
        self.assertEqual(str(g.filter_entry(3).cget("style")), "GridFilterOn.TEntry")
        self.assertEqual(g.filter_bar_text(), "2 filters active: col3, all columns")
        g.set_filter_text(2, "/[/")
        self.pump()
        self.assertIn("1 cannot be used", g.filter_bar_text())
        g.clear_filters()
        self.pump()
        self.assertEqual(g.filter_bar_text(), "")
        self.assertEqual(g.row_count(), 200)
        self.assertEqual(str(g.filter_entry(3).cget("style")), "GridFilter.TEntry")

    def test_filter_by_this_value_from_the_context_menu(self):
        from engine.filters import value_expr
        g = self.grid
        self.show(self.list_source(60))
        for row in (7, 5):                                  # a BLOB, then a text value
            v = g.row_data(row)[0][2]
            menu = g.build_context_menu(row, 2)
            labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                      if menu.type(i) == "command"]
            self.assertEqual(labels[:4], ["Copy cell", "Copy row as TSV", "Copy row as CSV",
                                          "Copy row as JSON"])
            for i in range(menu.index("end") + 1):
                if menu.type(i) == "command" and menu.entrycget(i, "label") == "Filter to this value":
                    menu.invoke(i)
            self.pump()
            self.assertEqual(g.filter_texts(), {"col2": value_expr(v)})
            self.assertTrue(g.row_count() >= 1)
            self.assertTrue(all(g.row_data(r)[0][2] == v for r in range(g.row_count())))
            g.clear_filters()
            self.pump()
        blob_row = next(r for r in range(20) if isinstance(g.row_data(r)[0][4], bytes))
        menu = g.build_context_menu(blob_row, 4)
        idx = [i for i in range(menu.index("end") + 1)
               if menu.type(i) == "command" and menu.entrycget(i, "label") == "Inspect BLOB…"]
        self.assertEqual(menu.entrycget(idx[0], "state"), "normal")
        menu.invoke(idx[0])
        self.assertEqual(self.blobs[-1][1], "col4")

    def test_selection_keyboard_and_copy(self):
        g = self.grid
        self.show(self.list_source(500))
        g.set_current_cell(3, 2)
        g.set_current_cell(6, 2, extend=True)
        self.assertEqual(g.selected_rows(), (3, 6))
        tsv = g.rows_copy_text("tsv").splitlines()
        self.assertEqual(tsv[0].split("\t"), ["_rid", "col1", "col2", "col3", "col4", "col5"])
        self.assertEqual(len(tsv), 5)
        self.assertEqual(tsv[1].split("\t")[0], "4")
        data = json.loads(g.rows_copy_text("json"))
        self.assertEqual([d["_rid"] for d in data], ["4", "5", "6", "7"])
        csv_text = g.rows_copy_text("csv")
        self.assertTrue(csv_text.startswith("_rid,col1,"))
        self.assertEqual(g.cell_copy_text(), "2.0")          # row 6, col2: (6 + 2) / 4
        # keys are bound on the cells canvas; run what they are bound to (sending key events
        # would need the window to take the keyboard focus from the desktop)
        for key in ("End", "Up", "Home", "Down", "Right", "Next", "Control-a", "Control-c",
                    "Control-C", "Return", "Prior", "Left"):
            self.assertTrue(g._cv.bind("<%s>" % key), key)
        g._key_jump(-1, None)
        g._key_move(-1, 0)
        g._key_jump(0, None)
        g._key_move(1, 0)
        g._key_move(0, 1)
        self.assertEqual(g.current_cell(), (1, 3))
        g._key_jump(-1, None)
        self.pump()
        self.assertEqual(g.current_cell()[0], 499)
        first, end = g.visible_row_range()
        self.assertTrue(first <= 499 < end)
        g._open_current()
        self.assertEqual(self.opened[-1][0], 499)
        g._key_jump(None, -1)
        self.pump()
        self.assertEqual(g.current_cell()[1], 5)
        self.assertIn(5, g.visible_columns())

    def test_hidden_columns_and_chooser(self):
        g = self.grid
        self.show(self.list_source(20, ncols=8))
        g.hide_column(2)
        self.assertNotIn(2, g.displayed_columns())
        self.assertNotIn("col2", g.rows_copy_text("tsv").splitlines()[0].split("\t"))
        ch = g.column_chooser()
        ch.search_var.set("col")
        self.assertEqual(len(ch.tree.get_children()), 7)
        ch.set_all(False)
        self.assertEqual(g.displayed_columns(), [0])
        ch.set_all(True)
        self.assertEqual(g.displayed_columns(), list(range(8)))
        ch.search_var.set("col7")
        ch._toggle("7")
        self.assertEqual(g.hidden_columns(), {7})
        ch.destroy()

    def test_flagged_rows_are_tinted_and_the_inspector_lists_every_column(self):
        from constants import ROW_FLAG_BG
        g = self.grid
        self.show(self.list_source(50, flags_every=4))
        self.assertEqual(g.row_background(4), ROW_FLAG_BG["flag_damaged"])
        self.assertNotEqual(g.row_background(5), ROW_FLAG_BG["flag_damaged"])
        g.set_current_cell(4, 1)
        g.set_inspector(True)
        g.redraw_now()
        lines = g.inspector_rows()
        self.assertEqual([l[0] for l in lines], g.columns())
        self.assertEqual(lines[0][1], "locator")
        self.assertIn("NULL", [l[1] for l in lines])
        g.set_inspector(False)
        self.assertFalse(g.inspector_visible())

    def test_values_are_drawn_by_kind(self):
        from browse_sources import ListSource
        g = self.grid
        long_text = "word " * 400
        src = ListSource(["_rid", "v"], [([Locator("rowid", i + 1), v], set()) for i, v in enumerate(
            [None, b"\x89PNG\r\n\x1a\nxx", InvalidText(b"\xffA"), "multi\nline", 2.5, 7,
             long_text])])
        self.show(src)
        texts = [t for _i, t in g.text_items("cells")]
        self.assertIn("NULL", texts)
        self.assertIn("[BLOB 10 bytes · PNG]", texts)
        self.assertTrue(any(t.startswith("⚠ invalid text: ff 41") for t in texts))
        self.assertIn("multi↵line", texts)
        self.assertIn("2.5", texts)
        cut = [t for t in texts if t.startswith("word word")]
        self.assertTrue(cut and cut[0].endswith("…"), cut)     # cut to the column width
        tip = g.show_cell_tip(6, 1, -3000, -3000)
        self.assertTrue(tip.startswith(long_text), tip[:80])       # whole (under the tip cap)
        self.assertIn("View value", tip)                           # and how to see everything
        self.assertEqual(g.badge_items()[(6, 1)], "2,000 chars")
        self.assertIsNotNone(g._tip)
        g._tip_hide()
        self.assertIsNone(g.show_cell_tip(4, 1, -3000, -3000))      # '2.5' fits: no tip

    def test_threaded_source_reads_off_the_tk_thread_and_drops_stale_windows(self):
        g = self.grid
        src = SlowSource(100000)
        src.gate.clear()
        g.set_source(src)
        self.root.update()
        self.pump(lambda: src.calls)
        self.assertIsNone(g.row_data(0))                     # still reading: placeholders
        self.assertEqual(g.text_items("frozen")[0][1], "…")
        self.assertNotIn(threading.current_thread().name, src.threads)
        g.sort_by(1)                                        # a new generation meanwhile
        src.gate.set()
        self.pump()
        self.assertEqual(g.row_data(0)[0][1], 0)
        self.assertIn(0, src.calls)
        self.pump(lambda: not g.worker_threads())
        self.assertTrue(src.released)                        # the worker let go of its thread state
        g.yview_moveto(0.9)
        self.pump()
        first, _end = g.visible_row_range()
        self.assertEqual(g.row_data(first)[0][0], Locator("rowid", first + 1))

    def frozen_texts(self):
        return [t for _i, t in self.grid.text_items("frozen")]

    def test_rows_drawn_stay_in_view_until_the_new_ones_arrive(self):
        g = self.grid
        src = SlowSource(100000)
        self.show(src)
        before = self.frozen_texts()
        self.assertIn("1", before)
        src.gate.clear()
        g.yview_moveto(0.5)
        self.pump(lambda: src.calls and src.calls[-1] >= 50000)
        self.root.update()
        self.assertTrue(g.waiting())
        self.assertEqual(self.frozen_texts(), before)           # no placeholders, no blanks
        self.assertNotIn("…", self.frozen_texts())
        self.assertEqual(g.target_row_range()[0], 50000)       # where the view is going
        self.assertEqual(g.visible_row_range()[0], 0)          # what is drawn meanwhile
        self.assertTrue(g.visible_rows_data())
        src.gate.set()
        self.pump()
        self.assertFalse(g.waiting())
        self.assertEqual(g.visible_row_range()[0], 50000)
        self.assertIn("50001", self.frozen_texts())
        self.assertEqual(g.frames["empty"], 0)

    def test_a_slow_read_is_read_again_once_the_source_is_fast(self):
        # a jump far into an unindexed sort: once the position index is ready the running
        # (OFFSET) read is stopped and the rows are read again, quickly
        g = self.grid
        src = SlowSource(100000)
        stopped = threading.Event()

        def interrupt(_thread):
            stopped.set()
        src.interrupt = interrupt
        slow_rows = src.rows

        def rows(start, count):
            if src.fast:
                return [([Locator("rowid", r + 1)] + [r * c for c in range(1, src.ncols)],
                         set()) for r in range(start, min(src.n, start + count))]
            src.calls.append(start)
            src.gate.clear()
            src.gate.wait(10)           # held until interrupted (or 10 s)
            if stopped.is_set():
                raise RuntimeError("interrupted")
            return slow_rows(start, count)
        src.fast = False
        self.show(src)
        src.rows = rows
        g.yview_moveto(0.99)
        self.pump(lambda: src.calls and src.calls[-1] >= 90000)
        self.root.update()
        self.assertTrue(g.waiting())
        t0 = time.time()
        g.waiting_seconds()
        src.fast = True                 # the index is ready
        g.restart_reads()
        src.gate.set()                  # the interrupted read returns (and fails)
        self.pump(lambda: not g.waiting())
        self.assertFalse(g.waiting())
        self.assertLess(time.time() - t0, 5)
        self.assertTrue(stopped.is_set())
        self.assertEqual(g.load_error, "")
        self.assertGreater(g.visible_row_range()[0], 90000)

    def test_sort_keeps_the_rows_drawn_until_sorted_rows_arrive(self):
        g = self.grid
        src = SlowSource(5000)
        self.show(src)
        before = self.frozen_texts()
        src.gate.clear()
        g.sort_by(1, True)
        self.root.update()
        self.assertTrue(g.waiting())
        self.assertEqual(self.frozen_texts(), before)
        src.gate.set()
        self.pump()
        self.assertFalse(g.waiting())
        self.assertEqual(g.frames["empty"], 0)

    def test_prefetch_follows_the_scroll_direction(self):
        g = self.grid
        src = SlowSource(100000)
        self.show(src)
        win = g._win
        del src.calls[:]
        g.scroll_to_row(10 * win)
        self.pump()
        self.assertIn(11 * win, src.calls)                      # downwards: the windows below
        self.assertIn(12 * win, src.calls)
        g.scroll_to_row(40 * win)
        self.pump()
        del src.calls[:]
        g.scroll_to_row(30 * win + 5)
        self.pump()
        self.assertIn(29 * win, src.calls)                      # upwards: those above
        self.assertIn(28 * win, src.calls)
        self.assertNotIn(32 * win, src.calls)

    def test_drag_reads_the_newest_position_and_stops_stale_reads(self):
        from engine import limits
        limits.load({"limits": {"grid_drag_debounce_ms": 30}})
        self.addCleanup(limits.reset)
        g = self.grid
        src = SlowSource(1000000)
        stopped = []
        src.interrupt = lambda th: (stopped.append(th.name), src.gate.set())
        src.retryable = lambda e: True
        self.show(src)
        src.gate.clear()
        for i in range(1, 40):
            g.drag_to(i / 40.0)
            self.root.update()
            self.assertNotIn("…", self.frozen_texts())
        src.gate.set()
        self.pump(lambda: not g.loading() and not g.waiting())
        first, _end = g.visible_row_range()
        self.assertEqual(first, int(39 / 40.0 * 1000000))
        self.assertEqual(g.row_data(first)[0][0], Locator("rowid", first + 1))
        self.assertEqual(g.frames["empty"], 0)
        self.assertFalse(g.load_error)

    def test_a_cut_copy_says_so(self):
        from engine import limits
        limits.load({"limits": {"grid_copy_rows": 5}})
        self.addCleanup(limits.reset)
        g = self.grid
        self.show(self.list_source(50))
        g.select_all()
        text = g.rows_copy_text("tsv")
        self.assertEqual(len(text.strip().split("\n")), 6)      # header + 5 rows
        self.assertIn("grid_copy_rows", g.notice)
        g.set_current_cell(0, 1)
        g.rows_copy_text("tsv")
        self.assertEqual(g.notice, "")

    def test_unknown_row_count_grows_and_a_short_window_ends_it(self):
        g = self.grid
        self.show(SlowSource(450, count_known=False))
        self.assertFalse(g.row_count_exact())
        g.scroll_to_row(10 ** 6)
        self.pump()
        for _ in range(5):
            g.scroll_to_row(10 ** 6)
            self.pump()
        self.assertTrue(g.row_count_exact())
        self.assertEqual(g.row_count(), 450)
        first, end = g.visible_row_range()
        self.assertEqual(end, 450)


@unittest.skipIf(tk is None, "no tkinter")
class GridHookTest(unittest.TestCase):
    """row_style marks rows (the damaged-row tint and the selection keep their colour, the
    marker stays), on_context_menu extends the menu, view_state() round-trips widths, hidden
    columns and the sort."""

    def setUp(self):
        from tests.helpers import tk_root
        self.root = tk_root(self)
        self.root.geometry("800x400+-4000+0")
        from grid import DataGrid
        from browse_sources import ListSource
        from constants import ROW_FLAG_BG
        self.flag_bg = ROW_FLAG_BG["flag_damaged"]
        rows = [([Locator("rowid", i + 1), "v%d" % i, i], {"damaged_record"} if i == 2 else set())
                for i in range(30)]
        self.src = ListSource(["_rid", "a", "b"], rows)
        mark = ("#eeffee", "#00aa00")
        self.marked = {1: mark, 2: mark, 3: mark}
        self.grid = DataGrid(
            self.root, row_style=lambda r, v, f: self.marked.get(v[0].value - 1, (None, None)),
            on_context_menu=lambda m, r, c: m.add_command(label="Extra %d/%s" % (r, c)))
        self.grid.pack(fill="both", expand=True)
        self.grid.set_source(self.src)
        self.root.deiconify()
        self.root.update()
        self.grid.redraw_now()

    def test_marks(self):
        g = self.grid
        self.assertEqual(g.row_background(1), "#eeffee")
        self.assertEqual(g.row_marker(1), "#00aa00")
        self.assertEqual(g.row_background(2), self.flag_bg)     # damaged: its tint wins
        self.assertEqual(g.row_marker(2), "#00aa00")           # ... the marker shows the mark
        self.assertIsNone(g.row_marker(4))
        g.set_current_cell(3, 1)
        g.redraw_now()
        self.assertNotEqual(g.row_background(3), "#eeffee")    # selected
        self.assertEqual(g.row_marker(3), "#00aa00")
        del self.marked[1]
        g.restyle()
        g.redraw_now()
        self.assertIsNone(g.row_marker(1))
        self.assertNotEqual(g.row_background(1), "#eeffee")

    def test_a_failing_row_style_marks_nothing(self):
        g = self.grid
        g.row_style = lambda r, v, f: 1 / 0
        g.restyle()
        g.redraw_now()
        self.assertIsNone(g.row_marker(1))
        self.assertEqual(g.visible_row_range()[0], 0)

    def test_context_menu_hook_and_keys(self):
        m = self.grid.build_context_menu(4, 1)
        labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)
                  if m.type(i) == "command"]
        self.assertEqual(labels[-1], "Extra 4/1")
        pressed = []
        self.grid.bind_key("<Control-t>", lambda: pressed.append(1))
        self.assertTrue(self.grid._cv.bind("<Control-t>"))

    def test_view_state_round_trip(self):
        g = self.grid
        g.set_column_width(1, 150)
        g.hide_column(2)
        g.sort_by(1, True)
        st = g.view_state()
        self.assertEqual(st, {"widths": {"a": 150}, "hidden": ["b"], "order": ["a", "b"],
                             "wrap": False, "sort": ["a", True]})
        self.assertEqual(json.loads(json.dumps(st)), st)
        g.set_source(self.src)
        self.assertEqual(g.view_state(), {"widths": {}, "hidden": [], "order": ["a", "b"],
                                          "wrap": False, "sort": None})
        g.apply_view_state(dict(st, widths={"a": 150, "gone": 99}))
        self.assertEqual(g.column_width(1), 150)
        self.assertEqual(g.hidden_columns(), {2})
        self.assertEqual(g.sort_state(), ("a", True))
        g.set_source(self.src)
        g.apply_view_state({"hidden": ["a"]})          # the current cell's column: it moves on
        self.assertEqual(g.hidden_columns(), {1})
        self.assertEqual(g.current_cell()[1], 2)

    def test_column_shifting(self):
        g = self.grid
        self.assertEqual(g.displayed_columns(), [0, 1, 2])
        self.assertTrue(g.move_column(2, -1))          # b left of a
        self.assertEqual(g.displayed_columns(), [0, 2, 1])
        self.assertTrue(g.move_column(2, +1))          # and back
        self.assertEqual(g.displayed_columns(), [0, 1, 2])
        self.assertFalse(g.move_column(1, -1))         # a is already first: nowhere to go
        self.assertFalse(g.move_column(2, +1))         # b is already last
        self.assertFalse(g.move_column(0, +1))         # the frozen column never moves
        st = g.view_state()
        g.move_column(2, -1)
        g.set_source(self.src)                        # a new source starts in natural order
        self.assertEqual(g.displayed_columns(), [0, 1, 2])
        g.apply_view_state(st)                        # the saved order comes back by name
        self.assertEqual(g.displayed_columns(), [0, 1, 2])
        g.apply_view_state(dict(st, order=["b", "a"]))
        self.assertEqual(g.displayed_columns(), [0, 2, 1])
        g.apply_view_state(dict(st, order=["b", "gone"]))   # unknown names are skipped
        self.assertEqual(g.displayed_columns(), [0, 2, 1])

    def test_header_drag_reorders(self):
        g = self.grid
        g.redraw_now()
        hdr = g._hdr
        mid = lambda i: (g._xs[i] + g._xs[i + 1]) // 2 - g._xoff   # middle of a header
        x = mid(0)                                   # column a's header
        press = type("E", (), {"x": x, "y": 4, "x_root": 500, "y_root": 200})()
        g._on_press(press, hdr, False)
        self.assertIsNotNone(g._hdr_move)
        drag = type("E", (), {"x": mid(1), "x_root": 700, "y_root": 200})()
        g._on_drag(drag, hdr)                        # past the click threshold: a reorder
        self.assertTrue(g._hdr_move[2])
        g._on_release(drag, hdr)
        self.assertEqual(g.displayed_columns(), [0, 2, 1])
        # a press without a drag still sorts (b is at slot 0 now; a is at slot 1)
        xa = mid(1)
        g._on_press(type("E", (), {"x": xa, "y": 4, "x_root": 500, "y_root": 200})(),
                    hdr, False)
        g._on_release(type("E", (), {"x": xa, "y": 4, "x_root": 500, "y_root": 200})(),
                      hdr)
        self.assertEqual(g.sort_state(), ("a", False))

    def test_wrap_text(self):
        g = self.grid
        rh1 = g._rh
        g.set_wrap(True)
        self.assertTrue(g._wrap)
        self.assertGreater(g._rh, rh1)
        lines = g._wrap_text("alpha beta gamma delta epsilon", 10 ** 6, 0)
        self.assertEqual(lines, ["alpha beta gamma delta epsilon"])
        lines = g._wrap_text("alpha beta gamma delta epsilon zeta eta theta", 60, 0)
        self.assertEqual(len(lines), g._wrap_lines)
        self.assertTrue(lines[-1].endswith("\u2026"))
        for ln in lines:
            self.assertLessEqual(g._text_width(ln, 0), 60)
        st = g.view_state()
        self.assertTrue(st["wrap"])
        g.set_wrap(False)
        self.assertFalse(g._wrap)
        self.assertEqual(g._rh, rh1)
        g.apply_view_state(dict(st, wrap=True))
        self.assertTrue(g._wrap)


if __name__ == "__main__":
    unittest.main()
