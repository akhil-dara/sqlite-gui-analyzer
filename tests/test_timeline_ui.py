"""The DataGrid column-formatter hook and the Timeline's event source, by widget introspection
(the window is never shown or captured)."""
import unittest
from datetime import datetime, timedelta

from tests.helpers import tk_root
from browse_sources import ListSource
from engine import timeline as tl
from engine.schema import Locator

try:
    import tkinter as tk
except ImportError:                 # pragma: no cover - Python built without Tk
    tk = None

MS = [1614834367000 + i * 3600000 for i in range(30)]


def source():
    rows = [([Locator("rowid", i + 1), ms, "row %d" % i, None if i == 3 else i], set())
            for i, ms in enumerate(MS)]
    return ListSource(["_rid", "ts", "name", "n"], rows)


@unittest.skipIf(tk is None, "no tkinter")
class FormatterTest(unittest.TestCase):
    def setUp(self):
        from grid import DataGrid
        self.root = tk_root(self)
        self.root.geometry("900x500+-4000+0")      # off-screen: drawn, never seen
        self.root.deiconify()
        self.menus = []
        self.grid = DataGrid(self.root, on_header_menu=lambda m, c: self.menus.append((m, c)),
                             filter_delay=10)
        self.grid.pack(fill="both", expand=True)
        self.root.update()
        self.grid.set_source(source())
        self.root.update()
        self.grid.redraw_now()

    def texts(self):
        return set(t for _i, t in self.grid.text_items("cells"))

    def test_cells_show_the_formatted_value(self):
        g = self.grid
        self.assertIn(str(MS[0]), self.texts())
        g.set_column_formatter(1, tl.formatter("unix_ms"), "UTC Unix ms")
        g.redraw_now()
        shown = self.texts()
        self.assertIn("2021-03-04 05:06:07", shown)
        self.assertNotIn(str(MS[0]), shown)
        self.assertIn("ts · UTC Unix ms", [t for _i, t in g.text_items("header")])
        self.assertEqual(g.display_text(MS[1], 1), "2021-03-04 06:06:07")
        self.assertEqual(g.display_text(None, 1), "NULL")           # no date: shown as stored
        # the source keeps the raw values: sorting and filters work on them
        self.assertEqual(g.row_data(0)[0][1], MS[0])
        g.set_filter_text(1, ">%d" % MS[10])
        self.assertEqual(g.row_count(), 19)
        g.set_filter_text(1, "")
        g.set_column_formatter(1, None)
        g.redraw_now()
        self.assertIn(str(MS[0]), self.texts())

    def test_copy_tip_inspector_and_menus(self):
        g = self.grid
        g.set_column_formatter(1, tl.formatter("unix_ms"), "UTC")
        g.set_current_cell(0, 1)
        self.assertEqual(g.cell_copy_text(), "2021-03-04 05:06:07")
        self.assertEqual(g.cell_copy_text(raw=True), str(MS[0]))
        self.assertIn("\t%d\t" % MS[0], g.rows_copy_text("tsv"))      # row copies stay raw
        menu = g.build_context_menu(0, 1)
        labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                  if menu.type(i) == "command"]
        self.assertIn("Copy raw value", labels)
        other = g.build_context_menu(0, 2)
        self.assertNotIn("Copy raw value", [other.entrycget(i, "label")
                                            for i in range(other.index("end") + 1)
                                            if other.type(i) == "command"])
        tip = g.show_cell_tip(0, 1, -5000, -5000)
        self.assertEqual(tip, "2021-03-04 05:06:07\nraw: %d" % MS[0])
        g._tip_hide()
        g.set_inspector(True)
        self.root.update()
        g.redraw_now()
        line = [r for r in g.inspector_rows() if r[0] == "ts"][0]
        self.assertEqual(line[2], "%d  (2021-03-04 05:06:07)" % MS[0])
        g.build_header_menu(1)
        self.assertEqual(self.menus[-1][1], 1)

    def test_a_failing_formatter_shows_the_raw_value(self):
        def bad(v):
            raise ValueError("no")
        self.grid.set_column_formatter(1, bad)
        self.grid.redraw_now()
        self.assertIn(str(MS[0]), self.texts())

    def test_new_source_drops_formatters(self):
        self.grid.set_column_formatter(1, tl.formatter("unix_ms"))
        self.grid.set_source(source())
        self.assertIsNone(self.grid.column_formatter(1))


