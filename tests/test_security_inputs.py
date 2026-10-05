"""Hostile input files: every crafted input is built here from code, opened through the engine,
and must give bounded time and memory, a clear message instead of a crash, and an untouched
evidence folder."""

import lzma
import os
import shutil
import sqlite3
import struct
import sys
import time
import unittest
import zlib

from tests.helpers import TempDirTest, dir_snapshot
from engine import limits, sqlsafe
from engine.decode import decode_blob, interpretations, summary
from engine.fileformat.btree import local_payload_size
from engine.fileformat.wal import WalCancelled, WalFile, wal_checksum
from engine.schema import TableInfo, describe_table
from engine.session import NATIVE, OpenCancelled, Session

PAD = ", ".join("c%02d TEXT" % i for i in range(40))


def make_db(path, statements, page_size=1024):
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=%d" % page_size)
    c.execute("PRAGMA journal_mode=DELETE")
    for s in statements:
        c.execute(s)
    c.commit()
    c.close()
    return path


def replace_master_sql(path, old, new):
    """Overwrite a sqlite_master SQL text in place (same length, padded with spaces)."""
    with open(path, "rb") as f:
        buf = bytearray(f.read())
    i = buf.find(old)
    assert i >= 0 and len(new) <= len(old)
    buf[i:i + len(old)] = new.ljust(len(old), b" ")
    with open(path, "wb") as f:
        f.write(buf)


def put_varint(v):
    if v > 0x00FFFFFFFFFFFFFF:
        out = bytearray([v & 0xFF])
        v >>= 8
        for _ in range(8):
            out.insert(0, (v & 0x7F) | 0x80)
            v >>= 7
        return bytes(out)
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.insert(0, (v & 0x7F) | 0x80)
        v >>= 7
    return bytes(out)


def rechain_wal(buf, page_size):
    big = struct.unpack_from(">I", buf, 0)[0] == 0x377F0683
    s0, s1 = wal_checksum(bytes(buf[0:24]), 0, 0, big)
    struct.pack_into(">II", buf, 24, s0, s1)
    off = 32
    while off + 24 + page_size <= len(buf):
        s0, s1 = wal_checksum(bytes(buf[off:off + 8]), s0, s1, big)
        s0, s1 = wal_checksum(bytes(buf[off + 24:off + 24 + page_size]), s0, s1, big)
        struct.pack_into(">II", buf, off + 16, s0, s1)
        off += 24 + page_size


class LimitsSet(object):
    """with LimitsSet(name=value, ...): limits in force for the block."""

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        limits.load({"limits": self.values})
        assert not limits.problems, limits.problems

    def __exit__(self, *exc):
        limits.reset()


