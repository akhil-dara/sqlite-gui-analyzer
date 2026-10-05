"""The date pickers (datepicker.py): the pure helpers (parsing, formatting, month grids,
presets), their agreement with engine.timeline.parse_when, the DateTimePicker field and its
calendar popup driven by the keyboard, and the DateRangePicker (presets, order check,
Custom)."""

import tkinter as tk
import unittest
from datetime import date, datetime, timedelta, timezone

from tests.helpers import off_screen_windows, send_key, tk_root
from datepicker import (PRESETS, DateRangePicker, DateTimePicker, format_datetime,
                        month_grid, parse_datetime, preset_range)
from engine.timeline import parse_when


class ParseTest(unittest.TestCase):
    def test_formats(self):
        want = datetime(2024, 3, 5, 14, 7, 9)
        for text in ("2024-03-05 14:07:09", "2024-03-05T14:07:09", "2024-03-05T14:07:09Z",
                     " 2024-03-05 14:07:09 ", "2024-03-05 14:07:09z"):
            self.assertEqual(parse_datetime(text), want, text)
        self.assertEqual(parse_datetime("2024-03-05 14:07"), datetime(2024, 3, 5, 14, 7))
        self.assertEqual(parse_datetime("2024-03-05T14:07"), datetime(2024, 3, 5, 14, 7))
        self.assertEqual(parse_datetime("2024-03-05"), datetime(2024, 3, 5))
        self.assertEqual(parse_datetime("2024-03-05 14:07:09.250"),
                         datetime(2024, 3, 5, 14, 7, 9, 250000))
        self.assertIsNone(parse_datetime(""))
        self.assertIsNone(parse_datetime("   "))
        self.assertIsNone(parse_datetime(None))

    def test_end_of_what_is_named(self):
        self.assertEqual(parse_datetime("2024-03-05", end=True),
                         datetime(2024, 3, 5, 23, 59, 59, 999999))
        self.assertEqual(parse_datetime("2024-03-05 10:15", end=True),
                         datetime(2024, 3, 5, 10, 15, 59, 999999))
        self.assertEqual(parse_datetime("2024-03-05 10:15:30", end=True),
                         datetime(2024, 3, 5, 10, 15, 30, 999999))
        # the last day there is: no overflow
        self.assertEqual(parse_datetime("9999-12-31", end=True),
                         datetime(9999, 12, 31, 23, 59, 59, 999999))

    def test_errors_are_short_value_errors(self):
        cases = {"2023-02-29": "February 2023 has 28 days",
                 "2024-13-01": "Month must be 01-12",
                 "2024-04-31": "April 2024 has 30 days",
                 "2024-01-01 24:00": "Hour must be 00-23",
                 "2024-01-01 10:60": "Minutes must be 00-59",
                 "2024-01-01 10:10:61": "Seconds must be 00-59",
                 "0000-01-01": "Year",
                 "yesterday": "YYYY-MM-DD",
                 "2024/01/01": "YYYY-MM-DD"}
        for text, part in cases.items():
            with self.assertRaises(ValueError) as cm:
                parse_datetime(text)
            self.assertIn(part, str(cm.exception), text)
            self.assertLess(len(str(cm.exception)), 70)
        self.assertEqual(parse_datetime("2024-02-29"), datetime(2024, 2, 29))

    def test_accepts_all_that_parse_when_accepts(self):
        for text in ("2024-03-05", "2024-3-5", "2024-03-05 14:07", "2024-03-05 4:7",
                     "2024-03-05 14:07:09", "2024-03-05T14:07:09", "2024-03-05   14:07"):
            for end in (False, True):
                self.assertEqual(parse_datetime(text, end), parse_when(text, end), text)

    def test_format_round_trips_through_parse_when(self):
        for dt in (datetime(2024, 3, 5, 14, 7, 9), datetime(1, 1, 1),
                   datetime(9999, 12, 31, 23, 59, 59), datetime(2024, 2, 29, 0, 0, 1)):
            text = format_datetime(dt)
            self.assertEqual(text, "%04d-%02d-%02d %02d:%02d:%02d" % (
                dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second))
            self.assertEqual(parse_when(text), dt)
            self.assertEqual(parse_datetime(text), dt)
            if dt.year < 9999:          # parse_when(end=True) overflows on 9999-12-31
                self.assertEqual(parse_when(text, end=True), parse_datetime(text, end=True))
        self.assertEqual(format_datetime(datetime(2024, 3, 5, 14, 7, 9), seconds=False),
                         "2024-03-05 14:07")
        self.assertEqual(format_datetime(date(2024, 3, 5)), "2024-03-05 00:00:00")
        self.assertEqual(format_datetime(None), "")