class EventSourceTest(unittest.TestCase):
    def events(self):
        base = datetime(2022, 1, 1)
        return [tl.Event(base + timedelta(hours=i), "t%d" % (i % 2), "c", "unix_s",
                         Locator("rowid", i), str(i), "text=%d" % i, "DB", 1640995200 + 3600 * i)
                for i in range(10)]

    def test_rows_sort_and_filter(self):
        from timeline_tab import EventSource
        evs = self.events()
        src = EventSource(evs, offset=330)
        self.assertEqual(src.columns()[:3], ["_rid", "Time (UTC)", "Time (UTC+05:30)"])
        values, flags = src.rows(0, 1)[0]
        self.assertEqual(values[:4], [1, "2022-01-01 00:00:00", "2022-01-01 05:30:00", "t0"])
        self.assertIs(src.event_at(values), evs[0])
        src.sort("Time (UTC)", True)
        self.assertEqual(src.rows(0, 1)[0][0][0], 10)
        src.set_filters({"Table": "=t1"}, "")
        # threaded: the view is rebuilt when rows are read (on the grid's worker thread), so
        # the count is unknown until then and the Tk thread never sorts or filters
        self.assertTrue(src.threaded)
        self.assertIsNone(src.row_count())
        src.rows(0, 1)
        self.assertEqual(src.row_count(), 5)
        self.assertTrue(all(e.table == "t1" for e in src.view_events()))
        src.set_filters({}, "text=4")
        self.assertEqual([e.row for e in src.view_events()], ["4"])
        src.set_filters({}, "")
        src.sort(None, False)
        self.assertEqual([e.row for e in src.view_events()], [str(i) for i in range(10)])
        self.assertEqual(len(list(src.iter_all())), 10)


class TimeFormatTest(unittest.TestCase):
    def test_format_event_time(self):
        from timeline_tab import format_event_time
        dt = datetime(2026, 10, 2, 14, 30, 45)
        self.assertEqual(format_event_time(dt), "2026-10-02 14:30:45")
        self.assertEqual(format_event_time(dt, "12h"), "2026-10-02 02:30:45 PM")
        self.assertEqual(format_event_time(datetime(2026, 10, 2, 0, 5, 6), "12h"),
                         "2026-10-02 12:05:06 AM")
        ms = datetime(2026, 10, 2, 14, 30, 45, 123000)
        self.assertEqual(format_event_time(ms), "2026-10-02 14:30:45.123")
        self.assertEqual(format_event_time(ms, "12h"), "2026-10-02 02:30:45.123 PM")
        self.assertEqual(format_event_time(ms, "excel"), "02-Oct-2026 14:30:45.123")
        self.assertEqual(format_event_time(ms, "us"), "10/02/2026 02:30:45.123 PM")
        self.assertEqual(format_event_time(dt, "excel"), "02-Oct-2026 14:30:45")
        us = datetime(2026, 10, 2, 14, 30, 45, 123456)
        self.assertEqual(format_event_time(us), "2026-10-02 14:30:45.123456")
        # custom strftime, with and without %f
        self.assertEqual(format_event_time(dt, "custom", "%d/%m/%Y %H:%M"), "02/10/2026 14:30")
        self.assertEqual(format_event_time(ms, "custom", "%H:%M:%S"), "14:30:45.123")
        self.assertEqual(format_event_time(ms, "custom", "%H:%M:%S.%f"), "14:30:45.123000")
        # an invalid custom format falls back to ISO (unknown directives like %Q pass
        # through strftime untouched on some platforms: only a real failure falls back)
        self.assertEqual(format_event_time(dt, "custom", chr(0xD800)),
                         "2026-10-02 14:30:45")
        self.assertEqual(format_event_time(dt, "custom", ""), "2026-10-02 14:30:45")
        self.assertEqual(format_event_time(dt, "nope"), "2026-10-02 14:30:45")

    def test_event_source_time_format(self):
        from timeline_tab import EventSource
        base = datetime(2022, 1, 1, 15, 30, 0)
        evs = [tl.Event(base, "t", "c", "unix_s", Locator("rowid", 1), "0", "", "DB", 0)]
        src = EventSource(evs, time_format="12h")
        self.assertEqual(src.values(0)[1], "2022-01-01 03:30:00 PM")
        src.time_format, src.time_custom = "custom", "%Y%m%d"
        self.assertEqual(src.values(0)[1], "20220101")
        # the local column follows the same format
        src = EventSource(evs, offset=60, time_format="12h")
        self.assertEqual(src.values(0)[2], "2022-01-01 04:30:00 PM")


if __name__ == "__main__":
    unittest.main()
