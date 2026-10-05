"""Stress: Copy with related and the Database Map on a database of millions of rows, cancel
storms, closing the database mid-export, hostile values (huge BLOBs, invalid UTF-8, NULL keys,
cycles in the links). Skipped unless SGA_STRESS=1 (the fixture takes a while to build).

Each test checks: no exception escapes as a crash, every worker thread ends, memory stays
bounded, and the output is well formed."""

import gc
import io
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from engine import datamap as dm
from engine import related_copy as rc
from engine.backends import Filter
from engine.datamap import Cancelled
from engine.filters import value_expr
from engine.relations import relation_map
from engine.schema import Locator
from engine.session import Session

STRESS = os.environ.get("SGA_STRESS") == "1"
MESSAGES = 2000000
CHATS = 2000
BIG_CHAT_EVERY = 20             # every 20th message is in chat 1: 100,000 rows
HUGE = 20 << 20                 # a 20 MB BLOB


def build(directory):
    """The multi-million-row fixture (built once per run, in a temporary folder)."""
    path = os.path.join(directory, "big.db")
    c = sqlite3.connect(path)
    c.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE jid(_id INTEGER PRIMARY KEY, raw_string TEXT);
        CREATE TABLE chat(_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT,
                          created_timestamp INTEGER);
        CREATE TABLE message(_id INTEGER PRIMARY KEY, chat_row_id INTEGER,
                             sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT,
                             raw BLOB);
        CREATE INDEX message_chat ON message(chat_row_id);
        CREATE TABLE message_media(message_row_id INTEGER PRIMARY KEY, file_path TEXT,
                                   thumb BLOB);
        CREATE TABLE receipt(_id INTEGER PRIMARY KEY, message_row_id INTEGER,
                             jid_row_id INTEGER);
        CREATE TABLE node(_id INTEGER PRIMARY KEY, parent_node_id INTEGER REFERENCES node(_id),
                          name TEXT);
        CREATE TABLE hostile(_id INTEGER PRIMARY KEY, message_row_id INTEGER, chat_row_id TEXT,
                             note TEXT, big BLOB);
    """)
    c.execute("WITH RECURSIVE x(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM x WHERE i < 5000) "
              "INSERT INTO jid SELECT i, i || '@s.example' FROM x")
    c.execute("WITH RECURSIVE x(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM x WHERE i < ?) "
              "INSERT INTO chat SELECT i, 1 + i % 5000, 'chat ' || i, 1600000000000 + i * 1000 "
              "FROM x", (CHATS,))
    c.execute("WITH RECURSIVE x(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM x WHERE i < ?) "
              "INSERT INTO message SELECT i, CASE WHEN i % ? = 0 THEN 1 ELSE 2 + i % (? - 1) END, "
              "1 + i % 5000, 1600000000000 + i * 37, 'message ' || i, "
              "CASE WHEN i % 1000 = 0 THEN randomblob(64) END FROM x",
              (MESSAGES, BIG_CHAT_EVERY, CHATS))
    c.execute("INSERT INTO message_media SELECT _id, '/media/' || _id || '.jpg', "
              "CASE WHEN _id % 5000 = 0 THEN randomblob(256) END FROM message WHERE _id % 10 = 0")
    c.execute("WITH RECURSIVE x(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM x WHERE i < 1000000) "
              "INSERT INTO receipt SELECT i, 1 + (i * 7) % ?, 1 + i % 5000 FROM x", (MESSAGES,))
    # cycles: 1 -> 2 -> 3 -> 1, and a node that is its own parent
    c.executemany("INSERT INTO node VALUES (?,?,?)",
                  [(1, 2, "a"), (2, 3, "b"), (3, 1, "c"), (4, 4, "self")] +
                  [(i, 1 + i % 4, "n%d" % i) for i in range(5, 200)])
    # hostile rows: a NULL key, invalid UTF-8 in a key column, a 20 MB BLOB, a key that is text
    c.execute("INSERT INTO hostile VALUES (1, NULL, NULL, 'null keys', NULL)")
    c.execute("INSERT INTO hostile VALUES (2, 5, CAST(x'fffe31' AS TEXT), CAST(x'c328e2' AS "
              "TEXT), zeroblob(?))", (HUGE,))
    c.execute("INSERT INTO hostile VALUES (3, 999999999, '1', 'text key', randomblob(100))")
    c.execute("INSERT INTO hostile VALUES (4, 10, '1', 'ok', ?)", (os.urandom(HUGE),))
    c.execute("UPDATE message_media SET thumb = zeroblob(16777216) WHERE message_row_id = 10")
    c.commit()
    c.close()
    return path


def threads():
    return set(t for t in threading.enumerate() if t.is_alive())


@unittest.skipUnless(STRESS, "stress tests: set SGA_STRESS=1")
class StressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="sga_stress_")
        cls.out = tempfile.mkdtemp(prefix="sga_stress_out_")
        t0 = time.perf_counter()
        cls.path = build(cls.dir)
        cls.build_seconds = time.perf_counter() - t0
        print("\n[stress] fixture: %.0f MB in %.1f s" % (os.path.getsize(cls.path) / 1e6,
                                                         cls.build_seconds))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)
        shutil.rmtree(cls.out, ignore_errors=True)

    def setUp(self):
        self.before = threads()
        self.s = Session.open(self.path, hash_evidence=False)
        self.m = relation_map(self.s)

    def tearDown(self):
        report = self.s.close()
        self.assertTrue(report.unchanged, report.text())
        deadline = time.time() + 10
        while time.time() < deadline and threads() - self.before:
            time.sleep(0.05)
        left = [t.name for t in threads() - self.before]
        self.assertEqual(left, [], "threads left running")

    def say(self, text):
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        # a cp1252 console cannot show every character of the notes (e.g. an arrow)
        print(("[stress] " + text).encode(enc, "backslashreplace").decode(enc))

    # -- scale ---------------------------------------------------------------------------------
    def test_database_map_on_millions_of_rows(self):
        t0 = time.perf_counter()
        m = dm.database_map(self.s, self.m, tool_version="stress")
        took = time.perf_counter() - t0
        self.assertIsNotNone(m)
        sizes = {}
        for fmt in ("html", "markdown", "json"):
            p = os.path.join(self.out, dm.map_file_name("big.db", fmt))
            sizes[fmt] = dm.write_map(p, fmt, m, self.s.evidence.is_protected)
        with open(os.path.join(self.out, "big.db - Database Map.json"), encoding="utf-8") as f:
            json.load(f)
        self.say("database map: %.1f s; html %.0f KB, md %.0f KB, json %.0f KB; %d links"
                 % (took, sizes["html"] / 1024.0, sizes["markdown"] / 1024.0,
                    sizes["json"] / 1024.0, m.data["summary"]["confident_links"]))
        self.assertLess(took, 60)
        self.assertLess(sizes["html"], 5 << 20)

    def test_copy_1000_rows(self):
        rc.related_bundle(self.s, self.m, "message", [Locator("rowid", 1)])     # links checked
        locs = [Locator("rowid", i) for i in range(1000, 2000)]
        t0 = time.perf_counter()
        b = rc.related_bundle(self.s, self.m, "message", locs)
        text = b.render("markdown")
        took = time.perf_counter() - t0
        self.say("copy 1,000 rows: %.1f s, %d rows, %.1f MB" % (took, b.rows, len(text) / 1e6))
        self.assertEqual(len(b.roots), 1000)
        self.assertLess(took, 10)

    def test_stream_all_filtered_rows_with_bounded_memory(self):
        flt = Filter(col_exprs={"chat_row_id": value_expr(1)})
        n = self.s.count("message", flt)
        self.assertEqual(n, MESSAGES // BIG_CHAT_EVERY)
        rc.related_bundle(self.s, self.m, "message", [Locator("rowid", 1)])     # links checked
        gc.collect()
        tracemalloc.start()
        try:
            t0 = time.perf_counter()
            res = rc.export_related_file(self.s, self.m, "message",
                                         self.s.iter_rows("message", flt),
                                         os.path.join(self.out, "chat1.json"), "json", total=n)
            took = time.perf_counter() - t0
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        size = os.path.getsize(os.path.join(self.out, "chat1.json"))
        self.say("stream %s filtered rows: %.1f s (under tracemalloc), %s related, %.0f MB file, "
                 "peak Python memory %.0f MB; notes: %s" % (
                     format(n, ","), took, format(res.related, ","), size / 1e6, peak / 1e6,
                     "; ".join(res.notes)[:300]))
        self.assertTrue(res.complete)
        self.assertEqual(res.starts, n)
        self.assertLess(peak, 400e6)
        with open(os.path.join(self.out, "chat1.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["rows_written"], n)

    # -- cancel and close ------------------------------------------------------------------------
    def _run(self, fn):
        box = {}

        def target():
            try:
                box["result"] = fn()
            except BaseException as e:      # noqa: BLE001 - reported to the test
                box["error"] = e
            finally:
                self.s.release_thread_connection()
        th = threading.Thread(target=target, name="stress-worker", daemon=True)
        th.start()
        return th, box

    def test_cancel_storm(self):
        rng = random.Random(7)
        flt = Filter(col_exprs={"chat_row_id": value_expr(1)})
        jobs = [
            lambda stop: rc.export_related(self.s, self.m, "message",
                                           self.s.iter_rows("message", flt), io.StringIO(),
                                           "markdown", cancel=stop),
            lambda stop: rc.related_bundle(self.s, self.m, "message",
                                           [Locator("rowid", i) for i in range(1, 3000)],
                                           hops=2, cancel=stop),
            lambda stop: dm.database_map(self.s, self.m, cancel=stop),
        ]
        worst = 0.0
        for i in range(24):
            flag = [False]
            th, box = self._run(lambda: jobs[i % 3](lambda: flag[0]))
            time.sleep(rng.uniform(0, 0.4))
            t0 = time.perf_counter()
            flag[0] = True
            self.s.interrupt(th)
            th.join(15)
            worst = max(worst, time.perf_counter() - t0)
            self.assertFalse(th.is_alive(), "job %d still running after Stop" % i)
            err = box.get("error")
            self.assertTrue(err is None or isinstance(err, Cancelled), repr(err))
        self.say("cancel storm: 24 jobs stopped, slowest stop %.2f s" % worst)
        # the session still works
        self.assertEqual(self.s.count("chat"), CHATS)

    def test_close_the_database_mid_export(self):
        s2 = Session.open(self.path, hash_evidence=False)
        m2 = relation_map(s2)
        out = io.StringIO()
        box = {}

        def target():
            try:
                box["result"] = rc.export_related(s2, m2, "message", s2.iter_rows("message"),
                                                  out, "json")
            except BaseException as e:      # noqa: BLE001 - reported to the test
                box["error"] = e
            finally:
                try:
                    s2.release_thread_connection()
                except Exception:           # noqa: BLE001
                    pass
        th = threading.Thread(target=target, name="stress-close", daemon=True)
        th.start()
        time.sleep(1.5)
        report = s2.close()
        th.join(20)
        self.assertFalse(th.is_alive(), "the export did not end after close")
        self.assertTrue(report.unchanged, report.text())
        err = box.get("error")
        self.say("close mid-export: ended with %s" % (
            type(err).__name__ if err is not None else
            "a result (complete=%s)" % box["result"].complete))
        self.assertTrue(err is None or isinstance(err, (sqlite3.Error, Cancelled)), repr(err))

    # -- a case: links between databases ------------------------------------------------------
    def test_case_links_between_databases(self):
        """messages of 2M rows and a contacts database holding its jids as text: Copy with
        related across them (streamed, then stopped), and the whole-case map."""
        from engine.crossdb import find_links
        cpath = os.path.join(self.out, "contacts.db")
        if not os.path.exists(cpath):
            c = sqlite3.connect(cpath)
            c.execute("CREATE TABLE wa_contacts(_id INTEGER PRIMARY KEY, jid TEXT UNIQUE, "
                      "display_name TEXT)")
            c.execute("WITH RECURSIVE x(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM x WHERE "
                      "i < 4000) INSERT INTO wa_contacts SELECT i, i || '@s.example', "
                      "'Person ' || i FROM x")
            c.commit()
            c.close()
        s2 = Session.open(cpath, hash_evidence=False)
        try:
            t0 = time.perf_counter()
            res = find_links([(1, self.s), (2, s2)])
            links = [l for l in res.links if l.confident]
            self.assertTrue(any(l.src_table == "jid" or l.dst_table == "jid" for l in links),
                            res.links)
            dbs = [rc.Database(1, "big.db", self.s, self.m), rc.Database(2, "contacts.db", s2)]
            case = rc.CaseSpec(1, dbs, res.links)
            gc.collect()
            tracemalloc.start()
            try:
                out = io.StringIO()
                r = rc.export_related(None, None, "chat", self.s.iter_rows("chat"), out, "json",
                                      hops=2, case=case, total=CHATS)
                _cur, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            d = json.loads(out.getvalue())
            across = sum(1 for row in d["rows"] for g in row["related"]
                         for r2 in g["rows"] for g2 in r2["related"]
                         if g2.get("database") == "contacts.db")
            self.assertTrue(r.complete)
            self.assertGreater(across, 0)
            # stopped half way: the output still ends properly
            stop = [False]
            out = io.StringIO()
            r2 = rc.export_related(None, None, "message", self.s.iter_rows("message"), out,
                                   "markdown", case=case, cancel=lambda: stop[0],
                                   progress=lambda n, t: stop.__setitem__(0, n >= 5000))
            self.assertFalse(r2.complete)
            self.assertIn("Stopped after", out.getvalue())
            cm = dm.case_map([dm.MapDatabase(1, "big.db", self.s, self.m, "#1f77b4"),
                              dm.MapDatabase(2, "contacts.db", s2, None, "#d62728")],
                             res.links)
            self.assertGreaterEqual(cm.data["summary"]["cross_links"], 1)
            h = dm.render_map(cm, "html")
            self.say("case: links found in %.1f s; %d chats with 2 links across in %.1f s "
                     "(%d contact rows reached, peak Python memory %.0f MB); case map html "
                     "%.0f KB" % (res.profile_seconds + res.check_seconds, CHATS, r.seconds,
                                  across, peak / 1e6, len(h) / 1024.0))
            self.assertLess(peak, 300e6)
        finally:
            s2.close()

    # -- hostile values ------------------------------------------------------------------------
    def test_hostile_values(self):
        gc.collect()
        tracemalloc.start()
        try:
            deep = {"related_hops": 3}
            b = rc.related_bundle(self.s, self.m, "hostile",
                                  [Locator("rowid", i) for i in (1, 2, 3, 4, 99)], hops=3,
                                  limits=deep)
            texts = dict((fmt, b.render(fmt)) for fmt in rc.FORMATS)
            cyc = rc.related_bundle(self.s, self.m, "node",
                                    [Locator("rowid", i) for i in (1, 4)], hops=3, limits=deep)
            ctext = cyc.render("json")
            media = rc.related_bundle(self.s, self.m, "message", [Locator("rowid", 10)])
            mtext = media.render("markdown")
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        d = json.loads(texts["json"])
        self.assertEqual(len(d["rows"]), 4)
        self.assertIn("1 selected row not found", d["notes"])
        # the 20 MB BLOBs are described, never dumped
        self.assertLess(len(texts["json"]), 1 << 20)
        self.assertLess(len(mtext), 1 << 20)
        c = json.loads(ctext)
        rows = [r["table"] + r["row"] for r in c["rows"]]
        self.assertEqual(len(rows), len(set(rows)))
        self.say("hostile values: %d rows (hops 3), cycles %d rows, peak Python memory %.0f MB"
                 % (b.rows, cyc.rows, peak / 1e6))
        self.assertLess(peak, 200e6)
        m = dm.database_map(self.s, self.m, dm.MapOptions(sample_rows=3))
        self.assertIsNotNone(m)
        json.loads(dm.map_json(m))
        self.assertIn("hostile", dm.map_html(m))
