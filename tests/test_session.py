import gc
import os
import re
import sqlite3
import threading
import time
import unittest
import weakref
from unittest import mock

from tests.helpers import TempDirTest, copy_with_sidecars, dir_snapshot
from tests.fixtures import make_fixtures as fx
from engine.backends import Filter, SqlBackend, ram_overlay_supported
from engine.evidence import EvidenceSet
from engine.fileformat.btree import BTreeReader
from engine.fileformat.pager import Pager
from engine.fileformat.record import InvalidText
from engine.fileformat.wal import WalFile
from engine.issues import IssueLog
from engine.schema import Locator, SchemaModel
from engine.session import IMMUTABLE, MAIN_ONLY, NATIVE, RAM_OVERLAY, Session, SessionError


class Py310Connection(sqlite3.Connection):
    """create_collation as CPython <= 3.10 implements it (Modules/_sqlite/connection.c): the
    name is upper-cased and any character outside [0-9A-Z_] raises ProgrammingError."""

    def create_collation(self, name, fn):
        upper = name.upper()
        if not re.match(r"[0-9A-Z_]*\Z", upper):
            raise sqlite3.ProgrammingError("invalid character in collation name")
        return sqlite3.Connection.create_collation(self, upper, fn)


def py310_connect(real_connect):
    def connect(*args, **kw):
        base = kw.get("factory", sqlite3.Connection)     # the engine passes EngineConnection
        if not issubclass(base, Py310Connection):
            base = type("Py310" + base.__name__, (Py310Connection, base), {})
        kw["factory"] = base
        return real_connect(*args, **kw)
    return connect


class SessionTestBase(TempDirTest):
    def open(self, path, **kw):
        kw.setdefault("hash_evidence", False)
        s = Session.open(path, **kw)
        self.addCleanup(s.close)
        return s