class GridTest(unittest.TestCase):
    def test_shape_and_neighbour_days(self):
        g = month_grid(2026, 9)               # 1 September 2026 is a Tuesday
        self.assertEqual(len(g), 6)
        self.assertTrue(all(len(r) == 7 for r in g))
        self.assertEqual(g[0][0], date(2026, 8, 31))
        self.assertEqual(g[0][1], date(2026, 9, 1))
        flat = [d for r in g for d in r]
        self.assertEqual(flat, [flat[0] + timedelta(days=i) for i in range(42)])
        self.assertTrue(all(d.weekday() == 0 for d in (r[0] for r in g)))

    def test_first_weekday_sunday(self):
        g = month_grid(2026, 9, first_weekday=6)
        self.assertEqual(g[0][0], date(2026, 8, 30))
        self.assertTrue(all(r[0].weekday() == 6 for r in g))

    def test_leap_years(self):
        for year, last in ((2024, 29), (2023, 28), (1900, 28), (2000, 29)):
            flat = [d for r in month_grid(year, 2) for d in r]
            self.assertIn(date(year, 2, last), flat)
            self.assertEqual([d for d in flat if d.month == 2][-1].day, last)

    def test_edges_of_the_calendar(self):
        g = month_grid(9999, 12)
        flat = [d for r in g for d in r]
        self.assertIn(date(9999, 12, 31), flat)
        self.assertIsNone(flat[-1])
        g = month_grid(1, 1)                  # 0001-01-01 is a Monday
        self.assertEqual(g[0][0], date(1, 1, 1))
        self.assertIsNone(month_grid(1, 1, first_weekday=6)[0][0])
        with self.assertRaises(ValueError):
            month_grid(2024, 13)


