"""Position indexes: windows read through checkpoints or position maps equal OFFSET windows."""

import os
import sqlite3
import threading
import unittest
from unittest import mock

from tests.helpers import TempDirTest
from engine import limits, positions
from engine.backends import Filter
from engine.fileformat.record import InvalidText
from engine.session import Session


def make_db(path, rows=600):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE t(id INTEGER PRIMARY KEY, n INTEGER, s TEXT, r REAL, m, w TEXT);
        CREATE INDEX t_n ON t(n);
        CREATE TABLE plain(a TEXT, b INTEGER);
        CREATE TABLE wr(a TEXT, b INTEGER, c, PRIMARY KEY(a, b DESC)) WITHOUT ROWID;
        CREATE TABLE wr2(a TEXT COLLATE NOCASE, b INTEGER, v, PRIMARY KEY(a, b)) WITHOUT ROWID;
        CREATE VIEW vw AS SELECT id, n FROM t WHERE id % 3 = 0;
        CREATE TABLE big(x INTEGER, y TEXT);
    """)
    c.executemany("INSERT INTO big VALUES (?,?)",
                  [(i % 97 if i % 9 else None, "y%d" % (i % 31)) for i in range(4000)])
    data = []
    for i in range(rows):
        n = None if i % 7 == 0 else (i * 13) % 11          # NULLs, many duplicates
        s = None if i % 5 == 0 else ("Name %03d" % ((i * 37) % 50))
        m = [None, i, "text %d" % (i % 9), b"\x00\x01" * (i % 3), i / 4.0][i % 5]
        data.append((i * 3 + 1, n, s, i / 7.0 if i % 4 else None, m, "w%d" % (i % 4)))
    c.executemany("INSERT INTO t VALUES (?,?,?,?,?,?)", data)
    c.execute("DELETE FROM t WHERE id % 17 = 0")               # gaps in the rowids
    c.executemany("INSERT INTO plain VALUES (?,?)",
                  [(None if i % 6 == 0 else "p%d" % (i % 13), i % 5) for i in range(300)])
    c.executemany("INSERT INTO wr VALUES (?,?,?)",
                  [("k%02d" % (i % 23), i, None if i % 3 == 0 else i % 4) for i in range(400)])
    c.executemany("INSERT INTO wr2 VALUES (?,?,?)",
                  [(("K%d" if i % 2 else "k%d") % (i % 29), i, i % 6) for i in range(300)])
    c.commit()
    c.close()


class PositionsTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        limits.load({"limits": {"checkpoint_every": 16}})
        self.path = os.path.join(self.tmp, "p.db")
        make_db(self.path)
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            s.close()
        limits.reset()
        TempDirTest.tearDown(self)

    def open(self):
        s = Session.open(self.path, hash_evidence=False)
        self.sessions.append(s)
        return s

    @staticmethod
    def ids(page):
        return [(r.locator.kind, r.locator.value, tuple(r.values)) for r in page.rows]

    def check_view(self, name, order_by=None, desc=False, flt=None, limit=7, step=1):
        plain, fast = self.open(), self.open()
        n = fast.build_positions(name, order_by, desc, flt)
        info = fast.positions_info(name, order_by, desc, flt)
        self.assertTrue(info["complete"])
        total = plain.count(name, flt)
        self.assertEqual(n, total)
        for p in list(range(0, total, step)) + [total - 1, total, total + 2]:
            want = self.ids(plain._browse_sql(plain.info(name), p, limit, order_by, desc, flt))
            got = self.ids(fast.browse(name, p, limit, order_by, desc, flt))
            self.assertEqual(got, want, (name, order_by, desc, p))
        after = fast.positions_info(name, order_by, desc, flt)
        self.assertEqual(after["missed"], 0, "every window must come from the index")
        self.assertGreater(after["served"], 0)
        return fast, info

    def test_rowid_natural(self):
        for desc in (False, True):
            fast, info = self.check_view("t", desc=desc)
            self.assertTrue(info["usable"])
            self.assertEqual(info["mapped"], 0)          # rowid order seeks: checkpoints only
            self.assertGreater(info["checkpoints"], 10)

    def test_sorted_indexed_with_nulls_and_ties(self):
        for desc in (False, True):
            self.check_view("t", "n", desc)

    def test_sorted_unindexed_uses_position_map(self):
        for col in ("s", "r", "m"):
            for desc in (False, True):
                _fast, info = self.check_view("t", col, desc)
                self.assertGreater(info["mapped"], 0)

    def test_filtered(self):
        flt = Filter(col_exprs={"n": ">3"}, words=["name"])
        for order_by in (None, "n", "s"):
            for desc in (False, True):
                _fast, info = self.check_view("t", order_by, desc, flt)
                self.assertEqual(info["mapped"], info["rows"])

    def test_filter_count_comes_from_the_index(self):
        s = self.open()
        flt = Filter(col_exprs={"w": "w1"})
        n = s.build_positions("t", None, False, flt)
        with mock.patch.object(s, "conn", side_effect=AssertionError("no scan")):
            self.assertEqual(s.count("t", flt), n)

    def test_map_limit_falls_back_to_checkpoints(self):
        limits.load({"limits": {"checkpoint_every": 16, "position_map_rows": 50}})
        fast, info = self.check_view("t", "s", False)
        self.assertEqual(info["mapped"], 0)
        page = fast.browse("t", 300, 5, "s", False)
        self.assertIn("position_map_rows", page.note)
        self.check_view("t", None, False, Filter(col_exprs={"n": ">2"}))

    def test_without_rowid(self):
        for name in ("wr", "wr2"):
            for order_by in (None, "c" if name == "wr" else "v", "a"):
                for desc in (False, True):
                    self.check_view(name, order_by, desc)

    def test_without_rowid_filtered(self):
        self.check_view("wr", "c", True, Filter(col_exprs={"a": "k1"}))

    def test_rowid_table_without_index(self):
        for order_by in (None, "a", "b"):
            for desc in (False, True):
                self.check_view("plain", order_by, desc)

    def test_thinning_keeps_windows_right(self):
        limits.load({"limits": {"checkpoint_every": 16, "checkpoints_max": 100}})
        _fast, info = self.check_view("big", "x", False, step=3)
        self.assertLessEqual(info["checkpoints"], 100)
        self.assertGreater(info["every"], 16)

    def test_views_and_native_tables(self):
        s = self.open()
        self.assertIsNone(s.build_positions("vw"))
        s._sql_failed["t"] = "forced"                   # read natively from now on
        self.assertEqual(s.source("t"), "native")
        self.assertIsNone(s.build_positions("t"))
        n = s.build_positions("t", "n", True)
        self.assertEqual(n, s.count("t"))

    def test_native_sort_ties_match_sql(self):
        sql, nat = self.open(), self.open()
        nat._sql_failed["t"] = "forced"
        for desc in (False, True):
            want = [r.locator.value for r in sql.browse("t", 0, 1000, "w", desc).rows]
            got = [r.locator.value for r in nat.browse("t", 0, 1000, "w", desc).rows]
            self.assertEqual(got, want, desc)

    def test_native_sort_cap_is_a_setting_and_says_so(self):
        limits.load({"limits": {"native_sort_rows": 1000}})
        self.assertEqual(limits.get("native_sort_rows"), 1000)
        c = sqlite3.connect(os.path.join(self.tmp, "big.db"))
        c.execute("CREATE TABLE b(x)")
        c.executemany("INSERT INTO b VALUES (?)", [(i % 10,) for i in range(1500)])
        c.commit()
        c.close()
        s = Session.open(os.path.join(self.tmp, "big.db"), hash_evidence=False)
        self.sessions.append(s)
        s._sql_failed["b"] = "forced"
        page = s.browse("b", 0, 5, "x")
        self.assertTrue(page.capped)
        self.assertIn("native_sort_rows", page.note)

    def test_partial_index_serves_its_prefix(self):
        s = self.open()
        stop = {"after": 0}

        def cancel():
            stop["after"] += 1
            return False
        # a build that stops half way leaves no index behind
        calls = []

        def cancel_later():
            calls.append(1)
            return len(calls) > 1
        old = positions.FETCH
        positions.FETCH = 50
        try:
            self.assertIsNone(s.build_positions("t", "s", False, None, cancel_later))
        finally:
            positions.FETCH = old
        self.assertIsNone(s.positions_info("t", "s"))
        self.assertIsNotNone(s.build_positions("t", "s", False, None, cancel))

    def test_interrupt_stops_a_build(self):
        s = self.open()
        started = threading.Event()

        def cancel():
            started.set()
            return False
        errors = []
        old = positions.FETCH
        positions.FETCH = 1

        def run():
            try:
                s.build_positions("t", "s", False, None, lambda: cancel() or s._closed)
            except sqlite3.Error as e:
                errors.append(e)
            finally:
                s.release_thread_connection()
        th = threading.Thread(target=run)
        try:
            th.start()
            started.wait(5)
            s.interrupt(th)
            th.join(10)
        finally:
            positions.FETCH = old
        self.assertFalse(th.is_alive())
        info = s.positions_info("t", "s")
        self.assertTrue(info is None or info["complete"])

    def test_close_while_building(self):
        s = Session.open(self.path, hash_evidence=False)
        started = threading.Event()
        old = positions.FETCH
        positions.FETCH = 1
        done = []

        def run():
            try:
                s.build_positions("t", "s", False, None, lambda: started.set() and False)
            except Exception as e:      # noqa: BLE001 - closed or interrupted under it
                done.append(e)
            finally:
                s.release_thread_connection()
        th = threading.Thread(target=run)
        try:
            th.start()
            started.wait(5)
            s.close()
            th.join(10)
        finally:
            positions.FETCH = old
        self.assertFalse(th.is_alive())
        self.assertIsNone(s.positions_info("t", "s"))

    def test_lru_keeps_a_bounded_number(self):
        limits.load({"limits": {"checkpoint_every": 16, "position_indexes_kept": 2}})
        s = self.open()
        for col in ("n", "s", "r"):
            s.build_positions("t", col)
        self.assertIsNone(s.positions_info("t", "n"))
        self.assertIsNotNone(s.positions_info("t", "r"))


class AffinityTest(unittest.TestCase):
    def test_affinity_safe(self):
        self.assertTrue(positions.affinity_safe("INTEGER", 5))
        self.assertTrue(positions.affinity_safe("INTEGER", "abc"))
        self.assertFalse(positions.affinity_safe("INTEGER", "12"))
        self.assertFalse(positions.affinity_safe("NUMERIC", " 1e3"))
        self.assertFalse(positions.affinity_safe("TEXT", 5))
        self.assertTrue(positions.affinity_safe("TEXT", "5"))
        self.assertTrue(positions.affinity_safe("BLOB", 5))
        self.assertFalse(positions.affinity_safe("TEXT", InvalidText(b"\xff")))
        self.assertFalse(positions.affinity_safe("BLOB", None))

    def test_unsafe_key_falls_back_to_offset(self):
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="sga_test_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "a.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE a(id INTEGER PRIMARY KEY, x TEXT)")
        c.execute("CREATE INDEX a_x ON a(x)")
        # numbers stored in a TEXT column (bypassing affinity through a typeless copy)
        c.execute("CREATE TABLE raw(id INTEGER PRIMARY KEY, x)")
        c.executemany("INSERT INTO raw VALUES (?,?)",
                      [(i, i if i % 2 else "t%d" % i) for i in range(1, 200)])
        c.commit()
        c.execute("PRAGMA writable_schema=ON")
        c.execute("UPDATE sqlite_master SET sql='CREATE TABLE raw(id INTEGER PRIMARY KEY, x TEXT)'"
                  " WHERE name='raw'")
        c.commit()
        c.close()
        limits.load({"limits": {"checkpoint_every": 16}})
        self.addCleanup(limits.reset)
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        plain = Session.open(path, hash_evidence=False)
        self.addCleanup(plain.close)
        s.build_positions("raw", "x")
        for p in range(0, 200, 13):
            self.assertEqual([r.locator.value for r in s.browse("raw", p, 9, "x").rows],
                             [r.locator.value for r in plain.browse("raw", p, 9, "x").rows])
        self.assertFalse(s.positions_info("raw", "x")["usable"])


class LimitsTest(unittest.TestCase):
    """The Browse limits live in engine.limits beside the others (one table, one API)."""

    def tearDown(self):
        limits.reset()

    def test_browse_limits_are_named_limits(self):
        for name in ("grid_window_rows", "checkpoint_every", "checkpoints_max",
                     "position_map_rows", "position_indexes_kept", "native_sort_rows",
                     "cell_draw_chars", "value_view_chars"):
            self.assertIn(name, limits.DEFAULTS)
            self.assertIn(name, limits.RANGES)
            self.assertIn(name, limits.LIMITS)
            lo, hi = limits.RANGES[name]
            self.assertTrue(lo <= limits.DEFAULTS[name] <= hi, name)
        good, problems = limits.validate({"grid_window_rows": 300, "checkpoint_every": "x",
                                          "checkpoints_max": 5, "grid_cache_windows": True})
        self.assertEqual(good["grid_window_rows"], 300)
        self.assertEqual(good["checkpoint_every"], limits.DEFAULTS["checkpoint_every"])
        self.assertEqual(good["checkpoints_max"], limits.DEFAULTS["checkpoints_max"])
        self.assertEqual(len(problems), 3)
        problems = limits.load({"limits": {"grid_window_rows": -1}})
        self.assertEqual(len(problems), 1)
        self.assertEqual(limits.get("grid_window_rows"), limits.DEFAULTS["grid_window_rows"])
        self.assertIn("grid_window_rows", limits.hint("grid_window_rows"))


if __name__ == "__main__":
    unittest.main()