class HostileDatabaseTest(TempDirTest):
    def folder(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        return d

    def open_all(self, path, **kw):
        """Open, browse and count every table and view, run the audit, close; returns the
        session (closed), the page notes and the seconds taken. The folder must not change."""
        folder = os.path.dirname(path)
        before = dir_snapshot(folder)
        t0 = time.time()
        s = Session(path, hash_evidence=False, **kw)
        notes = {}
        try:
            for name in s.tables() + s.views():
                notes[name] = s.browse(name, 0, 200).note
                try:
                    s.count(name)
                except sqlsafe.SQL_ERRORS as e:
                    notes[name + " count"] = str(e)
            s.forensics.audit(time_limit=20)
        finally:
            s.close(verify=False)
        self.assertEqual(dir_snapshot(folder), before)
        return s, notes, time.time() - t0

    # -- schema text replayed with no power ------------------------------------------------
    def test_create_as_select_never_runs_its_query(self):
        p = make_db(os.path.join(self.folder("ctas"), "e.db"), ["CREATE TABLE x(%s)" % PAD])
        replace_master_sql(p, ("CREATE TABLE x(%s)" % PAD).encode(),
                           b"CREATE TABLE x AS WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL "
                           b"SELECT i+1 FROM c) SELECT max(i) AS m FROM c")
        s, _notes, secs = self.open_all(p)
        self.assertLess(secs, 10)
        self.assertEqual(s.mode, NATIVE)        # SQLite refuses such a schema
        self.assertIn("never run", s.info("x").error)
        self.assertTrue(any(i.kind == "schema_replay_failed" for i in s.issues.items))

    def test_scratch_refuses_queries_and_stops_at_its_step_budget(self):
        with sqlsafe.Scratch() as sc:
            t0 = time.time()
            with self.assertRaises(sqlsafe.ReplayError):
                sc.replay("CREATE TABLE x AS WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL "
                          "SELECT i+1 FROM c) SELECT i FROM c")
            with self.assertRaises(sqlsafe.ReplayError):
                sc.replay("CREATE VIEW v AS SELECT 1")
            with self.assertRaises(sqlsafe.ReplayError):
                sc.pragma("PRAGMA writable_schema=ON")
            self.assertLess(time.time() - t0, 2)
        with sqlsafe.Scratch(steps=20000) as sc:
            with self.assertRaises(sqlsafe.ReplayError) as cm:
                sc._run("WITH a(i) AS (VALUES (1), (2), (3), (4), (5), (6), (7), (8), (9)) "
                        "SELECT count(*) FROM a, a b, a c, a d, a e, a f", select=True)
            self.assertIn("schema_replay_steps", str(cm.exception))

    def test_trailing_statements_and_nul_are_cut_off(self):
        for text in ("CREATE TABLE x(a, b); SELECT 1", "CREATE TABLE x(a, b)\x00; junk",
                     "CREATE TABLE x(a, b); ATTACH 'f' AS y; \n.shell calc"):
            t = TableInfo("x", "table", 2, text)
            describe_table(t, set())
            self.assertEqual(t.column_names, ["a", "b"], text)
        self.assertEqual(sqlsafe.first_statement("CREATE TRIGGER t AFTER INSERT ON x BEGIN "
                                                 "SELECT 1; SELECT 2; END; DROP TABLE x"),
                         ("CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 1; SELECT 2; END",
                          True))
        self.assertEqual(sqlsafe.first_statement("CREATE TABLE 'a;b'(\"c;\" /* ; */ -- ;\n)"),
                         ("CREATE TABLE 'a;b'(\"c;\" /* ; */ -- ;\n)", False))
        self.assertEqual(sqlsafe.first_statement(
            "CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT CASE WHEN 1 THEN 2 END; END; X"),
            ("CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT CASE WHEN 1 THEN 2 END; END", True))
        # linear time, whatever the text holds
        from engine import sqltext
        t0 = time.time()
        for text in ("CREATE TABLE x(a DEFAULT '" + ";" * 500000 + "'); X",
                     "CREATE TRIGGER t AFTER INSERT ON x BEGIN " + "SELECT 1;" * 50000 + " X",
                     "CREATE TABLE x(" + "a; " * 100000):
            sqlsafe.first_statement(text)
            sqltext.first_statement(text)
        self.assertLess(time.time() - t0, 5)

    def test_two_statements_open_on_every_python(self):
        p = make_db(os.path.join(self.folder("two"), "e.db"), ["CREATE TABLE x(%s)" % PAD])
        replace_master_sql(p, ("CREATE TABLE x(%s)" % PAD).encode(), b"CREATE TABLE x(a); SELECT 1")
        s, _notes, _secs = self.open_all(p)
        self.assertEqual(s.info("x").column_names, ["a"])

    def test_invalid_utf8_names_do_not_stop_open_or_browse(self):
        p = make_db(os.path.join(self.folder("names"), "e.db"),
                    ["CREATE TABLE t(abc TEXT DEFAULT 'xyz', def INTEGER)",
                     "INSERT INTO t VALUES ('one', 1)"])
        with open(p, "rb") as f:
            buf = f.read()
        buf = buf.replace(b"abc TEXT DEFAULT 'xyz'", b"a\xffc TE\xfeT DEFAULT 'x\xfdz'")
        with open(p, "wb") as f:
            f.write(buf)
        s, notes, _secs = self.open_all(p)
        self.assertEqual(len(s.info("t").columns), 2)

    def test_probe_errors_that_are_not_sqlite_errors_fall_back_to_native(self):
        p = make_db(os.path.join(self.folder("probe"), "e.db"),
                    ["CREATE TABLE t(a)", "INSERT INTO t VALUES (1)"])
        from engine import backends
        real = backends.SqlBackend.connect
        for exc in (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
                    MemoryError(), sqlite3.Warning("one statement")):
            def connect(self, exc=exc):
                conn = real(self)

                class Bad(object):
                    def execute(self, *a):
                        raise exc

                    def close(self):
                        conn.close()
                return Bad()
            backends.SqlBackend.connect = connect
            try:
                s = Session(p, hash_evidence=False)
            finally:
                backends.SqlBackend.connect = real
            self.assertEqual(s.mode, NATIVE)
            self.assertEqual(s.count("t"), 1)
            s.close(verify=False)

    def test_browse_errors_that_are_not_sqlite_errors_fall_back_to_native(self):
        p = make_db(os.path.join(self.folder("browse"), "e.db"),
                    ["CREATE TABLE t(a)", "INSERT INTO t VALUES (1), (2)"])
        for exc in (MemoryError(), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            s = Session(p, hash_evidence=False)
            try:
                real = s.conn()

                class Bad(object):
                    sga_guard = getattr(real, "sga_guard", None)

                    def execute(self, *a, **k):
                        raise exc
                s._local.conn = Bad()
                page = s.browse("t", 0, 10)
                self.assertEqual((page.source, len(page.rows)), ("native", 2))
                self.assertIn("t", s._sql_failed)
            finally:
                s._local.conn = real
                s.close(verify=False)

    # -- values SQLite computes on read ---------------------------------------------------
    def test_view_of_huge_values_stops_with_a_message(self):
        p = make_db(os.path.join(self.folder("vz"), "e.db"), [
            "CREATE TABLE t(a)", "INSERT INTO t VALUES (1), (2), (3), (4)",
            "CREATE VIEW v AS SELECT a, zeroblob(4000000) AS big FROM t"])
        with LimitsSet(sql_value_bytes=1 << 20):
            _s, notes, secs = self.open_all(p)
        self.assertLess(secs, 10)
        if sqlsafe.HAS_SETLIMIT:
            self.assertIn("sql_value_bytes", notes["v"])
        else:
            self.assertIn("zeroblob()", notes["v"])

    def test_generated_column_of_huge_values_is_bounded(self):
        p = make_db(os.path.join(self.folder("gz"), "e.db"), [
            "CREATE TABLE g(a, big BLOB GENERATED ALWAYS AS (zeroblob(4000000)) VIRTUAL)",
            "INSERT INTO g(a) VALUES (1), (2), (3), (4)"])
        with LimitsSet(sql_value_bytes=1 << 20):
            s, notes, _secs = self.open_all(p)
        # read natively (the computed column is NULL there, and the page says so)
        self.assertIn("VIRTUAL generated column", notes["g"])

    def test_view_with_an_endless_query_stops_at_the_step_budget(self):
        p = make_db(os.path.join(self.folder("vc"), "e.db"), [
            "CREATE TABLE t(a)",
            "CREATE VIEW v AS WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM c) "
            "SELECT max(i) AS m FROM c"])
        with LimitsSet(sql_view_steps=200000):
            _s, notes, secs = self.open_all(p)
        self.assertLess(secs, 10)
        self.assertTrue("sql_view_steps" in notes["v"] or "recursive" in notes["v"], notes)

    def test_window_of_computed_values_is_capped(self):
        p = make_db(os.path.join(self.folder("vw"), "e.db"), [
            "CREATE TABLE t(a, d BLOB)",
            "CREATE VIEW v AS SELECT a, d FROM t"])
        c = sqlite3.connect(p)
        c.executemany("INSERT INTO t VALUES (?, ?)", [(i, bytes(300000)) for i in range(20)])
        c.commit()
        c.close()
        with LimitsSet(sql_window_bytes=1 << 20):
            s = Session(p, hash_evidence=False)
            page = s.browse("v", 0, 200)
            s.close(verify=False)
        self.assertLess(len(page.rows), 20)
        self.assertIn("sql_window_bytes", page.note)

    def test_reader_connections_are_hardened(self):
        p = make_db(os.path.join(self.folder("h"), "e.db"), ["CREATE TABLE t(a)"])
        s = Session(p, hash_evidence=False)
        try:
            c = s.conn()
            self.assertEqual(c.execute("PRAGMA trusted_schema").fetchone()[0], 0)
            self.assertEqual(c.execute("PRAGMA cell_size_check").fetchone()[0], 1)
            self.assertEqual(c.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute("ATTACH ':memory:' AS x")
            if sqlsafe.HAS_SETLIMIT:
                self.assertEqual(c.getlimit(sqlite3.SQLITE_LIMIT_LENGTH),
                                 limits.get("sql_value_bytes"))
        finally:
            s.close(verify=False)

    # -- native parser bounds -------------------------------------------------------------
    def overflow_db(self, size):
        p = make_db(os.path.join(self.folder("ov%d" % size.bit_length()), "e.db"),
                    ["CREATE TABLE x(a)"])
        with open(p, "rb") as f:
            buf = bytearray(f.read())
        off = 120
        struct.pack_into(">H", buf, 103, 1)
        struct.pack_into(">H", buf, 105, off)
        struct.pack_into(">H", buf, 108, off)
        cell = put_varint(size) + put_varint(1)
        local = local_payload_size(size, 1024, True)
        start = off + len(cell)
        rec = bytes([6, 0, 0, 0, 0, 0])
        buf[off:start] = cell
        buf[start:start + local] = rec + bytes(local - len(rec))
        buf[start + local:start + local + 4] = b"\x00\x00\x00\x00"
        with open(p, "wb") as f:
            f.write(buf)
        return p

    def test_missing_overflow_chain_is_never_padded(self):
        import tracemalloc
        for size in (1 << 29, 1 << 31, 1 << 63):
            p = self.overflow_db(size)
            tracemalloc.start()
            try:
                s, _notes, secs = self.open_all(p)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 64 << 20, size)
            self.assertLess(secs, 10)
            kinds = set(i.kind for i in s.issues.items)
            self.assertTrue(kinds & {"payload_cut", "overflow_truncated"}, kinds)

    def test_forged_page_count_is_clamped(self):
        p = make_db(os.path.join(self.folder("pc"), "e.db"),
                    ["PRAGMA auto_vacuum=FULL", "CREATE TABLE x(a)", "INSERT INTO x VALUES (1)"])
        with open(p, "rb") as f:
            buf = bytearray(f.read())
        change = struct.unpack_from(">I", buf, 24)[0]
        struct.pack_into(">I", buf, 28, 0xFFFFFFFF)
        struct.pack_into(">I", buf, 92, change)
        with open(p, "wb") as f:
            f.write(buf)
        s, _notes, secs = self.open_all(p)
        self.assertLess(secs, 10)
        self.assertLess(s.pager.page_count, 100)
        self.assertEqual(s.pager.declared_page_count, 0xFFFFFFFF)
        self.assertTrue(s.safe_parse)               # damage the native check found
        self.assertTrue(any(b.short == "Declared size ignored" for b in s.banners()))

    def wal_db(self, name):
        d = self.folder(name)
        work = os.path.join(d, "work")
        os.makedirs(work)
        w = os.path.join(work, "e.db")
        c = sqlite3.connect(w, isolation_level=None)
        c.execute("PRAGMA page_size=512")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE t(a)")
        c.execute("INSERT INTO t VALUES ('x')")
        p = os.path.join(d, "e.db")
        shutil.copyfile(w, p)
        shutil.copyfile(w + "-wal", p + "-wal")
        c.close()
        shutil.rmtree(work)
        return p

    def test_forged_wal_database_size_does_not_size_the_overlay(self):
        p = self.wal_db("wsize")
        with open(p + "-wal", "rb") as f:
            buf = bytearray(f.read())
        last, off = None, 32
        while off + 24 + 512 <= len(buf):
            if struct.unpack_from(">I", buf, off + 4)[0]:
                last = off
            off += 24 + 512
        struct.pack_into(">I", buf, last + 4, 1000000)
        rechain_wal(buf, 512)
        with open(p + "-wal", "wb") as f:
            f.write(buf)
        s, _notes, secs = self.open_all(p)
        self.assertLess(secs, 10)
        self.assertLess(s.pager.page_count * s.pager.page_size, 1 << 20)

    def test_wal_parse_can_be_stopped_and_is_limited(self):
        p = self.wal_db("wmany")
        with open(p + "-wal", "rb") as f:
            src = f.read()
        frame = bytearray(src[32:32 + 24 + 512])
        struct.pack_into(">I", frame, 4, 0)
        n = 20000
        buf = bytearray(src[:32]) + frame * n
        struct.pack_into(">I", buf, 32 + (n - 1) * (24 + 512) + 4, 2)
        rechain_wal(buf, 512)
        with open(p + "-wal", "wb") as f:
            f.write(buf)
        with self.assertRaises(WalCancelled):
            WalFile(p + "-wal", cancel=lambda: True)
        with WalFile(p + "-wal", max_frames=1000) as w:
            self.assertTrue(w.frames_cut)
            self.assertEqual((len(w.frames), w.frames_total), (1000, n))
        with self.assertRaises(OpenCancelled):
            Session(p, hash_evidence=False, cancel=lambda: True)
        with LimitsSet(wal_frames=1000):
            s = Session(p, hash_evidence=False)
            self.assertTrue(any(b.short == "WAL cut" for b in s.banners()))
            s.close(verify=False)

    # -- safe parse -----------------------------------------------------------------------
    def test_safe_parse_never_gives_the_file_to_sqlite(self):
        p = make_db(os.path.join(self.folder("safe"), "e.db"),
                    ["CREATE TABLE t(a, b)", "INSERT INTO t VALUES (1, 'x')",
                     "CREATE VIEW v AS SELECT a FROM t"])
        s, notes, _secs = self.open_all(p, safe_parse=True)
        self.assertEqual((s.mode, s.safe_parse, s.sql), (NATIVE, True, None))
        self.assertTrue(any(b.short == "Safe parse" for b in s.banners()))
        self.assertIn("cannot be read without SQLite", notes["v"])
        from database import DB
        db = DB()
        db.open(p, safe_parse=True)
        try:
            self.assertEqual(db.mode, "safe-parse")
            self.assertEqual(db.count("t"), 1)
        finally:
            db.close()

    def test_old_sqlite_is_named(self):
        p = make_db(os.path.join(self.folder("old"), "e.db"), ["CREATE TABLE t(a)"])
        saved = sqlsafe.SQLITE_ADVISED
        sqlsafe.SQLITE_ADVISED = (99, 0, 0)
        try:
            s = Session(p, hash_evidence=False)
            self.assertTrue(any(b.short.startswith("Old SQLite") for b in s.banners()))
            s.close(verify=False)
        finally:
            sqlsafe.SQLITE_ADVISED = saved


class HostileBlobTest(unittest.TestCase):
    def test_lzma_dictionary_from_the_header_is_not_allocated(self):
        body = lzma.compress(b"hello" * 100, format=lzma.FORMAT_ALONE)
        for dict_size in (1 << 30, 3 << 30):
            blob = body[:1] + struct.pack("<I", dict_size) + body[5:]
            t0 = time.time()
            root = decode_blob(blob)
            self.assertLess(time.time() - t0, 2)
            text = repr([n.note for n in root.walk()]) if hasattr(root, "walk") else repr(root)
            self.assertIn("decode_lzma_memory", text + root.note)
            summary(blob)
            interpretations(blob)

    def test_xz_dictionary_beyond_the_memory_limit_is_refused(self):
        data = lzma.compress(b"hello" * 100, format=lzma.FORMAT_XZ, check=lzma.CHECK_NONE)
        buf = bytearray(data)
        hsize = (buf[12] + 1) * 4
        head = buf[12:12 + hsize]
        k = head.find(b"\x21\x01")              # LZMA2 filter, one property byte
        self.assertGreater(k, 0)
        head[k + 2] = 40                        # dictionary 4 GiB - 1
        struct.pack_into("<I", head, hsize - 4, zlib.crc32(bytes(head[:hsize - 4])) & 0xFFFFFFFF)
        buf[12:12 + hsize] = head
        t0 = time.time()
        root = decode_blob(bytes(buf))
        self.assertLess(time.time() - t0, 2)
        self.assertIn("decode_lzma_memory", _all_notes(root))

    def test_decode_budgets_are_settings_named_when_hit(self):
        blob = b"[" + b"[1, 2, 3, 4, 5, 6, 7, 8, 9]," * 400 + b"0]"
        with LimitsSet(decode_max_nodes=200):
            root = decode_blob(blob)
        self.assertIn("decode_max_nodes", root.note)

    def test_xml_doctype_with_a_subset_anywhere_is_refused(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "src"))
        import value_viewer
        text = ("<r><!--" + "x" * 5000 + "--></r>").replace(
            "<r>", '<!DOCTYPE r [<!ENTITY a "b">]><r>', 1)
        with self.assertRaises(ValueError):
            value_viewer.pretty(text)
        with self.assertRaises(ValueError):
            value_viewer.pretty("<a>" * 100000 + "</a>" * 100000)

    def test_image_pixel_budget(self):
        import previews
        def chunk(kind, data):
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", 13000, 13000, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(b"\x00" * 100000)) + chunk(b"IEND", b""))
        self.assertEqual(previews.header_size(png), (13000, 13000))
        with self.assertRaises(previews.PreviewRefused) as cm:
            previews.check_size(13000, 13000)
        self.assertIn("preview_pixels", str(cm.exception))
        w, h = previews.zoomed_size(13000, 13000, 10.0)
        self.assertLessEqual(w * h, limits.get("preview_pixels"))
        from constants import HAS_PIL
        if HAS_PIL:
            with self.assertRaises(previews.PreviewRefused):
                previews.thumbnail(png)


def _all_notes(node):
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n.note or "")
        stack.extend(n.children or ())
    return " | ".join(out)