class SessionTest(SessionTestBase):
    def test_immutable_mode_and_banner(self):
        s = self.open(fx.without_rowid(self.tmp))
        self.assertEqual(s.mode, IMMUTABLE)
        self.assertIn("immutable", s.banners()[0].text)

    def test_without_rowid_browse_row_count_search(self):
        s = self.open(fx.without_rowid(self.tmp))
        page = s.browse("pk_last", 0, 5)
        self.assertEqual(len(page.rows), 5)
        first = page.rows[0]
        self.assertEqual(first.locator.kind, "pk")
        self.assertEqual(s.row("pk_last", first.locator).values, first.values)
        self.assertEqual(s.count("pk_last"), 2000)
        hits = list(s.search("pk_last", "a0007", "ci", limit=5))
        self.assertTrue(hits and hits[0]["locator"].kind == "pk")
        blob = s.browse("blob_pk", 0, 1).rows[0]
        self.assertIsInstance(blob.locator.value[0], bytes)
        self.assertEqual(s.row("blob_pk", blob.locator).values, blob.values)

    def _multi_match_db(self):
        path = os.path.join(self.tmp, "multi.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE m(id INTEGER PRIMARY KEY, a TEXT, b TEXT, n INTEGER)")
        c.executemany("INSERT INTO m(a, b, n) VALUES (?, ?, ?)",
                      [("hello %d" % i, "say hello" if i % 2 else "bye", i) for i in range(50)])
        c.execute("CREATE TABLE other(x TEXT)")
        c.executemany("INSERT INTO other VALUES (?)", [("hello other %d" % i,) for i in range(30)])
        c.commit()
        c.close()
        return path

    def test_search_limit_counts_rows_and_hits_carry_the_row(self):
        s = self.open(self._multi_match_db())
        hits = list(s.search("m", "hello", "ci", limit=3))
        self.assertEqual(len(set(h["locator"] for h in hits)), 3)
        self.assertEqual([h["column"] for h in hits], ["a", "a", "b", "a"])   # row 2 matches twice
        for h in hits:
            self.assertEqual(h["row"], s.row("m", h["locator"]).values)

    def test_search_skips_numbers_in_numeric_columns_only_when_they_cannot_match(self):
        path = os.path.join(self.tmp, "nums.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE n(i INTEGER, r REAL, x NUMERIC, t TEXT)")
        c.executemany("INSERT INTO n VALUES (?, ?, ?, ?)",
                      [(k, k + 0.5, k * 10, "t%d" % k) for k in range(200)] +
                      [("hello in int", "Hello in real", b"hello blob", "x"),   # text kept as text
                       (12, float("inf"), None, "inf")])
        c.commit()
        c.close()
        s = self.open(path)

        def found(term, mode="ci"):
            return sorted((h["column"], str(h["value"])) for h in s.search("n", term, mode, limit=999))
        self.assertEqual(found("hello"), [("i", "hello in int"), ("r", "Hello in real")])
        self.assertEqual(found("hello", "cs"), [("i", "hello in int")])
        # a BLOB hit shows the bytes around the match, its encoding, offset and matched bytes
        self.assertIn(("x", "hello blob  [utf-8 @0: 68 65 6c 6c 6f]"), found("hello", "blob"))
        self.assertIn(("i", "12"), found("12"))                  # numbers still match numeric terms
        self.assertIn(("x", "1990"), found("99"))
        self.assertIn(("r", "inf"), found("inf"))                 # infinity: SQLite text "Inf"
        self.assertIn(("r", "100.5"), found("0.5"))
        orig = Session._sql_rows
        with mock.patch.object(Session, "_sql_rows", autospec=True, side_effect=orig) as spy:
            list(s.search("n", "hello", "ci"))
            guarded = spy.call_args[0][3]
            list(s.search("n", "12", "ci"))
            plain = spy.call_args[0][3]
        self.assertEqual(guarded.count(">= '' COLLATE BINARY"), 3)   # i, r, x; never t
        self.assertNotIn(">= ''", plain)

    def test_search_scans_no_further_than_its_row_limit(self):
        s = self.open(self._multi_match_db())
        orig = Session._sql_rows
        with mock.patch.object(Session, "_sql_rows", autospec=True, side_effect=orig) as spy:
            list(s.search("m", "hello", "ci", limit=7))
        self.assertEqual(spy.call_args[0][5], 7)       # SQLite is asked for 7 rows at a time

    def test_search_tables_equals_one_by_one_search_and_releases_its_connections(self):
        for path in (self._multi_match_db(), fx.wal_states(self.tmp), fx.without_rowid(self.tmp)):
            s = self.open(path)
            names = s.tables()
            owned = len(s._owned)
            got = {}
            for name, hits, err in s.search_tables(names, "e", "ci", limit=40, workers=3):
                self.assertIsNone(err, name)
                got[name] = [(h["column"], h["locator"], h["value"]) for h in hits]
            want = dict((n, [(h["column"], h["locator"], h["value"])
                             for h in s.search(n, "e", "ci", limit=40)]) for n in names)
            self.assertEqual(got, want, path)
            self.assertEqual(len(s._owned), owned, path)   # every worker closed its connection
            self.assertFalse([t for t in threading.enumerate() if t.name.startswith("search-")])

    def test_search_tables_cancel_yields_nothing_and_ends_its_workers(self):
        s = self.open(self._multi_match_db())
        self.assertEqual(list(s.search_tables(s.tables(), "hello", "ci", cancel=lambda: True,
                                              workers=2)), [])
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith("search-")])

    def test_search_tables_reports_a_failing_table_and_goes_on(self):
        s = self.open(self._multi_match_db())
        orig = Session.search

        def search(self_, name, *a, **kw):
            if name == "m":
                raise sqlite3.DatabaseError("boom")
            return orig(self_, name, *a, **kw)
        with mock.patch.object(Session, "search", search):
            out = dict((n, (len(h), e)) for n, h, e in s.search_tables(["m", "other"], "hello", "ci"))
        self.assertIsInstance(out["m"][1], sqlite3.DatabaseError)
        self.assertEqual(out["other"], (30, None))

    def test_banners_have_compact_chip_labels(self):
        seen = 0
        for make in (fx.without_rowid, fx.wal_states, fx.quirks, fx.unreadable_by_sqlite):
            for b in self.open(make(self.tmp)).banners():
                seen += 1
                self.assertTrue(b.short and len(b.short) <= 32, b)
                self.assertNotEqual(b.short, b.text, b)
        self.assertGreaterEqual(seen, 4)

    def test_absent_row_is_not_an_issue_and_does_not_scan(self):
        s = self.open(fx.without_rowid(self.tmp))
        tables = [t for t in s.tables() if s.source(t) == "sql" and s.info(t).natively_readable]
        self.assertIn("pk_last", tables)
        before = len(s.issues)
        with mock.patch("engine.backends.NativeTable.iter_all",
                        side_effect=AssertionError("full scan for an absent row")):
            for t in tables:
                info = s.info(t)
                loc = (Locator("pk", tuple("no-such-key" for _ in info.pk_columns))
                       if info.without_rowid else Locator("rowid", 987654321))
                self.assertIsNone(s.row(t, loc), t)
        self.assertEqual(len(s.issues), before, [i for i in s.issues][before:])

    def test_views_and_ordinal_locators(self):
        s = self.open(fx.quirks(self.tmp))
        page = s.browse("v_shadow", 0, 10)
        self.assertEqual(page.rows[0].values, ["shadowed"])
        self.assertEqual(page.rows[0].locator, Locator("ordinal", 0))

    def test_view_row_detail_returns_the_clicked_row(self):
        s = self.open(fx.views(self.tmp))
        pages = [s.browse("vw", 0, 5, order_by="name"),
                 s.browse("vw", 3, 5, order_by="id", desc=True),
                 s.browse("vw", 2, 5, flt=Filter(any_term="n05"))]
        self.assertEqual(pages[0].rows[0].values, [100, "n001"])
        for page in pages:
            self.assertEqual(len(page.rows), 5)
            for r in page.rows:
                self.assertEqual(r.locator.kind, "ordinal")
                self.assertEqual(s.row("vw", r.locator).values, r.values)
        hits = list(s.search("vw", "n050", "ci"))
        self.assertEqual(len(hits), 1)
        self.assertEqual(s.row("vw", hits[0]["locator"]).values, [51, "n050"])
        every = list(s.iter_rows("vw"))
        self.assertEqual(s.row("vw", every[42].locator).values, every[42].values)

    def test_shadowed_rowid_table_is_served_natively(self):
        s = self.open(fx.quirks(self.tmp))
        self.assertEqual(s.source("shadow"), "native")
        row = s.browse("shadow", 0, 1).rows[0]
        self.assertEqual(row.values, ["r", "u", "o", "shadowed"])
        self.assertEqual(row.locator, Locator("rowid", 1))
        self.assertEqual(s.row("shadow", row.locator).values, row.values)

    def test_damaged_record_is_all_null_not_defaults(self):
        s = self.open(fx.damaged_records(self.tmp))
        got = [(v, f) for _loc, v, f in s._native_table("ev").iter_all()]
        # DEFAULT 'deleted' and DEFAULT CURRENT_TIMESTAMP must not be invented for a damaged row
        self.assertEqual(got, [([1, None, None], {"damaged_record"})])

    def test_invalid_text_does_not_hide_the_table(self):
        s = self.open(fx.quirks(self.tmp))
        rows = s.browse("bad_text", 0, 10).rows
        self.assertEqual(len(rows), 2)
        self.assertIsInstance(rows[1].values[1], InvalidText)

    def test_collation_table_sorts_with_fallback(self):
        s = self.open(fx.quirks(self.tmp))
        names = [r.values[0] for r in s.browse("collated", 0, 10, order_by="name").rows]
        self.assertEqual(names, ["A", "b", "c"])
        self.assertTrue(any("MYCOLL" in b.text for b in s.banners()))

    def test_natural_order_desc_matches_native(self):
        # '_rid DESC' (newest first) must reverse the natural order on SQL-served tables too.
        s = self.open(fx.without_rowid(self.tmp))
        for name in ("pk_last", "blob_pk", "desc_pk"):
            t = s.info(name)
            self.assertEqual(s.source(name), "sql")
            for offset in (0, 7):
                sql_rows = s.browse(name, offset, 5, desc=True).rows
                native_rows = s._browse_native(t, offset, 5, None, True, None).rows
                self.assertEqual([r.locator for r in sql_rows], [r.locator for r in native_rows],
                                 name)
        s2 = self.open(fx.freelist(self.tmp))
        self.assertEqual([r.locator.value for r in s2.browse("notes", 0, 3, desc=True).rows],
                         [20, 19, 18])
        flt = Filter(any_term="number 1")               # ids 2 and 11..20
        want = [r.locator.value for r in
                s2._browse_native(s2.info("notes"), 0, 3, None, True, flt).rows]
        self.assertEqual(want, [20, 19, 18])
        self.assertEqual([r.locator.value for r in s2.browse("notes", 0, 3, desc=True, flt=flt).rows],
                         want)

    def test_filter(self):
        s = self.open(fx.without_rowid(self.tmp))
        rows = s.browse("pk_last", 0, 50, flt=Filter(col_terms={"a": "a000"})).rows
        self.assertEqual(len(rows), 10)

    @unittest.skipUnless(ram_overlay_supported(), "needs Python 3.11+ / SQLite 3.36+")
    def test_wal_data_visible_in_ram_overlay(self):
        s = self.open(fx.wal_only_data(self.tmp))
        self.assertEqual(s.mode, RAM_OVERLAY)
        self.assertEqual(s.count("activity"), 40)

    def test_wal_data_visible_in_main_only(self):
        s = self.open(fx.wal_only_data(self.tmp), ram_limit=0)
        self.assertEqual(s.mode, MAIN_ONLY)
        self.assertEqual(s.source("activity"), "native")
        self.assertEqual(s.count("activity"), 40)
        row = s.browse("activity", 0, 1).rows[0]
        self.assertEqual(s.row("activity", row.locator).values, row.values)
        self.assertTrue(any(b.level == "warning" for b in s.banners()))

    def test_main_only_matches_ram_overlay(self):
        path = fx.wal_states(self.tmp)
        a = self.open(path, ram_limit=0)
        want = [(r.locator, r.values) for r in a.browse("t", 0, 5000).rows]
        if ram_overlay_supported():
            other = os.path.join(self.tmp, "b")
            os.makedirs(other)
            b = self.open(copy_with_sidecars(path, other))
            self.assertEqual(b.mode, RAM_OVERLAY)
            self.assertEqual([(r.locator, r.values) for r in b.browse("t", 0, 5000).rows], want)
        self.assertEqual(want[-1][1][1], "after restart 2")

    def test_corrupt_table_falls_back_and_explains(self):
        s = self.open(fx.corrupt(self.tmp))
        self.assertEqual(len(s.browse("ok", 0, 100).rows), 50)
        page = s.browse("broken", 0, 100)
        self.assertEqual(page.rows, [])
        self.assertEqual(page.source, "native")
        self.assertIn("not a b-tree page", page.note)

    def test_not_sqlite_raises_session_error(self):
        p = os.path.join(self.tmp, "junk.db")
        with open(p, "wb") as f:
            f.write(b"\x00" * 4096)
        with self.assertRaises(SessionError):
            Session.open(p, hash_evidence=False)

    def test_close_reports_unchanged_and_writes_nothing(self):
        path = fx.wal_states(self.tmp)
        before = dir_snapshot(self.tmp)
        s = Session.open(path)
        for name in s.tables():
            s.browse(name, 0, 50)
            s.count(name)
        list(s.search("t", "restart", "ci"))
        self.assertTrue(s.close().unchanged)
        self.assertEqual(dir_snapshot(self.tmp), before)

    def test_mid_stream_sql_failure_yields_each_row_once(self):
        path = os.path.join(self.tmp, "mid.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA page_size=1024")
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
        c.executemany("INSERT INTO t(s) VALUES (?)",
                      [(("row %05d " % i) * 8,) for i in range(6000)])
        c.commit()
        root = c.execute("SELECT rootpage FROM sqlite_master WHERE name='t'").fetchone()[0]
        c.close()
        pager = Pager(path)
        segs = BTreeReader(pager, IssueLog()).segments(root, False)
        pager.close()
        page_no, _cell, lost = segs[-1]           # last leaf: far beyond the first 2000-row chunk
        with open(path, "r+b") as f:
            f.seek((page_no - 1) * 1024)
            f.write(b"\x3d")                      # destroy its page-type byte
        want = 6000 - lost
        s = self.open(path)
        rows = list(s.iter_rows("t"))
        self.assertIn("sql_scan_resumed", [i.kind for i in s.issues])   # SQL yielded >= 1 chunk first
        self.assertEqual(len(rows), want)
        self.assertEqual(len(set(r.locator for r in rows)), want)
        s2 = self.open(path)
        hits = list(s2.search("t", "row", "ci", limit=100000))
        self.assertEqual(len(hits), want)
        self.assertEqual(len(set(h["locator"] for h in hits)), want)


