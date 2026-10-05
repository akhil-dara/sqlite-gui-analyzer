"""Column filters: type detection, the conditions the popover builds (each read back by the
engine and selecting exactly the expected rows of a fixture, in SQL and natively), the value
checklist, chips, history, copies, quick filters, the empty state and the popover's workers.
Tk parts are checked by widget introspection in a window placed off the screen."""

import os
import random
import sqlite3
import threading
import time
import unittest
from datetime import datetime, timedelta

from tests.helpers import TempDirTest, off_screen_windows, send_key, tk_root

import colfilter as cf
from engine import limits
from engine import timeline as tl
from engine.backends import Filter
from engine.filters import parse_expr
from engine.schema import Locator

MS0 = 1772323200000                     # 2026-03-01 00:00:00 UTC in Unix milliseconds
WEBKIT_EPOCH_GAP_US = 11644473600 * 1000000


def make_fixture(path, n=600, seed=7):
    """A table of every column kind the popover knows; returns the rows as inserted."""
    rnd = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT, status INTEGER, "
                 "score REAL, ts_ms INTEGER, ts_s INTEGER, webkit INTEGER, iso TEXT, "
                 "flag INTEGER, data BLOB, js TEXT)")
    rows = []
    for i in range(1, n + 1):
        ms = MS0 + i * 3600 * 1000 + rnd.randrange(0, 3600000)
        dt = tl.to_utc(ms, "unix_ms")
        name = rnd.choice(["alpha", "beta", "otp code", "", None, "Gamma %d" % i, "o'hara ?"])
        row = (i, name, rnd.choice([1, 2, 3, None]), round(rnd.random() * 100, 3), ms,
               ms // 1000, ms * 1000 + WEBKIT_EPOCH_GAP_US,
               dt.strftime("%Y-%m-%d %H:%M:%S"), i % 2,
               bytes([i % 7]) * 4 if i % 5 else (b"\x89PNG" + bytes(400) if i % 10 == 0 else None),
               '{"k": %d}' % i)
        rows.append(row)
    conn.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return rows


COLS = ["id", "name", "status", "score", "ts_ms", "ts_s", "webkit", "iso", "flag", "data", "js"]