class PresetTest(unittest.TestCase):
    NOW = datetime(2024, 3, 15, 12, 30, 45)

    def test_relative_to_reference(self):
        n = self.NOW
        self.assertEqual(preset_range("last_1h", n), (n - timedelta(hours=1), n))
        self.assertEqual(preset_range("last_24h", n), (n - timedelta(days=1), n))
        self.assertEqual(preset_range("last_7d", n), (n - timedelta(days=7), n))
        self.assertEqual(preset_range("last_30d", n), (n - timedelta(days=30), n))
        self.assertEqual(preset_range("this_month", n), (datetime(2024, 3, 1), n))
        self.assertEqual(preset_range("all", n), (None, None))
        self.assertEqual(preset_range("all", None), (None, None))
        aware = n.replace(tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(preset_range("last_1h", aware)[1], n - timedelta(hours=2))
        with self.assertRaises(ValueError):
            preset_range("next_week", n)

    def test_every_preset_is_listed(self):
        names = [n for n, _l in PRESETS]
        self.assertEqual(names, ["last_1h", "last_24h", "last_7d", "last_30d", "this_month",
                                 "all"])
        self.assertIn(("last_24h", "Last 24 hours"), PRESETS)
        for name in names:
            preset_range(name, self.NOW)


class TkCase(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        off_screen_windows(self)
        import theme
        theme.apply(self.root)
        self.root.geometry("600x300")      # off the screen (off_screen_windows)
        self.root.deiconify()
        self.root.update()

    def toplevels(self):
        return [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)] + [
            w for p in self.root.winfo_children() for w in _descendants(p)
            if isinstance(w, tk.Toplevel)]

    def key(self, widget, seq):
        """A key such as '<Shift-Next>' through the widget's bindings (helpers.send_key:
        event_generate needs the system's keyboard focus, which another program can take)."""
        parts = seq.strip("<>").split("-")
        widget.focus_set()
        send_key(widget, parts[-1], modifiers=parts[:-1])
        self.root.update()


def _descendants(w):
    out = []
    for c in w.winfo_children():
        out.append(c)
        out.extend(_descendants(c))
    return out


class PickerTest(TkCase):
    def make(self, **kw):
        self.changes = []
        p = DateTimePicker(self.root, on_change=self.changes.append, **kw)
        p.pack(padx=10, pady=10)
        self.root.update()
        return p

    def type_in(self, p, text):
        p.entry.delete(0, "end")
        p.entry.insert(0, text)
        self.key(p.entry, "<Return>")

    def test_typing_valid_and_invalid(self):
        p = self.make()
        self.assertTrue(p.placeholder.winfo_ismapped())
        self.assertEqual(p.placeholder.full_text(), "YYYY-MM-DD HH:MM:SS")
        self.type_in(p, "2024-03-05 14:07")
        self.assertEqual(p.text(), "2024-03-05 14:07:00")       # normalised
        self.assertEqual(p.get_utc(), datetime(2024, 3, 5, 14, 7))
        self.assertEqual(self.changes, [datetime(2024, 3, 5, 14, 7)])
        self.assertEqual(str(p.entry.cget("style")), "TEntry")
        self.assertFalse(p.error_label.winfo_ismapped())

        self.type_in(p, "2023-02-29")
        self.assertEqual(str(p.entry.cget("style")), "Invalid.TEntry")
        self.assertTrue(p.error_label.winfo_ismapped())
        self.assertIn("February 2023 has 28 days", str(p.error_label.cget("text")))
        self.assertEqual(p.error(), "February 2023 has 28 days")
        self.assertIsNone(p.get_utc())
        self.assertEqual(p.text(), "2023-02-29")                # left for the user to fix
        self.assertEqual(len(self.changes), 1)                  # nothing taken

        self.type_in(p, "2023-02-28")
        self.assertEqual(str(p.entry.cget("style")), "TEntry")
        self.assertFalse(p.error_label.winfo_ismapped())
        self.assertEqual(p.error(), "")
        self.assertEqual(self.changes[-1], datetime(2023, 2, 28))

        self.type_in(p, "")
        self.assertIsNone(p.get_utc())
        self.assertEqual(p.error(), "")
        self.assertIsNone(self.changes[-1])
        self.assertTrue(p.placeholder.winfo_ismapped())

    def test_focus_out_validates(self):
        p = self.make()
        other = tk.Entry(self.root)
        other.pack()
        p.entry.insert(0, "2024-01-01 25:00")
        p.entry.focus_set()
        self.root.update()
        other.focus_set()
        p.entry.event_generate("<FocusOut>")     # what Tk sends as the focus leaves it
        self.root.update()
        self.assertEqual(str(p.entry.cget("style")), "Invalid.TEntry")
        self.assertEqual(p.error(), "Hour must be 00-23")

    def test_end_field_and_parse_when(self):
        p = self.make(end=True)
        self.type_in(p, "2024-03-05")
        self.assertEqual(p.text(), "2024-03-05 23:59:59")
        self.assertEqual(p.get_utc(), datetime(2024, 3, 5, 23, 59, 59, 999999))
        self.assertEqual(parse_when(p.text(), end=True), p.get_utc())
        q = self.make()
        q.set_utc(datetime(2024, 3, 5, 1, 2, 3))
        self.assertEqual(parse_when(q.text()), q.get_utc())

    def test_local_display(self):
        p = self.make(offset_minutes=330, mode="local")
        self.assertEqual(p.zone_text(), "UTC+05:30")
        self.assertEqual(str(p.zone_label.cget("text")), "UTC+05:30")
        p.set_utc(datetime(2024, 3, 5, 20, 0, 0))
        self.assertEqual(p.text(), "2024-03-06 01:30:00")
        self.assertEqual(p.get_utc(), datetime(2024, 3, 5, 20, 0, 0))
        self.type_in(p, "2024-01-01 05:30")
        self.assertEqual(p.get_utc(), datetime(2024, 1, 1, 0, 0))
        p.set_mode("utc")
        self.assertEqual(p.text(), "2024-01-01 00:00:00")
        self.assertEqual(p.zone_text(), "UTC")
        self.assertEqual(p.get_utc(), datetime(2024, 1, 1, 0, 0))
        p.set_mode("local", -480)
        self.assertEqual(p.text(), "2023-12-31 16:00:00")
        aware = datetime(2024, 1, 1, 2, 0, tzinfo=timezone(timedelta(hours=2)))
        p.set_utc(aware)
        self.assertEqual(p.get_utc(), datetime(2024, 1, 1, 0, 0))
        with self.assertRaises(ValueError):
            p.set_mode("mars")

    def test_out_of_range_in_local_time_is_a_message(self):
        p = self.make(offset_minutes=330, mode="local")
        self.type_in(p, "0001-01-01 01:00")
        self.assertIn("Out of range", p.error())
        self.assertIsNone(p.get_utc())
        p = self.make(offset_minutes=-60, mode="local")
        self.type_in(p, "9999-12-31 23:30")
        self.assertIn("Out of range", p.error())

    def open(self, p):
        p.open()
        self.root.update()
        self.assertTrue(p.is_open())
        return p.popup

    def test_popup_opens_on_the_value_month(self):
        p = self.make()
        p.set_utc(datetime(2024, 2, 29, 8, 9, 10))
        pop = self.open(p)
        self.assertEqual(pop.view, (2024, 2))
        self.assertEqual(str(pop.title.cget("text")), "February 2024")
        self.assertEqual(pop.sel, date(2024, 2, 29))
        self.assertEqual((pop.hh.get(), pop.mm.get(), pop.ss.get()), ("08", "09", "10"))
        self.assertIn(date(2024, 2, 29), pop.cells())
        self.assertEqual(len(pop.grid), 6)
        self.assertTrue(pop.win.winfo_ismapped())
        self.assertTrue(bool(pop.win.overrideredirect()))

    def test_keys_pick_a_day(self):
        p = self.make()
        p.set_utc(datetime(2024, 1, 31, 10, 0, 0))
        pop = self.open(p)
        c = pop.canvas
        self.key(c, "<Right>")
        self.assertEqual(pop.sel, date(2024, 2, 1))
        self.assertEqual(pop.view, (2024, 2))
        self.key(c, "<Down>")
        self.assertEqual(pop.sel, date(2024, 2, 8))
        self.key(c, "<Left>")
        self.key(c, "<Up>")
        self.assertEqual(pop.sel, date(2024, 1, 31))
        self.key(c, "<Home>")                   # Monday of that week
        self.assertEqual(pop.sel, date(2024, 1, 29))
        self.key(c, "<End>")
        self.assertEqual(pop.sel, date(2024, 2, 4))
        self.key(c, "<Return>")
        self.assertFalse(p.is_open())
        self.assertEqual(p.get_utc(), datetime(2024, 2, 4, 10, 0, 0))
        self.assertEqual(self.changes, [datetime(2024, 2, 4, 10, 0, 0)])
        self.assertEqual(self.toplevels(), [])

    def test_page_keys_change_month_and_year(self):
        p = self.make()
        p.set_utc(datetime(2024, 1, 31))
        pop = self.open(p)
        self.key(pop.canvas, "<Next>")
        self.assertEqual(pop.sel, date(2024, 2, 29))            # clamped to the month
        self.assertEqual(str(pop.title.cget("text")), "February 2024")
        self.key(pop.canvas, "<Prior>")
        self.assertEqual(pop.sel, date(2024, 1, 29))
        self.key(pop.canvas, "<Shift-Next>")
        self.assertEqual(pop.sel, date(2025, 1, 29))
        self.key(pop.canvas, "<Shift-Prior>")
        self.assertEqual(pop.sel, date(2024, 1, 29))
        pop.step_view(1)
        self.assertEqual(pop.view, (2024, 2))
        self.assertEqual(pop.sel, date(2024, 1, 29))            # the buttons only look

    def test_edges_never_raise(self):
        p = self.make()
        p.set_utc(datetime(9999, 12, 31))
        pop = self.open(p)
        for seq in ("<Right>", "<Next>", "<Shift-Next>", "<Down>", "<End>"):
            self.key(pop.canvas, seq)
        self.assertEqual(pop.sel, date(9999, 12, 31))
        pop.step_view(12)
        self.assertEqual(pop.view, (9999, 12))
        p.close()
        p.set_utc(datetime(1, 1, 1))
        pop = self.open(p)
        for seq in ("<Left>", "<Prior>", "<Shift-Prior>", "<Up>", "<Home>"):
            self.key(pop.canvas, seq)
        self.assertEqual(pop.sel, date(1, 1, 1))

    def test_time_spinboxes(self):
        p = self.make()
        p.set_utc(datetime(2024, 5, 6, 1, 2, 3))
        pop = self.open(p)
        pop.hh.set("23")
        pop.mm.set("45")
        pop.ss.set("99")                        # clamped
        hh = pop.spins[0]
        hh.event_generate("<<Increment>>")      # wraps 23 -> 00
        self.root.update()
        self.assertEqual(int(pop.hh.get()), 0)
        pop.hh.set("17")
        pop.apply()
        self.root.update()
        self.assertEqual(p.get_utc(), datetime(2024, 5, 6, 17, 45, 59))
        self.assertFalse(p.is_open())

    def test_escape_leaves_value(self):
        p = self.make()
        p.set_utc(datetime(2024, 5, 6, 1, 2, 3))
        pop = self.open(p)
        self.key(pop.canvas, "<Right>")
        self.key(pop.canvas, "<Escape>")
        self.assertFalse(p.is_open())
        self.assertEqual(p.get_utc(), datetime(2024, 5, 6, 1, 2, 3))
        self.assertEqual(self.changes, [])

    def test_clear_and_today(self):
        p = self.make()
        p.set_utc(datetime(2024, 5, 6, 1, 2, 3))
        pop = self.open(p)
        pop.go_today()
        self.assertEqual(pop.sel, p.today())
        pop.clear()
        self.root.update()
        self.assertFalse(p.is_open())
        self.assertIsNone(p.get_utc())
        self.assertEqual(p.text(), "")
        self.assertEqual(self.changes, [None])

    def test_click_and_double_click(self):
        p = self.make()
        p.set_utc(datetime(2024, 5, 6))
        pop = self.open(p)
        x0, y0, x1, y1 = pop.cells()[date(2024, 5, 20)]
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        pop.canvas.event_generate("<Button-1>", x=cx, y=cy)
        self.root.update()
        self.assertEqual(pop.sel, date(2024, 5, 20))
        self.assertTrue(p.is_open())
        pop._double(type("E", (), {"x": cx, "y": cy})())
        self.root.update()
        self.assertFalse(p.is_open())
        self.assertEqual(p.get_utc(), datetime(2024, 5, 20))

    def test_empty_field_opens_on_today(self):
        p = self.make(end=True)
        pop = self.open(p)
        self.assertEqual(pop.sel, p.today())
        self.assertFalse(pop.has_sel)
        self.assertEqual((pop.hh.get(), pop.mm.get(), pop.ss.get()), ("23", "59", "59"))
        self.key(pop.canvas, "<Return>")
        t = p.today()
        self.assertEqual(p.get_utc(), datetime(t.year, t.month, t.day, 23, 59, 59, 999999))

    def test_zone_toggle_in_popup(self):
        modes = []
        p = self.make(offset_minutes=330, mode="utc", on_mode=modes.append)
        p.set_utc(datetime(2024, 3, 5, 20, 0, 0))
        pop = self.open(p)
        self.assertEqual(str(pop.local_btn.cget("text")), "Local time (UTC+05:30)")
        self.assertEqual(str(pop.utc_btn.cget("style")), "DatePickerOn.TButton")
        pop.set_zone("local")
        self.assertEqual(modes, ["local"])
        self.assertEqual(p.mode, "local")
        self.assertEqual(pop.sel, date(2024, 3, 6))
        self.assertEqual((pop.hh.get(), pop.mm.get()), ("01", "30"))
        self.assertEqual(p.text(), "2024-03-06 01:30:00")
        pop.apply()
        self.assertEqual(p.get_utc(), datetime(2024, 3, 5, 20, 0, 0))

    def test_focus_leaving_closes(self):
        p = self.make()
        other = tk.Entry(self.root)
        other.pack()
        pop = self.open(p)
        pop.canvas.event_generate("<FocusIn>")
        self.root.update()
        self.assertTrue(pop._had_focus)
        # another program takes the keyboard focus: the calendar stays open
        pop.win.focus_get = lambda: None
        pop.canvas.event_generate("<FocusOut>")
        self.root.update()
        self.root.update()
        self.assertTrue(p.is_open())
        # the focus goes to another widget of the app: it closes
        pop.win.focus_get = lambda: other
        pop.canvas.event_generate("<FocusOut>")
        self.root.update()
        self.root.update()
        self.assertFalse(p.is_open())

    def test_destroy_while_open(self):
        p = self.make()
        self.open(p)
        self.assertEqual(len(self.toplevels()), 1)
        p.destroy()
        self.root.update()
        self.assertEqual(self.toplevels(), [])
        self.assertIsNone(p.get_utc())          # no exception after the destroy

    def test_textvariable_shared(self):
        var = tk.StringVar(self.root, value="2024-03-05")
        p = self.make(textvariable=var)
        self.assertEqual(var.get(), "2024-03-05 00:00:00")
        self.assertEqual(p.get_utc(), datetime(2024, 3, 5))
        p.destroy()
        var.set("2024-01-01")                   # the trace went with the picker


class RangeTest(TkCase):
    NOW = datetime(2024, 3, 15, 12, 30, 45)

    def make(self, **kw):
        self.changes = []
        r = DateRangePicker(self.root, reference_time=lambda: self.NOW,
                            on_change=lambda s, e: self.changes.append((s, e)), **kw)
        r.pack(padx=10, pady=10)
        self.root.update()
        return r

    def test_presets_fill_both_ends(self):
        r = self.make()
        self.assertEqual(r.preset_label(), "Range")
        r.menu.invoke(1)                        # Last 24 hours
        self.root.update()
        self.assertEqual(r.preset(), "last_24h")
        self.assertEqual(r.preset_label(), "Last 24 hours")
        s, e = r.get_range()
        self.assertEqual(s, datetime(2024, 3, 14, 12, 30, 45))
        self.assertEqual(e, datetime(2024, 3, 15, 12, 30, 45, 999999))
        self.assertEqual(r.start.text(), "2024-03-14 12:30:45")
        self.assertEqual(r.end.text(), "2024-03-15 12:30:45")
        self.assertEqual(self.changes, [(s, e)])
        r.apply_preset("this_month")
        self.assertEqual(r.get_range()[0], datetime(2024, 3, 1))
        r.apply_preset("all")
        self.assertEqual(r.get_range(), (None, None))
        self.assertEqual(r.preset_label(), "All time")

    def test_to_before_from(self):
        r = self.make()
        r.start.entry.insert(0, "2024-03-10")
        self.key(r.start.entry, "<Return>")
        r.end.entry.insert(0, "2024-03-01")
        self.key(r.end.entry, "<Return>")
        self.assertTrue(r.error_label.winfo_ismapped())
        self.assertEqual(str(r.error_label.cget("text")), "‘To’ is before ‘From’")
        self.assertEqual(str(r.end.entry.cget("style")), "Invalid.TEntry")
        with self.assertRaises(ValueError) as cm:
            r.get_range()
        self.assertIn("before", str(cm.exception))
        self.assertEqual(self.changes, [(datetime(2024, 3, 10), None)])
        r.end.entry.delete(0, "end")
        r.end.entry.insert(0, "2024-03-10")     # the same day: fine (To is its end)
        self.key(r.end.entry, "<Return>")
        self.assertFalse(r.error_label.winfo_ismapped())
        self.assertEqual(str(r.end.entry.cget("style")), "TEntry")
        self.assertEqual(r.get_range(), (datetime(2024, 3, 10),
                                         datetime(2024, 3, 10, 23, 59, 59, 999999)))

    def test_bad_text_raises(self):
        r = self.make()
        r.start.entry.insert(0, "2024-02-30")
        self.key(r.start.entry, "<Return>")
        with self.assertRaises(ValueError) as cm:
            r.get_range()
        self.assertIn("From:", str(cm.exception))

    def test_typing_switches_to_custom(self):
        r = self.make()
        r.apply_preset("last_7d")
        self.assertEqual(r.preset_label(), "Last 7 days")
        r.start.entry.insert("end", "x")
        self.root.update()
        self.assertEqual(r.preset(), "custom")
        self.assertEqual(r.preset_label(), "Custom")

    def test_set_range_and_mode(self):
        r = self.make(offset_minutes=330, mode="local")
        self.assertEqual(str(r.zone_label.cget("text")), "UTC+05:30")
        r.set_range(datetime(2024, 1, 1), datetime(2024, 1, 2, 0, 0, 0))
        self.assertEqual(r.start.text(), "2024-01-01 05:30:00")
        self.assertEqual(r.preset_label(), "Custom")
        s, e = r.get_range()
        self.assertEqual(s, datetime(2024, 1, 1))
        self.assertEqual(e, datetime(2024, 1, 2, 0, 0, 0, 999999))
        r.set_mode("utc")
        self.assertEqual(r.start.text(), "2024-01-01 00:00:00")
        self.assertEqual(r.end.text(), "2024-01-02 00:00:00")
        self.assertEqual(str(r.zone_label.cget("text")), "UTC")
        self.assertEqual(r.get_range(), (s, e))
        r.set_range(None, None)
        self.assertEqual(r.get_range(), (None, None))

    def test_popup_shows_the_range(self):
        r = self.make()
        r.set_range(datetime(2024, 3, 3), datetime(2024, 3, 9))
        r.end.open()
        self.root.update()
        pop = r.end.popup
        self.assertEqual(pop.sel, date(2024, 3, 9))
        self.assertEqual(r.end.partner_date(), date(2024, 3, 3))
        fills = set()
        for item in pop.canvas.find_withtag("cell"):
            fills.add(pop.canvas.itemcget(item, "fill").lower())
        self.assertIn("#dbeafe", fills)         # primary_soft between the two dates
        self.assertIn("#1e40af", fills)         # the selected day
        pop.set_zone("local")                   # shared: the From field follows
        self.assertEqual(r.start.mode, "local")

    def test_destroy_while_open(self):
        r = self.make()
        r.start.open()
        self.root.update()
        self.assertEqual(len(self.toplevels()), 1)
        r.destroy()
        self.root.update()
        self.assertEqual(self.toplevels(), [])


if __name__ == "__main__":
    unittest.main()