def count_to(n):
    """A read-only statement that runs for a while (about 1 s per 4-5 million on this machine)."""
    return ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < %d) "
            "SELECT count(*) FROM c" % n)


class InterruptTest(SessionTestBase):
    """Spec 7.2: long SQL is cancellable with conn.interrupt(); an interrupted statement is a
    cancellation, never a reason to switch the table to native reads."""

    def big_db(self):
        path = os.path.join(self.tmp, "big.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
        c.executemany("INSERT INTO t(s) VALUES (?)", [("row %05d" % i,) for i in range(6000)])
        c.commit()
        c.close()
        return path

    def run_in_thread(self, conn_factory, sql):
        out, started = {}, threading.Event()

        def run():
            conn = conn_factory()
            started.set()
            try:
                out["result"] = conn.execute(sql).fetchone()[0]
            except Exception as e:     # noqa: BLE001 - recorded for the assertions
                out["result"] = str(e)
                out["error"] = type(e).__name__
        th = threading.Thread(target=run, daemon=True)
        th.start()
        started.wait(5)
        time.sleep(0.3)                      # let the statement start running
        return th, out

    def test_interrupted_sql_is_a_cancellation_not_a_native_fallback(self):
        s = self.open(self.big_db())
        # Every statement on this thread's connection now fails with 'interrupted', exactly as
        # if Session.interrupt() had been called while it ran.
        s.conn().set_progress_handler(lambda: 1, 1)
        calls = [lambda: s.count("t"), lambda: s.browse("t", 0, 10),
                 lambda: s.row("t", Locator("rowid", 5)), lambda: list(s.iter_rows("t")),
                 lambda: list(s.search("t", "row", "ci"))]
        for call in calls:
            with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                call()
        self.assertEqual(s.source("t"), "sql")
        self.assertNotIn("sql_table_failed", [i.kind for i in s.issues])
        self.assertEqual(list(s.search("t", "row", "ci", cancel=lambda: True)), [])  # quiet stop
        self.assertEqual(s.source("t"), "sql")

    def test_close_during_a_parallel_search_is_safe(self):
        # Closing mid-search frees connections, cursors and errors on several threads at once:
        # on Python <= 3.10 any overlap of close() with another use crashed the process.
        path = os.path.join(self.tmp, "many.db")
        c = sqlite3.connect(path)
        for k in range(8):
            c.execute("CREATE TABLE t%d(id INTEGER PRIMARY KEY, s TEXT, n INTEGER)" % k)
            c.executemany("INSERT INTO t%d(s, n) VALUES (?, ?)" % k,
                          [("row %d hello" % i, i) for i in range(8000)])
        c.commit()
        c.close()
        for i in range(30):
            s = Session.open(path, hash_evidence=False)
            stop = [False]
            th = threading.Thread(target=lambda: list(s.search_tables(
                s.tables(), ("hello", "zzz", "1")[i % 3], "ci", limit=999999,
                cancel=lambda: stop[0], workers=4)))
            th.start()
            time.sleep((i % 5) * 0.01)
            stop[0] = bool(i % 2)
            s.close()
            th.join(10)
            self.assertFalse(th.is_alive())
            self.assertFalse([t for t in threading.enumerate() if t.name.startswith("search-")])
            self.assertNotIn("sql_table_failed", [x.kind for x in s.issues])   # no false fallback

    def test_close_interrupts_running_sql(self):
        s = Session.open(self.big_db(), hash_evidence=False)
        owned = s.new_connection()
        workers = [self.run_in_thread(s.conn, count_to(30000000)),
                   self.run_in_thread(lambda: owned, count_to(30000000))]
        t0 = time.time()
        s.close()
        took = time.time() - t0
        for th, out in workers:
            th.join(20)
            self.assertFalse(th.is_alive())
            # Always the interrupt: close() never closes a connection under a running statement
            # (that crashes Python <= 3.10, or fails the statement with some other error).
            self.assertEqual(out.get("result"), "interrupted", out)
        self.assertLess(took, 2.0)      # without interrupt(), close() waits for both statements
        # The worker let go of its own connection in time; the SQL-tab one is its caller's.
        self.assertEqual([(i.kind, i.severity) for i in s.issues if i.kind == "connection_left_open"],
                         [("connection_left_open", "info")])
        s.release_connection(owned)     # what the SQL-tab worker's `finally` does

    def test_close_leaves_a_connection_another_thread_holds_to_that_thread(self):
        s = Session.open(self.big_db(), hash_evidence=False)
        holding, go_on, out = threading.Event(), threading.Event(), {}

        def worker():
            conn = s.conn()
            conn.execute("SELECT count(*) FROM t").fetchone()
            holding.set()
            go_on.wait(10)              # still holds its connection while close() runs
            try:
                out["after"] = conn.execute("SELECT count(*) FROM t").fetchone()[0]
            except sqlite3.Error as e:
                out["after"] = str(e)
            out["ref"] = weakref.ref(conn)

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        self.assertTrue(holding.wait(10))
        t0 = time.time()
        s.close(wait=0.3)
        took = time.time() - t0
        left = [(i.severity, i.where) for i in s.issues if i.kind == "connection_left_open"]
        go_on.set()
        th.join(10)
        self.assertTrue(0.3 <= took < 2.0, took)       # waited for the thread, but bounded
        self.assertEqual(left, [("warning", th.name)])
        self.assertEqual(out["after"], 6000)            # its connection was not closed under it
        gc.collect()                    # Python 3.11+: freed by the cycle collector (see session)
        self.assertIsNone(out["ref"]())                 # ...and freed (closed) once let go

    def test_close_does_not_wait_for_a_thread_that_let_go(self):
        s = Session.open(self.big_db(), hash_evidence=False)
        used, done, out = threading.Event(), threading.Event(), {}

        def worker():
            out["ref"] = weakref.ref(s.conn())
            s.count("t")
            used.set()
            done.wait(10)               # alive, holding no connection (like a long native count)

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        self.assertTrue(used.wait(10))
        t0 = time.time()
        s.close(wait=2.0)
        took = time.time() - t0
        freed = out["ref"]() is None
        done.set()
        th.join(10)
        self.assertLess(took, 1.0)
        self.assertTrue(freed)                          # so closed, while its thread still runs
        self.assertNotIn("connection_left_open", [i.kind for i in s.issues])

    def test_close_closes_own_and_ended_threads_connections(self):
        s = Session.open(self.big_db(), hash_evidence=False)
        mine, theirs = s.conn(), {}
        th = threading.Thread(target=lambda: theirs.setdefault("conn", s.conn()))
        th.start()
        th.join(10)
        s.close(wait=0.3)
        for conn in (mine, theirs["conn"]):       # still referenced here, yet closed
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                conn.execute("SELECT 1")
        self.assertNotIn("connection_left_open", [i.kind for i in s.issues])
        # A closed session never opens the evidence again.
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            s.conn()
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            s.new_connection()

    def test_interrupt_one_thread_only(self):
        s = self.open(self.big_db())
        slow, slow_out = self.run_in_thread(s.conn, count_to(30000000))
        other, other_out = self.run_in_thread(s.conn, count_to(3000000))
        s.interrupt(slow)
        slow.join(20)
        other.join(20)
        self.assertEqual(slow_out.get("result"), "interrupted")
        self.assertEqual(other_out.get("result"), 3000000)


def open_native(test, path):
    """Open with SQLite refusing the file, so every table is read natively (NATIVE mode)."""
    with mock.patch.object(SqlBackend, "connect",
                           side_effect=sqlite3.DatabaseError("file is not a database")):
        return test.open(path)


class VisibilityTest(SessionTestBase):
    """Spec 8 error visibility: what the engine had to skip or could not know is announced."""

    def isolated(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        return d

    def test_unusable_wal_is_announced(self):
        for name, content in (("junk", b"\x00" * 4096), ("short", b"\x37\x7f\x06\x82")):
            path = fx.freelist(self.isolated(name))
            with open(path + "-wal", "wb") as f:
                f.write(content)
            s = self.open(path)
            self.assertIsNone(s.wal)
            self.assertIn("wal_unreadable", [i.kind for i in s.issues], name)
            self.assertTrue(any(b.level == "warning" and "NOT applied" in b.text
                                for b in s.banners()), name)
        path = fx.freelist(self.isolated("empty"))
        open(path + "-wal", "wb").close()          # empty -wal: normal after a checkpoint
        s = self.open(path)
        self.assertFalse(any("NOT applied" in b.text for b in s.banners()))

    @unittest.skipUnless(sqlite3.sqlite_version_info >= (3, 31, 0), "generated columns")
    def test_virtual_generated_columns_read_natively_are_announced(self):
        s = open_native(self, fx.quirks(self.tmp))
        self.assertEqual(s.mode, NATIVE)
        page = s.browse("gen", 0, 5)
        self.assertEqual(page.rows[0].values, [5, None, 6, "five"])
        self.assertIn("virtual_generated", page.rows[0].flags)
        self.assertIn("VIRTUAL generated column(s) b", page.note)
        self.assertTrue(any("VIRTUAL generated" in b.text and "gen" in b.text
                            for b in s.banners()))

    def test_file_sqlite_refuses_is_read_natively_with_flags(self):
        s = self.open(fx.unreadable_by_sqlite(self.tmp))
        self.assertEqual(s.mode, NATIVE)
        self.assertEqual(s.banners()[0].level, "error")
        row = s.browse("ev", 0, 5).rows[0]
        self.assertEqual((row.values, row.flags), ([1, None, None], {"damaged_record"}))

    def test_sql_served_generated_columns_need_no_note(self):
        s = self.open(fx.quirks(self.tmp))
        if sqlite3.sqlite_version_info >= (3, 31, 0):
            self.assertEqual(s.browse("gen", 0, 5).note, "")
        self.assertFalse(any("VIRTUAL generated" in b.text for b in s.banners()))


class OldPythonCollationTest(SessionTestBase):
    """Python 3.8-3.10 reject collation names such as Windows Search's
    'UNICODE_en-US_LINGUISTIC_IGNORECASE'; the database must still open and browse."""

    COLL = "UNICODE_en-US_LINGUISTIC_IGNORECASE"

    def gather_db(self):
        """A Windows Search style table whose column (and so its index) uses COLL.

        Python 3.8-3.10 cannot register COLL, so the table is built with NOCASE (a similar
        case-insensitive order for the index) and the stored CREATE TABLE text is then rewritten
        to name COLL, which is what the real file's sqlite_master holds.
        """
        path = os.path.join(self.tmp, "gather.db")
        c = sqlite3.connect(path)
        c.execute('CREATE TABLE SystemIndex_Gthr(id INTEGER PRIMARY KEY, '
                  'FileName TEXT COLLATE NOCASE, Size INTEGER DEFAULT 0)')
        c.execute('CREATE INDEX ix_name ON SystemIndex_Gthr(FileName)')
        c.executemany("INSERT INTO SystemIndex_Gthr(FileName, Size) VALUES (?, ?)",
                      [("b.txt", 1), ("A.txt", 2), ("c.txt", 3)])
        c.commit()
        version = c.execute("PRAGMA schema_version").fetchone()[0]
        c.execute("PRAGMA writable_schema=ON")
        c.execute("UPDATE sqlite_master SET sql = replace(sql, 'COLLATE NOCASE', ?) "
                  "WHERE name = 'SystemIndex_Gthr'", ('COLLATE "%s"' % self.COLL,))
        c.execute("PRAGMA schema_version=%d" % (version + 1))     # readers reload the schema
        c.commit()
        c.close()
        c = sqlite3.connect(path)
        stored = c.execute("SELECT sql FROM sqlite_master WHERE name = 'SystemIndex_Gthr'").fetchone()[0]
        c.close()
        self.assertIn('FileName TEXT COLLATE "%s"' % self.COLL, stored)
        return path

    def test_emulation_rejects_the_name(self):
        conn = py310_connect(sqlite3.connect)(":memory:")
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.create_collation(self.COLL, lambda a, b: 0)
        conn.create_collation("MYCOLL", lambda a, b: 0)

    def test_opens_logs_and_browses(self):
        path = self.gather_db()
        with mock.patch.object(sqlite3, "connect", py310_connect(sqlite3.connect)):
            s = self.open(path)
            self.assertEqual(s.mode, IMMUTABLE)
            unreg = [i for i in s.issues if i.kind == "collation_unregistered"]
            self.assertEqual([i.where for i in unreg], [self.COLL])
            t = s.info("SystemIndex_Gthr")
            self.assertEqual(t.metadata_source, "scratch")
            self.assertEqual(t.column_names, ["id", "FileName", "Size"])
            self.assertEqual(t.defaults, [None, None, 0])
            self.assertEqual(s.count("SystemIndex_Gthr"), 3)
            rows = s.browse("SystemIndex_Gthr", 0, 10).rows
            self.assertEqual(sorted(r.values[1] for r in rows), ["A.txt", "b.txt", "c.txt"])
            self.assertEqual(s.row("SystemIndex_Gthr", rows[0].locator).values, rows[0].values)
            names = [r.values[1] for r in
                     s.browse("SystemIndex_Gthr", 0, 10, order_by="FileName").rows]
            self.assertEqual(sorted(names), ["A.txt", "b.txt", "c.txt"])
            hits = list(s.search("SystemIndex_Gthr", "b.tx", "ci"))
            self.assertEqual([h["value"] for h in hits], ["b.txt"])
            self.assertTrue(any(self.COLL in b.text and "could not be registered" in b.text
                                for b in s.banners()))

    def test_open_failure_releases_everything(self):
        path = fx.wal_states(self.tmp)
        made = {}

        class SpyPager(Pager):
            def __init__(self, *a, **kw):
                made["pager"] = self
                Pager.__init__(self, *a, **kw)

        class SpyWal(WalFile):
            def __init__(self, *a, **kw):
                made["wal"] = self
                WalFile.__init__(self, *a, **kw)

        class SpyEvidence(EvidenceSet):
            def __init__(self, *a, **kw):
                made["evidence"] = self
                EvidenceSet.__init__(self, *a, **kw)

        with mock.patch("engine.session.Pager", SpyPager), \
                mock.patch("engine.session.WalFile", SpyWal), \
                mock.patch("engine.session.EvidenceSet", SpyEvidence), \
                mock.patch.object(SchemaModel, "describe_all", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                Session.open(path)
        self.assertIsNone(made["pager"]._mm)
        self.assertIsNone(made["pager"]._f)
        self.assertIsNone(made["wal"]._mm)
        self.assertTrue(made["evidence"]._cancel)


if __name__ == "__main__":
    unittest.main()