class Fixture(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        limits.reset()
        self.addCleanup(limits.reset)
        self.path = os.path.join(self.tmp, "fixture.db")
        self.rows = make_fixture(self.path)
        from database import DB
        self.db = DB()
        self.db.open(self.path)
        self.addCleanup(self.db.close)

    def source(self):
        from browse_sources import TableSource
        return TableSource(self.db, "t", self.db.count("t"))

    def list_source(self):
        from browse_sources import ListSource
        return ListSource(["_rid"] + COLS, [([Locator("rowid", r[0])] + list(r), set())
                                            for r in self.rows])

    def ids_sql(self, col, text):
        """ids the engine selects for a column filter through SQLite."""
        return sorted(r[1] for r in self.db.iter_filtered("t", Filter(col_exprs={col: text})))

    def ids_py(self, col, text):
        """ids the engine selects matching each value in Python (natively read rows)."""
        e = parse_expr(text)
        i = COLS.index(col)
        return sorted(r[0] for r in self.rows if e.match(r[i]))

    def expect(self, col, text, pred):
        i = COLS.index(col)
        want = sorted(r[0] for r in self.rows if pred(r[i]))
        self.assertEqual(self.ids_sql(col, text), want, "%s %s (SQL)" % (col, text))
        self.assertEqual(self.ids_py(col, text), want, "%s %s (Python)" % (col, text))
        return want


class TypeDetectionTest(unittest.TestCase):
    def test_kinds(self):
        det = cf.detect_type
        self.assertEqual(det("name", "TEXT", ["alpha %d" % i for i in range(40)]).kind, "text")
        self.assertEqual(det("amount", "", [i * 1.5 + 0.25 for i in range(40)]).kind, "number")
        ct = det("created", "INTEGER", [MS0 + i * 997 * 1000 for i in range(40)])
        self.assertEqual((ct.kind, ct.date_kind), ("date", "unix_ms"))
        self.assertEqual(ct.label, "Date · Unix ms")
        ct = det("when", "TEXT", ["2026-03-%02dT10:%02d:00" % (d, d) for d in range(1, 28)])
        self.assertEqual((ct.kind, ct.date_kind, ct.iso_style), ("date", "iso_text", "T"))
        self.assertEqual(det("is_read", "INTEGER", [0, 1] * 20).kind, "bool")
        ct = det("status", "", [1, 2, 3, 7] * 10)
        self.assertEqual((ct.kind, ct.base), ("enum", "number"))
        self.assertEqual(det("state", "", ["new", "sent", "read"] * 10).base, "text")
        self.assertEqual(det("thumb", "BLOB", [b"\x89PNG" + bytes(i) for i in range(20)]).kind,
                         "blob")
        self.assertEqual(det("payload", "", ['{"a": %d}' % i for i in range(30)]).kind, "json")
        # the kind a column is shown as wins; no values: the declared type
        self.assertEqual(det("x", "", [5], date_kind="webkit_us").date_kind, "webkit_us")
        self.assertEqual(det("n", "REAL", []).kind, "number")
        self.assertEqual(det("b", "BLOB", [None, None]).kind, "blob")


class ConditionTest(Fixture):
    def test_text_conditions_select_the_expected_rows(self):
        ct = cf.ColumnType("text")
        low = lambda v: v.lower() if isinstance(v, str) else None       # noqa: E731
        for op, args, pred in (
                ("contains", ("OTP",), lambda v: v is not None and "otp" in low(v)),
                ("not_contains", ("otp",), lambda v: v is None or "otp" not in low(v)),
                ("equals", ("alpha",), lambda v: v == "alpha"),
                ("not_equals", ("alpha",), lambda v: v is not None and v != "alpha"),
                ("starts", ("gam",), lambda v: v is not None and low(v).startswith("gam")),
                ("ends", ("A",), lambda v: v is not None and low(v).endswith("a")),
                ("contains", ("'hara ?",), lambda v: v is not None and "'hara ?" in v),
                ("regex", (r"^Gamma \d+$", False), lambda v: v is not None and
                 v.startswith("Gamma ")),
                ("empty", (), lambda v: v in (None, "")),
                ("not_empty", (), lambda v: v not in (None, ""))):
            text = cf.compile_condition(ct, op, args)
            self.assertIsNotNone(parse_expr(text))
            self.assertTrue(self.expect("name", text, pred), (op, text))
        self.assertIsNone(cf.compile_condition(ct, "contains", ("",)))     # nothing asked yet
        with self.assertRaises(ValueError):
            cf.compile_condition(ct, "regex", ("([", False))

    def test_number_conditions_and_two_joined(self):
        ct = cf.ColumnType("number")
        for op, args, pred in (("gt", ("50",), lambda v: v > 50),
                               ("le", ("12.5",), lambda v: v <= 12.5),
                               ("between", ("10", "20"), lambda v: 10 <= v <= 20),
                               ("not_equals", ("0",), lambda v: v != 0)):
            self.expect("score", cf.compile_condition(ct, op, args), pred)
        a = cf.compile_condition(ct, "lt", ("10",))
        b = cf.compile_condition(ct, "gt", ("90",))
        self.expect("score", cf.join_conditions([a, b], "OR"), lambda v: v < 10 or v > 90)
        self.expect("score", cf.join_conditions([cf.compile_condition(ct, "ge", ("10",)), b],
                                                "AND"), lambda v: v > 90)
        with self.assertRaises(ValueError):
            cf.compile_condition(ct, "gt", ("ten",))
        with self.assertRaises(ValueError):
            cf.compile_condition(ct, "between", ("20", "10"))

    def test_top_n_threshold(self):
        src = self.source()
        scores = sorted((r[3] for r in self.rows), reverse=True)
        th = cf.source_nth(src, "score", None, 10, True)
        self.assertEqual(th, scores[9])
        self.assertEqual(cf.source_nth(self.list_source(), "score", None, 10, True), scores[9])
        text = cf.compile_condition(cf.ColumnType("number"), "top", ("10",), threshold=th)
        self.assertEqual(len(self.expect("score", text, lambda v: v >= scores[9])), 10)
        bottom = cf.source_nth(src, "score", None, 5, False)
        self.assertEqual(bottom, sorted(r[3] for r in self.rows)[4])
        # with the other filters applied, and fewer numbers than N: the last one
        flt = Filter(col_exprs={"id": "<=3"})
        self.assertEqual(cf.source_nth(src, "score", flt, 10, True),
                         min(r[3] for r in self.rows[:3]))
        self.assertIsNone(cf.compile_condition(cf.ColumnType("number"), "top", ("10",)))

    def test_date_conditions_on_each_stored_kind(self):
        now = datetime(2026, 3, 20, 12, 0, 0)
        start, end = datetime(2026, 3, 2, 6, 0), datetime(2026, 3, 5, 23, 59, 59, 999999)
        day = datetime(2026, 3, 10, 15, 30)
        for col, kind in (("ts_ms", "unix_ms"), ("ts_s", "unix_s"), ("webkit", "webkit_us"),
                          ("iso", "iso_text")):
            ct = cf.ColumnType("date", date_kind=kind, iso_style=" ")
            dec = lambda v, k=kind: tl.to_utc(v, k)                     # noqa: E731
            cases = (
                ("between", (start, end), lambda v: start <= dec(v) <= end),
                ("before", (start,), lambda v: dec(v) < start),
                ("after", (end,), lambda v: dec(v) > end),
                ("on", (day,), lambda v: dec(v).date() == day.date()),
                ("last_days", ("3",), lambda v: dec(v) >= now - timedelta(days=3)),
                ("this_month", (), lambda v: (dec(v).year, dec(v).month) == (2026, 3)))
            for op, args, pred in cases:
                text = cf.compile_condition(ct, op, args, now=now)
                got = self.expect(col, text, pred)
                self.assertTrue(got, (col, op, text))
            # the chip words and the builder read it back
            text = cf.compile_condition(ct, "between", (datetime(2026, 3, 1), datetime(
                2026, 3, 5, 23, 59, 59, 999999)))
            self.assertEqual(cf.describe_filter(col, text, ct), "%s: 1 Mar – 5 Mar 2026"
                             % col)
            st = cf.builder_state(text, ct)
            self.assertEqual(st["conds"], [("between", (datetime(2026, 3, 1), datetime(
                2026, 3, 5, 23, 59, 59, 999999)))])
            on = cf.compile_condition(ct, "on", (day,))
            self.assertEqual(cf.describe_filter(col, on, ct), "%s: on 10 Mar 2026" % col)
            self.assertEqual(cf.builder_state(on, ct)["conds"], [("on", (datetime(2026, 3, 10),))])
        # raw values of the stored kind: whole numbers of milliseconds
        ct = cf.ColumnType("date", date_kind="unix_ms")
        self.assertEqual(cf.compile_condition(ct, "before", (datetime(2026, 3, 1),)),
                         "<%d" % MS0)
        iso_t = cf.ColumnType("date", date_kind="iso_text", iso_style="T")
        self.assertEqual(cf.compile_condition(iso_t, "after", (datetime(2026, 3, 1, 10),)),
                         ">2026-03-01T10:00:00")

    def test_describe_and_builder_state(self):
        ct = cf.ColumnType("number")
        self.assertEqual(cf.describe_filter("status", "=3", ct), "status = 3")
        self.assertEqual(cf.describe_filter("text", "otp"), "text contains 'otp'")
        self.assertEqual(cf.describe_filter("status", "IN (1, 2)"), "status is 1 or 2")
        self.assertEqual(cf.describe_filter("status", "NOT IN (3)"), "status ≠ 3")
        self.assertEqual(cf.describe_filter(None, None, words="a b"),
                         "rows contain 'a' and 'b'")
        st = cf.builder_state("{>=10} OR {<2}", ct)
        self.assertEqual((st["conds"], st["join"]), ([("ge", ("10",)), ("lt", ("2",))], "OR"))
        st = cf.builder_state("{*=abc} AND {NOT IN (x, y)}", cf.ColumnType("text"))
        self.assertEqual(st["conds"], [("contains", ("abc",))])
        self.assertEqual(st["list"].kind, "notin")
        self.assertEqual(cf.builder_state("a%b", cf.ColumnType("text"))["conds"],
                         [("expr", ("a%b",))])

    def test_checklist_in_or_not_in_blanks_and_nothing_ticked(self):
        pairs = [(None, 5), ("", 2), ("a", 50), ("b", 30), ("c", 10), ("d", 1)]
        entries = cf.build_entries(pairs)
        self.assertEqual([e.label for e in entries], ["(Blanks)", "'a'", "'b'", "'c'", "'d'"])
        keys = dict((e.label, e.key) for e in entries)
        all_keys = set(keys.values())
        self.assertIsNone(cf.checklist_text(entries, all_keys, True))       # all: no filter
        # one unticked: NOT IN is shorter
        self.assertEqual(cf.checklist_text(entries, all_keys - {keys["'b'"]}, True),
                         "NOT IN (b)")
        # two ticked: IN is shorter
        self.assertEqual(cf.checklist_text(entries, {keys["'a'"], keys["'c'"]}, True),
                         "IN (a, c)")
        # (Blanks) unticked: NULL and '' both left out (NOT IN keeps NULL unless listed)
        text = cf.checklist_text(entries, all_keys - {keys["(Blanks)"]}, True)
        self.assertEqual(text, 'NOT IN (NULL, "")')
        e = parse_expr(text)
        self.assertEqual([v for v in (None, "", "a", "z") if e.match(v)], ["a", "z"])
        text = cf.checklist_text(entries, {keys["(Blanks)"]}, True)
        self.assertEqual([v for v in (None, "", "a") if parse_expr(text).match(v)], [None, ""])
        with self.assertRaises(cf.NothingTicked):
            cf.checklist_text(entries, set(), True)
        # a list cut by the limit: values not listed stay unless 'Select none' was used
        self.assertEqual(cf.checklist_text(entries, all_keys - {keys["'d'"]}, False),
                         "NOT IN (d)")
        self.assertEqual(cf.checklist_text(entries, {keys["'a'"]}, False, only_ticked=True),
                         "IN (a)")
        self.assertEqual(cf.checklist_text(entries, all_keys, False, only_ticked=True),
                         'IN (NULL, "", a, b, c, d)')
        # a value that cannot be written: the other way round, or said
        big = cf.build_entries([(cf.LargeBlob(9000), 3), (b"\x01", 1), (b"\x02", 1)])
        bk = [e.key for e in big]
        self.assertFalse(big[0].expressible)
        self.assertEqual(cf.checklist_text(big, {bk[0], bk[1]}, True), "NOT IN (x'02')")
        with self.assertRaises(ValueError):
            cf.checklist_text(big, {bk[0]}, False, only_ticked=True)

    def test_distinct_values_capped_and_scan_limit(self):
        src = self.source()
        r = cf.source_distinct(src, "status", None, 100, 10 ** 9)
        want = {}
        for row in self.rows:
            want[row[2]] = want.get(row[2], 0) + 1
        self.assertEqual(dict(r.values), want)
        self.assertEqual((r.total, r.capped, r.scan_capped, r.scanned), (4, False, False, 600))
        self.assertEqual([n for _v, n in r.values], sorted(want.values(), reverse=True))
        # the other columns' filters apply
        r = cf.source_distinct(src, "status", Filter(col_exprs={"flag": "=1"}), 100, 10 ** 9)
        self.assertEqual(sum(n for _v, n in r.values), 300)
        # in memory: the same answer
        g = cf.source_distinct(self.list_source(), "status", None, 100, 10 ** 9)
        self.assertEqual(dict(g.values), want)
        # the limits: 10 values listed of 600; 1,000 rows read at most (of 600: all)
        limits.load({"limits": {"filter_distinct_values": 10}})
        r = cf.source_distinct(src, "id", None, limits.get("filter_distinct_values"),
                               limits.get("filter_distinct_scan_rows"))
        self.assertEqual((len(r.values), r.total, r.capped), (10, 600, True))
        r = cf.source_distinct(src, "id", None, 10, 100)
        self.assertEqual((r.scanned, r.scan_capped), (100, True))
        g = cf.source_distinct(self.list_source(), "id", None, 10, 100)
        self.assertEqual((len(g.values), g.scanned, g.scan_capped), (10, 100, True))
        # large BLOBs come back as their size
        r = cf.source_distinct(src, "data", None, 100, 10 ** 9)
        self.assertIn(cf.LargeBlob(404), [v for v, _n in r.values])

    def test_distinct_groups_equal_numbers_and_says_so(self):
        path = os.path.join(self.tmp, "mixed.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE m(x)")
        conn.executemany("INSERT INTO m VALUES (?)", [(5,), (5.0,), ("5",), (7,), (None,)])
        conn.commit()
        conn.close()
        from database import DB
        from browse_sources import TableSource
        db = DB()
        db.open(path)
        self.addCleanup(db.close)
        src = TableSource(db, "m", 5)
        fast = cf.source_distinct(src, "x", None, 100, 10 ** 6)     # every row: GROUP BY x
        self.assertTrue(fast.merged)
        self.assertEqual(sorted(n for _v, n in fast.values), [1, 1, 1, 2])
        self.assertIsNone(cf.count_from_distinct(fast, "*=5"))       # counted by the worker
        exact = cf.source_distinct(src, "x", None, 100, 4)           # rows cut: by storage class
        self.assertFalse(exact.merged)
        self.assertEqual(sorted(n for _v, n in exact.values), [1, 1, 1, 1])
        self.assertEqual((exact.scanned, exact.scan_capped), (4, True))

    def test_counts_estimate_and_date_bins(self):
        src = self.source()
        flt = Filter(col_exprs={"status": "=2"})
        n = sum(1 for r in self.rows if r[2] == 2)
        self.assertEqual(cf.source_count(src, flt), n)
        self.assertEqual(cf.source_count(self.list_source(), flt), n)
        self.assertIsNone(cf.source_estimate(src, flt))       # a small table: exact at once
        est = src.estimate_matching(Filter(col_exprs={"id": ">0"}), slices=4, per_slice=10)
        self.assertEqual(est, (600, 40))                    # 4 rowid ranges of 10 rows read
        # too few matches in the sample: no estimate (the exact count follows)
        self.assertIsNone(src.estimate_matching(Filter(col_exprs={"id": "=7"}), slices=4,
                                                per_slice=10))
        r = cf.source_distinct(src, "status", None, 100, 10 ** 9)
        self.assertEqual(cf.count_from_distinct(r, "=2"), n)
        self.assertEqual(cf.count_from_distinct(r, None), 600)
        for col, kind in (("ts_ms", "unix_ms"), ("iso", "iso_text")):
            b = cf.source_date_bins(src, col, None, kind, 40, 10 ** 9)
            self.assertEqual(sum(b.totals), 600, col)
            self.assertEqual(b.first, min(tl.to_utc(r[COLS.index(col)], kind) for r in self.rows))
            self.assertEqual(len(b.edges), len(b.totals) + 1)
            g = cf.source_date_bins(self.list_source(), col, None, kind, 40, 10 ** 9)
            self.assertEqual(sum(g.totals), 600)

    def test_estimate_on_a_large_table(self):
        path = os.path.join(self.tmp, "big.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE b(x INTEGER)")
        conn.executemany("INSERT INTO b VALUES (?)", ((i % 10,) for i in range(40000)))
        conn.commit()
        conn.close()
        from database import DB
        from browse_sources import TableSource
        db = DB()
        db.open(path)
        self.addCleanup(db.close)
        src = TableSource(db, "b", 40000)
        est, seen = src.estimate_matching(Filter(col_exprs={"x": "<3"}), slices=8, per_slice=500)
        self.assertEqual(seen, 4000)
        self.assertEqual(est, 12000)
        self.assertEqual(cf.source_count(src, Filter(col_exprs={"x": "<3"})), 12000)

    def test_copy_as_sql_where(self):
        exprs = {"name": "*=o'hara ?", "score": ">50"}
        text = cf.sql_where_text(exprs, "alpha", ["id", "name", "score"])
        self.assertTrue(text.startswith("WHERE "))
        self.assertIn("'%o''hara ?%'", text)
        self.assertIn("> 50", text)
        self.assertNotIn(" ? ", text.replace("'%o''hara ?%'", ""))
        conn = sqlite3.connect(self.path)          # the fixture (never evidence)
        try:
            got = sorted(r[0] for r in conn.execute("SELECT id FROM t " + text))
        finally:
            conn.close()
        flt = Filter(col_exprs=exprs, words=["alpha"])
        want = sorted(r[0] for r in self.rows
                      if flt.matches(["id", "name", "score"], [r[0], r[1], r[3]]))
        self.assertEqual(got, want)
        self.assertEqual(cf.sql_where_text({}, "", ["a"]), "")
        self.assertEqual(cf.filter_lines([("a", ">1")], "x y"), "a: >1\nsearch: x y")


# -- the grid ---------------------------------------------------------------------------------------
class GridFilterTest(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.root = tk_root(self)
        off_screen_windows(self)
        from widgets import setup_theme
        setup_theme(self.root)
        self.root.geometry("1100x640")
        self.root.deiconify()
        self.errors = []
        self.root.report_callback_exception = lambda *a: self.errors.append(a[1])
        from grid import DataGrid
        self.grid = DataGrid(self.root, frozen=1, filter_delay=10)
        self.grid.pack(fill="both", expand=True)
        self.root.update()
        self.addCleanup(self._close)

    def _close(self):
        workers = self.grid.worker_threads()
        try:
            self.grid.destroy()
        except Exception:               # noqa: BLE001
            pass
        for t in workers:
            t.join(10)
        try:
            self.root.update_idletasks()    # nothing left pending when the window goes
        except Exception:               # noqa: BLE001
            pass
        self.assertEqual(self.errors, [])

    def key(self, widget, seq):
        """A key such as '<Control-z>' on a widget, through its bindings (helpers.send_key:
        event_generate needs the system's keyboard focus, which another program can take)."""
        parts = [p for p in seq.strip("<>").split("-") if p not in ("KeyPress", "Key")]
        widget.focus_set()
        send_key(widget, parts[-1], modifiers=parts[:-1])
        self.root.update()

    def pump(self, until=None, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.root.update()
            if until() if until else not self.grid.loading():
                self.root.update()
                return True
            time.sleep(0.005)
        return False

    def show(self, src=None):
        self.grid.set_source(src or self.source())
        self.pump()
        self.grid.redraw_now()
        return self.grid

    def popover(self, c, wait_values=True):
        pop = self.grid.open_filter_popover(c)
        self.assertIsNotNone(pop)
        if wait_values:
            self.assertTrue(self.pump(lambda: pop._distinct is not None))
        return pop

    def settle(self, pop):
        self.assertTrue(self.pump(lambda: not pop._cancels and pop._count_after is None
                                  and not pop.count_text.startswith("counting")))

    def ids(self):
        """ids of the rows the grid's source serves for the filters in force."""
        self.pump()
        return sorted(values[1] for values in self.grid.source.iter_rows())

    def test_value_search_reaches_the_whole_column_and_adds_to_the_filter(self):
        # a checklist cut to the 10 most frequent values still finds a rare value: the search
        # reads the whole column; with 'Add …' the matches join the values kept before
        g = self.show()
        limits.load({"limits": {"filter_distinct_values": 10}})
        c = COLS.index("name") + 1
        pop = self.popover(c)
        self.assertTrue(pop._distinct.capped)
        rare = [r[1] for r in self.rows if r[1] and r[1].startswith("Gamma")][-1]
        self.assertNotIn(rare, [e.label for e in pop.values.entries])
        pop.value_search_var.set(rare)
        self.assertTrue(self.pump(lambda: rare in [e.label for e in pop.values.shown_entries()]))
        self.assertIn("in the whole column", pop.values_note.cget("text"))
        self.assertTrue(pop.add_check.winfo_manager())
        text, msg = pop.compile()
        self.assertEqual((text, msg), ("IN (%s)" % rare, ""))       # the matches only
        pop.add_var.set(True)
        text, _msg = pop.compile()
        self.assertTrue(text.startswith("IN (") and rare in text and "alpha" in text,
                        text)                                    # the matches and the rest
        pop.value_search_var.set("")
        self.assertFalse(pop.add_check.winfo_manager())
        pop.close()
        limits.reset()

    def test_funnels_hover_and_the_active_filter_look(self):
        from tokens import COLOR as K
        g = self.show()
        funnels = g.funnel_items()
        self.assertNotIn(0, funnels)                        # the row locator has none
        self.assertIn(3, funnels)
        self.assertEqual(funnels[3], ("", K["muted_text"]))
        c = 3
        k = g._pos[c]
        right = g._xs[k + 1] - g._xoff
        self.assertEqual(g._funnel_at(g._hdr, right - 10, 8), c)
        self.assertIsNone(g._funnel_at(g._hdr, right - 60, 8))
        g._set_funnel_hover(c)
        g.redraw_now()
        self.assertEqual(g.funnel_items()[c], (K["primary_soft"], K["primary"]))
        g.set_filter_text(c, "=2")
        self.pump()
        g.redraw_now()
        self.assertEqual(g.funnel_items()[c][0], K["accent"])
        self.assertEqual(g.header_background(c), K["accent_soft"])
        self.assertNotEqual(g.header_background(2), K["accent_soft"])

        class Ev(object):
            def __init__(self, x, y):
                self.x, self.y, self.x_root, self.y_root = x, y, 0, 0
        g._on_press(Ev(right - 10, 8), g._hdr, False)       # a click on the funnel
        g._on_release(Ev(right - 10, 8), g._hdr)
        pop = g.filter_popover()
        self.assertIsNotNone(pop)
        self.assertEqual(pop.column, "status")
        self.assertEqual(g.sort_state(), (None, False))      # it did not sort
        pop.close()

    def test_popover_types_values_keyboard_and_apply(self):
        g = self.show()
        pop = self.popover(COLS.index("status") + 1)
        self.assertEqual(pop.type_label.cget("text"), "Few values · number")
        self.assertIn("4 distinct values", pop.values_note.cget("text"))
        self.assertEqual(len(pop.enum_chips), 4)
        lines = pop.values.visible_lines()
        self.assertEqual(lines[0][0], "(Blanks)")
        self.settle(pop)
        self.assertEqual(pop.count_text, "600 of 600 rows")
        # untick 1 (a chip): the count follows before applying
        one = [e for e in pop.values.entries if e.label == "1"][0]
        pop.enum_chips[one.key]._activate()
        self.assertNotIn(one.key, pop.values.ticked)
        text, msg = pop.compile()
        self.assertEqual((text, msg), ("NOT IN (1)", ""))
        n = sum(1 for r in self.rows if r[2] != 1)
        self.settle(pop)
        self.assertEqual(pop.count_text, "%d of 600 rows" % n)
        self.key(pop.values.cv, "<KeyPress-Return>")        # Enter applies
        self.pump()
        self.assertIsNone(g.filter_popover())
        self.assertEqual(g.filter_texts(), {"status": "NOT IN (1)"})
        self.assertEqual(self.ids(), sorted(r[0] for r in self.rows if r[2] != 1))
        # nothing ticked: said, Apply not possible
        pop = self.popover(3)
        pop.select_all(False)
        self.assertEqual(pop.compile(), (None, "Tick at least one value (or use Clear filter)"))
        self.assertFalse(pop.apply())
        self.assertIsNotNone(g.filter_popover())
        self.key(pop.values.cv, "<KeyPress-Escape>")        # Escape closes, nothing changes
        self.pump()
        self.assertIsNone(g.filter_popover())
        self.assertEqual(g.filter_texts(), {"status": "NOT IN (1)"})
        # the popover opened on a filter shows it
        pop = self.popover(3)
        self.assertEqual([e.label for e in pop.values.entries if e.key not in pop.values.ticked],
                         ["1"])
        pop.clear_filter()
        self.pump()
        self.assertEqual(g.filter_texts(), {})
        # a limit that cut the checklist is said (and ticking stays usable)
        limits.load({"limits": {"filter_distinct_values": 10}})
        pop = self.popover(1)
        self.assertEqual(pop.values_note.cget("text"), "Showing the 10 most frequent of 600 "
                                                       "values (limit filter_distinct_values)")
        self.assertEqual(len(pop.values.entries), 10)
        pop.values.toggle(pop.values.entries[0].key)
        self.assertEqual(pop.compile(), ("NOT IN (%d)" % pop.values.entries[0].values[0], ""))
        pop.close()
        limits.reset()
        # each type gets its builder
        for col, kind, first_op in (("name", "text", "contains"), ("score", "number", "equals"),
                                    ("ts_ms", "date", "between"), ("flag", "bool", "equals"),
                                    ("data", "blob", "not_empty"), ("js", "json", "contains")):
            pop = self.popover(COLS.index(col) + 1, wait_values=False)
            self.assertEqual((pop.ct.kind, pop.lines[0].op), (kind, first_op), col)
            if kind == "date":
                self.assertTrue(self.pump(lambda: getattr(pop, "bins", None) is not None))
                self.assertEqual(sum(pop.bins.totals), 600)
                self.assertIn("(UTC), 600 rows with a date", pop.range_label.cget("text"))
                self.assertGreater(pop.chart.bar_count(), 0)
            pop.close()

    def test_dragging_across_the_date_histogram_sets_between(self):
        g = self.show()
        pop = self.popover(COLS.index("ts_ms") + 1, wait_values=False)
        self.assertTrue(self.pump(lambda: getattr(pop, "bins", None) is not None))
        chart = pop.chart
        self.pump(lambda: chart.winfo_width() > 100)
        w = chart.winfo_width()
        a, b = int(w * 0.25), int(w * 0.6)
        chart.event_generate("<ButtonPress-1>", x=a, y=30)
        chart.event_generate("<B1-Motion>", x=(a + b) // 2, y=30)
        chart.event_generate("<ButtonRelease-1>", x=b, y=30)
        self.root.update()
        start, end = chart.selection
        self.assertLess(start, end)
        self.assertEqual(pop.lines[0].op, "between")
        text, msg = pop.compile()
        self.assertEqual(msg, "")
        st = cf.builder_state(text, pop.ct)
        self.assertEqual(st["conds"][0][0], "between")
        got_a, got_b = st["conds"][0][1]
        self.assertLessEqual(abs((got_a - start).total_seconds()), 1)
        self.assertLessEqual(abs((got_b - end).total_seconds()), 1)
        self.assertTrue(pop.apply())
        self.pump()
        ts = COLS.index("ts_ms")
        ids = set(self.ids())
        one = timedelta(seconds=1)

        def when(r):
            return datetime(1970, 1, 1) + timedelta(milliseconds=r[ts])
        dated = [r for r in self.rows if r[ts] is not None]
        inside = set(r[0] for r in dated if start + one <= when(r) <= end - one)
        near = set(r[0] for r in dated if start - one <= when(r) <= end + one)
        self.assertTrue(inside)
        self.assertLess(len(ids), len(self.rows))
        self.assertTrue(inside <= ids <= near)          # the rows of the range dragged
        # a click without dragging clears the range again
        pop = self.popover(COLS.index("ts_ms") + 1, wait_values=False)
        self.assertTrue(self.pump(lambda: getattr(pop, "bins", None) is not None))
        pop.chart.select(start, end)
        pop.chart.event_generate("<ButtonPress-1>", x=a, y=30)
        pop.chart.event_generate("<ButtonRelease-1>", x=a + 1, y=30)
        self.root.update()
        self.assertIsNone(pop.chart.selection)
        pop.close()
        self.assertIsNotNone(g)

    def test_text_condition_recent_terms_and_chips(self):
        g = self.show()
        c = COLS.index("name") + 1
        pop = self.popover(c)
        box = pop.lines[0].fields["box"]
        self.assertIn("alpha", box.cget("values"))           # the column's frequent values
        pop.set_line(0, "contains", ("otp",))
        self.settle(pop)
        n = sum(1 for r in self.rows if r[1] and "otp" in r[1])
        self.assertEqual(pop.count_text, "%d of 600 rows" % n)
        self.assertTrue(pop.apply())
        self.pump()
        from combobox import recent_choices
        self.assertEqual(recent_choices("colfilter:name")[0], "otp")
        pop = self.popover(COLS.index("score") + 1)
        pop.set_line(0, "gt", ("50",))
        pop.apply()
        self.pump()
        g.set_global_filter("a", apply=True)
        self.pump()
        chips = g.filter_chips()
        self.assertEqual([ch[1] for ch in chips], ["name contains 'otp'", "score > 50",
                                                   "rows contain 'a'"])
        bar = g.filter_bar
        widgets = bar.chip_widgets()
        self.assertEqual([w.text for w in widgets], [ch[1] for ch in chips])
        self.assertTrue(all(w.active for w in widgets))
        self.assertEqual(g.filter_bar_text(), "3 filters active: name, score, all columns")
        # a click on a chip edits it
        widgets[1]._activate()
        self.assertEqual(g.filter_popover().column, "score")
        self.assertEqual(g.filter_popover().lines[0].op, "gt")
        g.close_filter_popover()
        # x on a chip removes it
        w = bar.chip_widgets()[1]
        self.root.update()
        w.event_generate("<Button-1>", x=int(w.cget("width")) - 5, y=10)
        self.pump()
        self.assertEqual(g.filter_texts(), {"name": "otp"})
        # Delete / Backspace on a focused chip removes it
        self.key(bar.chip_widgets()[0], "<KeyPress-Delete>")
        self.pump()
        self.assertEqual(g.filter_texts(), {})
        self.assertEqual(g.global_filter(), "a")
        self.key(bar.chip_widgets()[0], "<KeyPress-BackSpace>")
        self.pump()
        self.assertEqual(g.global_filter(), "")
        self.assertFalse(bar.chips.winfo_manager())         # no filter: no chips row
        # Clear all
        g.set_filter_text(3, "=1")
        g.set_filter_text(4, ">10")
        self.pump()
        self.assertEqual(len(bar.chip_widgets()), 2)
        links = [w for w in bar.chips.items() if not hasattr(w, "active")]
        [w for w in links if w.cget("text") == "Clear all"][0].invoke()
        self.pump()
        self.assertEqual(g.filter_texts(), {})

    def test_undo_redo_and_copies(self):
        g = self.show()
        g.set_filter_text(3, "=2")
        self.pump()
        g.sort_by(4, True)
        self.pump()
        g.set_filter_text(2, "*=a")
        self.pump()
        views, i = g.filter_history()
        self.assertEqual(len(views), 4)
        self.assertEqual(i, 3)
        where = g.sql_where()
        self.assertEqual(where, "WHERE (CAST(\"name\" AS TEXT) LIKE '%a%' ESCAPE '\\' AND "
                                "+\"status\" = 2)")
        self.assertEqual(g.filter_text(), "name: *=a\nstatus: =2")
        self.key(g._cv, "<Control-z>")
        self.pump()
        self.assertEqual(g.filter_texts(), {"status": "=2"})
        self.assertEqual(g.sort_state(), ("score", True))
        self.assertTrue(g.undo_filter())
        self.pump()
        self.assertEqual(g.sort_state(), ("_rid", False))
        self.assertTrue(g.undo_filter())
        self.pump()
        self.assertEqual(g.filter_texts(), {})
        self.assertFalse(g.undo_filter())
        self.assertFalse(g.can_undo_filter())
        self.key(g._cv, "<Control-y>")
        self.pump()
        self.assertEqual(g.filter_texts(), {"status": "=2"})
        self.assertTrue(g.redo_filter() and g.redo_filter())
        self.pump()
        self.assertEqual(g.filter_texts(), {"status": "=2", "name": "*=a"})
        self.assertEqual(g.sort_state(), ("score", True))
        self.assertFalse(g.redo_filter())
        # a new change drops the views ahead
        g.undo_filter()
        g.set_filter_text(5, ">1")
        self.pump()
        self.assertFalse(g.can_redo_filter())
        self.assertTrue(g.filter_bar.fwd.instate(["disabled"]))
        self.assertTrue(g.filter_bar.back.instate(["!disabled"]))

    def test_saved_filters_hook(self):
        store = {}
        g = self.show()
        g.saved_filters = (lambda key: dict(store.get(key, {})),
                           lambda key, name, f: store.setdefault(key, {}).__setitem__(name, f)
                           if f is not None else store.get(key, {}).pop(name, None))
        g.set_filter_text(3, "=2")
        g.set_global_filter("alpha", apply=True)
        self.pump()
        self.assertTrue(g.save_current_filter("mine"))
        self.assertEqual(store, {"t": {"mine": {"columns": {"status": "=2"}, "search": "alpha",
                                                "sort": None}}})
        g.clear_filters()
        self.pump()
        menu = g.filter_bar.saved_menu()
        self.assertEqual(menu.entrycget(0, "label"), "mine")
        menu.invoke(0)
        self.pump()
        self.assertEqual((g.filter_texts(), g.global_filter()), ({"status": "=2"}, "alpha"))
        skipped = g.apply_saved_filter({"columns": {"nope": "=1", "flag": "=1"}, "search": "",
                                        "sort": ["score", False]})
        self.pump()
        self.assertEqual(skipped, ["nope"])
        self.assertIn("no column nope", g.notice)
        self.assertEqual(g.filter_texts(), {"flag": "=1"})
        self.assertEqual(g.sort_state(), ("score", False))

    def test_quick_filters_from_the_cell_menu(self):
        g = self.show()
        g.set_column_formatter(COLS.index("ts_ms") + 1, tl.formatter("unix_ms"), "UTC Unix ms")

        def labels(menu):
            return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                    if menu.type(i) in ("command", "cascade")]

        def invoke(menu, label):
            for i in range(menu.index("end") + 1):
                if menu.type(i) == "command" and menu.entrycget(i, "label") == label:
                    menu.invoke(i)
                    return True
            return False
        status = COLS.index("status") + 1
        v = g.row_data(0)[0][status]
        m = g.build_context_menu(0, status)
        for want in ("Filter to this value", "Exclude this value", "Show rows with the same",
                     "Filter status…"):
            self.assertIn(want, labels(m))
        self.assertNotIn("Filter to this day", labels(m))
        self.assertTrue(invoke(m, "Exclude this value"))
        self.pump()
        self.assertEqual(self.ids(), sorted(r[0] for r in self.rows if r[2] != v))
        g.redraw_now()
        shown = [(i, g.row_data(i)[0][status]) for i in range(*g.visible_row_range())]
        row, other = [(i, x) for i, x in shown if x not in (v, None)][0]
        m = g.build_context_menu(row, status)
        self.assertTrue(invoke(m, "Exclude this value"))     # merged into one NOT IN
        self.pump()
        self.assertEqual(parse_expr(g.filter_texts()["status"]).kind, "notin")
        self.assertEqual(self.ids(), sorted(r[0] for r in self.rows if r[2] not in (v, other)))
        g.clear_filters()
        self.pump()
        ts = COLS.index("ts_ms") + 1
        m = g.build_context_menu(3, ts)
        self.assertIn("Filter to this day", labels(m))
        raw = g.row_data(3)[0][ts]
        self.assertTrue(invoke(m, "Filter to this hour"))
        self.pump()
        hour = tl.to_utc(raw, "unix_ms").replace(minute=0, second=0, microsecond=0)
        self.assertEqual(self.ids(), sorted(r[0] for r in self.rows
                                            if hour <= tl.to_utc(r[4], "unix_ms")
                                            < hour + timedelta(hours=1)))
        self.assertIn("ts_ms: ", g.filter_chips()[0][1])
        g.clear_filters()
        self.pump()
        # several rows selected: their values (IN)
        name = COLS.index("name") + 1
        g.set_current_cell(0, name)
        g.set_current_cell(5, name, extend=True)
        vals = set(g.row_data(i)[0][name] for i in range(6))
        m = g.build_context_menu(2, name)
        self.assertTrue(invoke(m, "Filter to these values (6 rows)"))
        self.pump()
        self.assertEqual(self.ids(), sorted(r[0] for r in self.rows if r[1] in vals))
        # 'Show rows with the same <column>'
        g.clear_filters()
        self.pump()
        m = g.build_context_menu(0, name)
        idx = [i for i in range(m.index("end") + 1) if m.type(i) == "cascade"
               and m.entrycget(i, "label") == "Show rows with the same"][0]
        sub = m.nametowidget(m.entrycget(idx, "menu"))
        flag_label = [sub.entrycget(i, "label") for i in range(sub.index("end") + 1)
                      if sub.entrycget(i, "label").startswith("flag")][0]
        self.assertTrue(invoke(sub, flag_label))
        self.pump()
        self.assertEqual(g.filter_texts(), {"flag": "=%d" % self.rows[0][8]})

    def test_search_words_are_highlighted_in_the_cells(self):
        g = self.show(self.list_source())
        g.set_global_filter("ALPHA", apply=True)
        self.pump()
        g.redraw_now()
        hl = g.highlight_items()
        self.assertTrue(hl)
        name = COLS.index("name") + 1
        self.assertTrue(all(c == name for (_r, c, _n) in hl))
        x0, x1 = hl[(0, name, 0)]
        font = g._fonts[0]
        self.assertEqual(x1 - x0, font.measure("alpha"))
        g.set_global_filter("", apply=True)
        self.pump()
        g.redraw_now()
        self.assertEqual(g.highlight_items(), {})

    def test_empty_state_offers_to_remove_the_last_condition(self):
        g = self.show()
        g.set_filter_text(3, "=2")
        self.pump()
        g.set_filter_text(2, "=nothing like this")
        self.pump(lambda: g.row_count_exact())
        g.redraw_now()
        self.assertEqual(g.empty_state(),
                         "No rows match — remove “name = 'nothing like this'”?")
        g._empty.remove_btn.invoke()
        self.pump()
        g.redraw_now()
        self.assertEqual(g.filter_texts(), {"status": "=2"})
        self.assertEqual(g.empty_state(), "")
        g.set_filter_text(3, "=99")
        self.pump(lambda: g.row_count_exact())
        g.redraw_now()
        self.assertIn("status = 99", g.empty_state())
        g._empty.clear_btn.invoke()
        self.pump()
        g.redraw_now()
        self.assertEqual((g.filter_texts(), g.empty_state()), ({}, ""))

    def test_inline_filter_row_is_optional_and_the_old_api_works(self):
        g = self.show(self.list_source())
        # the filter row is shown by default now (it used to be opt-in)
        self.assertTrue(g.inline_filters())
        g.set_inline_filters(False)
        self.assertFalse(g.inline_filters())
        self.assertEqual(int(g._hdr.cget("height")), g._hh)
        g.set_inline_filters(True)
        self.assertTrue(g.inline_filters())
        g.set_filter_text(3, ">1")
        g.set_filter_text(2, "/[/")
        self.pump()
        self.assertEqual(g.filter_texts(), {"status": ">1", "name": "/[/"})
        self.assertIn("name", g.filter_errors())
        self.assertEqual(str(g.filter_entry(2).cget("style")), "GridFilterBad.TEntry")
        self.assertTrue(g.filter_tip(2).startswith("Cannot use this filter"))
        chips = g.filter_chips()
        self.assertEqual([c[3] for c in chips], [False, True])       # the bad one is marked
        self.assertIn("invalid: ", [w.text for w in g.filter_bar.chip_widgets()][1])
        self.assertIn("1 cannot be used", g.filter_bar_text())
        menu = g.build_header_menu(3)
        i = [k for k in range(menu.index("end") + 1) if menu.type(k) == "checkbutton"][0]
        self.assertEqual(menu.entrycget(i, "label"), "Filter row under the headers")
        menu.invoke(i)
        self.assertFalse(g.inline_filters())  # the row was on: the menu turns it off
        menu.invoke(i)
        self.assertTrue(g.inline_filters())  # and back on again
        g.redraw_now()
        self.root.update_idletasks()
        self.assertEqual(int(g._hdr.cget("height")), g._hh + g._fh)
        for c in g.visible_columns():
            e = g.filter_entry(c)
            self.assertEqual((e.winfo_x(), e.winfo_width()), (g.column_x(c), g.column_width(c)))
        g.filter_bar.row_btn.invoke()
        self.assertFalse(g.inline_filters())
        g.clear_filters()
        self.pump()
        self.assertEqual(g.filter_texts(), {})

    def test_fast_changes_cancel_the_workers(self):
        path = os.path.join(self.tmp, "big.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE b(x INTEGER, s TEXT)")
        conn.executemany("INSERT INTO b VALUES (?, ?)",
                         ((i % 97, "v%d" % (i % 5000)) for i in range(150000)))
        conn.commit()
        conn.close()
        from database import DB
        from browse_sources import TableSource
        db = DB()
        db.open(path)
        self.addCleanup(db.close)
        g = self.grid
        g.set_source(TableSource(db, "b", 150000))
        self.pump()
        pop = g.open_filter_popover(2)
        for i in range(40):
            pop.set_line(0, "contains", ("v%d" % i,))
            self.root.update()
        self.settle(pop)
        n = sum(1 for k in range(5000) if "v39" in "v%d" % k) * 30
        self.assertEqual(pop.count_text, "%s of 150K rows" % cf.fmt_int(n))
        for i in range(10):                     # closing in the middle of reads
            pop = g.open_filter_popover(1 + i % 2)
            pop.set_line(0, "contains" if i % 2 else "gt", ("1",))
            self.root.update()
        g.close_filter_popover()
        limits.load({"limits": {"filter_distinct_scan_rows": 1000}})
        pop = self.popover(2)
        self.assertIn("counted in the first 1,000 rows only (limit filter_distinct_scan_rows)",
                      pop.values_note.cget("text"))
        g.close_filter_popover()
        deadline = time.time() + 20
        while g.worker_threads() and time.time() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(g.worker_threads(), [])
        self.assertEqual(self.errors, [])
        g.set_source(None)


if __name__ == "__main__":
    unittest.main()