@unittest.skipUnless(sys.platform == "win32", "Windows locks")
class LockedEvidenceTest(TempDirTest):
    def test_locked_lock_byte_page_is_hashed_as_zeros_and_said(self):
        import ctypes
        import msvcrt
        from ctypes import wintypes as wt
        from engine.evidence import _sha256
        from engine.locks import LOCK_BYTE
        p = os.path.join(self.tmp, "big.db")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                        ctypes.c_void_p]
        with open(p, "wb") as f:
            ret = wt.DWORD()
            if not k32.DeviceIoControl(msvcrt.get_osfhandle(f.fileno()), 0x900C4, None, 0,
                                       None, 0, ctypes.byref(ret), None):
                self.skipTest("no sparse files here")
            f.write(b"A" * 4096)
            f.seek(LOCK_BYTE + 4096)
            f.write(b"B" * 4096)
        plain = _sha256(p)
        locker = open(p, "rb+")
        try:
            locker.seek(LOCK_BYTE)
            msvcrt.locking(locker.fileno(), msvcrt.LK_NBLCK, 512)
            try:
                notes = []
                self.assertEqual(_sha256(p, notes=notes), plain)   # those bytes are zeros
                self.assertTrue(notes and "lock-byte page" in notes[0], notes)
            finally:
                locker.seek(LOCK_BYTE)
                msvcrt.locking(locker.fileno(), msvcrt.LK_UNLCK, 512)
        finally:
            locker.close()

    def test_sharing_violation_names_the_program(self):
        import ctypes
        from ctypes import wintypes as wt
        from engine.session import SessionError
        p = make_db(os.path.join(self.tmp, "held.db"), ["CREATE TABLE t(a)"])
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                    wt.DWORD, wt.HANDLE]
        h = k32.CreateFileW(p, 0x80000000, 0, None, 3, 0x80, None)   # GENERIC_READ, no sharing
        self.assertNotEqual(h, wt.HANDLE(-1).value)
        try:
            with self.assertRaises(SessionError) as cm:
                Session(p, hash_evidence=False)
            text = str(cm.exception)
            self.assertIn("in use by", text)
            self.assertIn("PID %d" % os.getpid(), text)
        finally:
            k32.CloseHandle(h)


if __name__ == "__main__":
    unittest.main()
