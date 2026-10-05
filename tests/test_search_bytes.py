"""Searching BLOB content through the engine: SQL pre-filters, WAL, freelist, cancel, decoded."""
import base64
import gzip
import os
import plistlib
import shutil
import sqlite3
import sys
import threading
import unittest
import zlib
from unittest import mock

from tests.helpers import TempDirTest
from tests.fixtures import make_fixtures as fx
from engine.fileformat.freelist import page_cells
from engine.search import DecodedSearchUnavailable, Matcher, blob_lower_works
from engine.session import Session

from database import DB

try:
    from engine.decode import decoded_strings as REAL_DECODER
except ImportError:
    REAL_DECODER = None


def le(s):
    return s.encode("utf-16-le")


def be(s):
    return s.encode("utf-16-be")


def blob_docs(directory):
    """BLOBs holding text in each encoding and hex bytes, text in a BLOB column, a BLOB in a
    TEXT column, and filler rows that match nothing."""
    path = os.path.join(directory, "blobs.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, label TEXT, data BLOB, info TEXT, "
              "n INTEGER)")
    c.executemany("INSERT INTO docs VALUES (?,?,?,?,?)", [
        (1, "le", b"\x00\x00" + le("Secret Plan") + b"\x00\x00", "x", 1),
        (2, "be", b"\x07" + be("secret plan"), "x", 2),
        (3, "nul first", b"\x00secret plan\x00", "x", 3),
        (4, "text in a blob column", "secret plan as text", "x", 4),
        (5, "blob in a text column", None, b"\xff\xfe" + le("Secret plan in text"), 5),
        (6, "nibbles", bytes.fromhex("148656c0"), "x", 6),
        (7, "bytes", bytes.fromhex("0048656c00"), "x", 7),
        (8, "bom", b"\xef\xbb\xbfsecret plan", "y", 8),
        (9, "digits", b"id 2024-07", "z", 9)] +
        [(i, "f%d" % i, bytes((i * j) % 251 for j in range(64)), "f", i) for i in range(10, 400)])
    c.commit()
    c.close()
    return path


def wal_blobs(directory):
    """BLOB rows that exist only in committed WAL frames."""
    path = os.path.join(directory, "wal_blobs.db")
    work = os.path.join(directory, "_walb_work")
    os.makedirs(work)
    wpath = os.path.join(work, "wal_blobs.db")
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE b(id INTEGER PRIMARY KEY, data BLOB)")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("INSERT INTO b VALUES (1, ?)", (b"\x09\x00" + le("wal secret") + b"\x00\x00",))
    c.execute("INSERT INTO b VALUES (2, ?)", (b"\x01\xde\xad\xbe\xef",))
    for sfx in ("", "-wal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.close()
    shutil.rmtree(work, ignore_errors=True)
    return path


def freed_blobs(directory):
    """Deleted rows whose BLOBs hold UTF-16 text, left on freelist pages."""
    path = os.path.join(directory, "freed_blobs.db")
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE att(id INTEGER PRIMARY KEY, data BLOB)")
    c.executemany("INSERT INTO att VALUES (?, ?)",
                  [(i, b"\x05\x00" + le("gone secret %03d" % i) + b"\x00" * 200) for i in range(1, 201)])
    c.commit()
    c.execute("DELETE FROM att WHERE id > 5")
    c.commit()
    c.close()
    return path


def full_scan():
    """Patch that makes every search read every row (no SQL pre-filter)."""
    return mock.patch.object(Matcher, "sql_where", return_value=("", []))


class SearchBytesTest(TempDirTest):
    def open(self, path):
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        return s

    @staticmethod
    def found(s, table, term, mode, deep=False, limit=10000):
        return sorted((h["locator"].value, h["column"], h["encoding"], h["offset"], h["value"])
                      for h in s.search(table, term, mode, limit, deep))

    def ids(self, s, term, mode, deep=False):
        return sorted(set(h[0] for h in self.found(s, "docs", term, mode, deep) if h[0] < 10))

    def test_blob_mode_finds_text_in_every_encoding_and_says_where(self):
        s = self.open(blob_docs(self.tmp))
        got = dict((h[0], h[1:4]) for h in self.found(s, "docs", "secret plan", "blob") if h[0] < 10)
        self.assertEqual(got, {1: ("data", "utf-16le", 2), 2: ("data", "utf-16be", 1),
                               3: ("data", "utf-8", 1), 4: ("data", "text", None),
                               5: ("info", "utf-16le", 2), 8: ("data", "utf-8", 3)})

    def test_text_modes_search_blobs_only_with_deep_blob(self):
        s = self.open(blob_docs(self.tmp))
        self.assertEqual(self.ids(s, "secret plan", "ci"), [])       # as before: BLOBs not read
        self.assertEqual(self.ids(s, "SECRET PLAN", "ci", True), [1, 2, 3, 4, 5, 8])
        self.assertEqual(self.ids(s, "Secret Plan", "cs", True), [1])
        self.assertEqual(self.ids(s, "secret plan", "ex", True), [8])
        self.assertEqual(self.ids(s, "Secret", "sw", True), [4, 5, 8])      # 5 and 8 after a BOM
        self.assertEqual(self.ids(s, "PLAN", "ew", True), [1, 2, 3, 8])
        self.assertEqual(self.ids(s, r"secret\s+plan", "rx", True), [2, 3, 4, 8])
        self.assertEqual(self.ids(s, "2024-07", "ci", True), [9])

    def test_hex_is_byte_aligned(self):
        s = self.open(blob_docs(self.tmp))
        hits = self.found(s, "docs", "48 65 6c", "hex")
        self.assertEqual([(h[0], h[2], h[3]) for h in hits], [(7, "hex", 1)])
        self.assertIn("[hex @1: 48 65 6c]", hits[0][4])
        self.assertEqual(self.ids(s, "48656c", "blob", True), [7])
        self.assertEqual(self.ids(s, "48 ?? 6c", "hex"), [7])
        self.assertEqual(self.ids(s, "00 ?? 65", "hex"), [3, 7])      # b"\x00se..", b"\x00He.."
        self.assertEqual(self.ids(s, "65 ?? 6c", "hex"), [])

    def test_sql_prefilter_never_drops_a_hit(self):
        s = self.open(blob_docs(self.tmp))
        cases = [("secret plan", "blob", False), ("secret plan", "blob", True),
                 ("SECRET PLAN", "ci", True), ("Secret Plan", "cs", True), ("secret plan", "sw", True),
                 ("PLAN", "ew", True), ("secret plan", "ex", True), (r"secret\s+plan", "rx", True),
                 (r"secret\s+plan", "rx", False), ("48 65 6c", "hex", False), ("48 ?? 6c", "hex", False),
                 ("00 ?? 65", "hex", False), ("48656c", "blob", True), ("2024-07", "ci", True),
                 ("id 2024", "blob", False), ("secret", "ci", False), ("f1", "ci", True),
                 ("78", "hex", False), ("x", "blob", True), ("SECRET", "ew", True),
                 (r"(?i)SECRET\s+plan", "rx", True), (r"(?i:SECRET)\s+plan", "rx", True),
                 (r"Secret\s+Plan", "rx", True), (r"id \d+-0", "rx", True)]
        orig = Session._sql_rows
        self.assertTrue(s._sql_lower)
        for sql_lower in (True, False):     # with and without lower() folding BLOB bytes in SQL
            s._sql_lower = sql_lower
            for term, mode, deep in cases:
                with mock.patch.object(Session, "_sql_rows", autospec=True, side_effect=orig) as spy:
                    via_sql = self.found(s, "docs", term, mode, deep)
                self.assertTrue(spy.called, (term, mode, deep))          # a WHERE clause was used
                with full_scan():
                    via_scan = self.found(s, "docs", term, mode, deep)
                self.assertEqual(via_sql, via_scan, (term, mode, deep, sql_lower))

    def test_regex_prefilter_keeps_matches_of_repeats_and_groups(self):
        path = os.path.join(self.tmp, "rx.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT, b BLOB)")
        c.executemany("INSERT INTO t VALUES (?, ?, ?)",
                      [(1, "say hello", None), (2, "x5y", None), (3, "a 152 b", None),
                       (4, None, b"\x00" + le("id 152"))] + [(i, "other", None) for i in range(5, 50)])
        c.commit()
        c.close()
        s = self.open(path)
        for pattern, deep, want in (("hel+o", False, [1]), (r"x(\d)y", False, [2]),
                                    (r"1(\d)2", False, [3]), (r"1(\d)2", True, [3, 4]),
                                    (r"id \d", True, [4]), (r"(?i)ID \d", True, [4])):
            got = self.found(s, "t", pattern, "rx", deep)
            self.assertEqual([h[0] for h in got], want, pattern)
            with full_scan():
                self.assertEqual(self.found(s, "t", pattern, "rx", deep), got, pattern)
        # BLOBs are narrowed by the regex literal as written, unless case may be ignored
        where, params = Matcher(r"say h\w+", "rx", True).sql_where(["b"], ["BLOB"])
        self.assertIn("instr(CAST(\"b\" AS BLOB), ?) > 0", where)
        self.assertEqual(params[-3:], [b"say h", le("say h"), be("say h")])
        for pattern in (r"(?i)say h\w+", r"(?i:say) h\w+"):
            where, _params = Matcher(pattern, "rx", True).sql_where(["b"], ["BLOB"])
            self.assertIn("(typeof(\"b\") = 'blob' AND (instr(", where)     # only ' ' is certain
            where, params = Matcher(pattern, "rx", True, sql_lower=True).sql_where(["b"], ["BLOB"])
            self.assertIn("instr(lower(\"b\"), ?) > 0", where)     # ASCII case folded, but 's'
            self.assertEqual(params[-3:], ["ay h", "a\x00y\x00 \x00h\x00", "\x00a\x00y\x00 \x00h"])

    def test_blob_prefilter_selects_by_bytes_every_match_contains(self):
        where, params = Matcher("id 2024-07", "blob", True).sql_where(["data"], ["BLOB"])
        self.assertIn("instr(CAST(\"data\" AS BLOB), ?) > 0", where)   # the case-free part
        self.assertIn(b" 2024-07", params)
        where, _params = Matcher("secret", "blob", False).sql_where(["data"], ["BLOB"])
        self.assertIn("typeof(\"data\") = 'blob'", where)       # letters only: no exact bytes known
        where, params = Matcher("Secret", "ci", True, sql_lower=True).sql_where(["data"], ["BLOB"])
        self.assertIn("instr(lower(\"data\"), ?) > 0", where)   # ASCII: any case, folded by SQLite
        self.assertEqual(params[-3:], ["secret", "s\x00e\x00c\x00r\x00e\x00t\x00",
                                       "\x00s\x00e\x00c\x00r\x00e\x00t"])
        where, _params = Matcher("über", "ci", True, sql_lower=True).sql_where(["data"], ["BLOB"])
        self.assertNotIn("lower(", where)                       # lower() folds ASCII only

    def test_lower_prefilter_is_only_used_where_it_is_exact(self):
        self.assertTrue(self.open(blob_docs(self.tmp)).matcher("x", "ci", True).sql_lower)
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertTrue(blob_lower_works(conn))
        conn.create_function("lower", 1, lambda v: v.lower() if isinstance(v, str) else v)
        self.assertFalse(blob_lower_works(conn))                # e.g. a Unicode-aware lower()
        u16 = os.path.join(self.tmp, "u16.db")
        c = sqlite3.connect(u16)
        c.execute("PRAGMA encoding='UTF-16le'")
        c.execute("CREATE TABLE t(b BLOB)")
        c.execute("INSERT INTO t VALUES (?)", (b"\x00HELLO",))
        c.commit()
        c.close()
        s = self.open(u16)
        self.assertFalse(s.matcher("x", "ci", True).sql_lower)  # lower() would transcode bytes
        self.assertEqual([h["offset"] for h in s.search("t", "hello", "blob")], [1])

    def test_hex_reads_stored_text_bytes_in_the_database_encoding(self):
        path = os.path.join(self.tmp, "u16.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA encoding='UTF-16le'")
        c.execute("CREATE TABLE t(s TEXT, b BLOB)")
        c.executemany("INSERT INTO t VALUES (?, ?)", [("AB", None), ("xyz", b"\x41\x00\x42"),
                                                       ("zAB", None)])
        c.commit()
        c.close()
        s = self.open(path)
        via_sql = self.found(s, "t", "41 00 42", "hex")
        self.assertEqual([(h[0], h[1], h[3]) for h in via_sql], [(1, "s", 0), (2, "b", 0), (3, "s", 2)])
        with full_scan():
            self.assertEqual(self.found(s, "t", "41 00 42", "hex"), via_sql)

    def test_malformed_hex_is_a_readable_error(self):
        s = self.open(blob_docs(self.tmp))
        with self.assertRaises(ValueError):
            list(s.search("docs", "4g", "hex"))
        with self.assertRaises(ValueError):
            list(s.search_tables(s.tables(), "48 6", "hex"))
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith("search-")])
        self.assertEqual(list(s.search("docs", "48 6", "blob", deep_blob=True)), [])  # text only

    def test_search_tables_passes_the_new_options(self):
        s = self.open(blob_docs(self.tmp))
        got = dict((n, h) for n, h, e in s.search_tables(["docs"], "secret plan", "ci",
                                                          deep_blob=True, workers=2))
        self.assertEqual(sorted(set(h["locator"].value for h in got["docs"]) & set(range(10))),
                         [1, 2, 3, 4, 5, 8])

    def test_db_adapter_accepts_the_hex_mode_key(self):
        db = DB()
        db.open(blob_docs(self.tmp))
        self.addCleanup(db.close)
        hits = list(db.search("docs", db.columns("docs"), "48 65 6c", "hex", 50, False, None))
        self.assertEqual([(h["column"], h["offset"]) for h in hits], [("data", 1)])
        with self.assertRaises(ValueError):
            list(db.search_tables(["docs"], "zz", "hex", 50, False, None))

    def test_cancel_during_a_deep_blob_scan(self):
        path = os.path.join(self.tmp, "many.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE big(id INTEGER PRIMARY KEY, data BLOB)")
        c.executemany("INSERT INTO big VALUES (?, ?)",
                      [(i, b"\x00" + le("needle %d" % i) + os.urandom(200)) for i in range(3000)])
        c.commit()
        c.close()
        s = self.open(path)
        calls = [0]

        def cancel():
            calls[0] += 1
            return calls[0] > 100
        hits = list(s.search("big", "needle", "ci", 100000, deep_blob=True, cancel=cancel))
        self.assertTrue(0 < len(hits) <= 100)
        with full_scan():               # the same when every row is read (no WHERE clause)
            calls[0] = 0
            hits = list(s.search("big", "needle", "blob", 100000, cancel=cancel))
            self.assertTrue(0 < len(hits) <= 100)
        self.assertEqual(list(s.search_tables(["big"], "needle", "rx", deep_blob=True,
                                              cancel=lambda: True, workers=2)), [])
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith("search-")])


class AdapterTestBase(TempDirTest):
    def open(self, path):
        db = DB()
        db.open(path)
        self.addCleanup(db.close)
        return db


class WalBytesTest(AdapterTestBase):
    def test_wal_search_reads_blobs_as_bytes(self):
        db = self.open(wal_blobs(self.tmp))
        hits = list(db.wal.search("WAL SECRET", "BLOB/Hex"))
        self.assertEqual([(h["table"], h["column"], h["encoding"], h["offset"], h["category"])
                          for h in hits], [("b", "data", "utf-16le", 2, "current")])
        self.assertIn("wal secret", hits[0]["value"])
        self.assertEqual(list(db.wal.search("wal secret", "ci")), [])      # BLOB column skipped
        self.assertEqual(len(list(db.wal.search("wal secret", "ci", deep_blob=True))), 1)
        hx = list(db.wal.search("de ad ?? ef", "hex"))
        self.assertEqual([(h["type"], h["offset"]) for h in hx], [("blob_hex", 1)])
        with self.assertRaises(ValueError):
            list(db.wal.search("de ad b", "hex"))
        # the same rules as the table search over the same rows
        for term, mode in (("WAL SECRET", "blob"), ("de ad ?? ef", "hex")):
            tbl = [(h["locator"], h["encoding"], h["offset"], h["value"])
                   for h in db.session.search("b", term, mode)]
            wal = [(h["locator"], h["encoding"], h["offset"], h["value"])
                   for h in db.wal.search(term, mode)]
            self.assertEqual(tbl, wal)


class FreelistSearchTest(AdapterTestBase):
    def test_freelist_text_records_are_searchable_with_their_place(self):
        db = self.open(fx.freelist(self.tmp))
        want = sorted(r.rowid for r in db.freed_page_records()
                      if "note number 3" in (r.values_dict().get("body") or ""))
        self.assertTrue(want)
        hits = list(db.search_freelist("NOTE NUMBER 3", "Case-Insensitive", limit=100000))
        self.assertEqual(sorted(h["rowid"] for h in hits), want)
        pager = db.session.pager
        for h in hits:
            self.assertEqual((h["source"], h["table"], h["column"], h["encoding"]),
                             ("Freelist", "notes", "body", "text"))
            self.assertIn(h["confidence"], ("high", "medium", "low"))   # the carver's scale
            self.assertTrue(h["reasons"])
            cell = [c for c in page_cells(pager, h["page"]) if c[3].offset == h["cell_offset"]]
            self.assertEqual(cell[0][1], h["rowid"])          # the cell really holds that record
            self.assertEqual(h["row"][1], h["value"])
        self.assertEqual(len(set(h["locator"] for h in hits)), len(hits))
        limited = list(db.search_freelist("note number", "ci", limit=3))
        self.assertEqual(len(set(h["locator"] for h in limited)), 3)
        self.assertEqual(list(db.search_freelist("note number", "ci", cancel=lambda: True)), [])
        self.assertEqual(list(db.search_freelist("note", "Column Name")), [])

    def test_freelist_blob_records_use_the_byte_rules(self):
        db = self.open(freed_blobs(self.tmp))
        hits = list(db.search_freelist("GONE SECRET", "BLOB/Hex", limit=100000))
        self.assertTrue(hits)
        self.assertEqual(set((h["column"], h["encoding"], h["offset"], h["type"]) for h in hits),
                         set([("data", "utf-16le", 2, "BLOB")]))
        self.assertEqual(list(db.search_freelist("gone secret", "ci")), [])   # BLOB column skipped
        self.assertEqual(len(list(db.search_freelist("gone secret", "ci", 100000, True))), len(hits))
        self.assertEqual(len(list(db.search_freelist("67 00 6f 00 6e 00", "hex", 100000))), len(hits))


def _reversing_decoder(data, max_depth=4, limit=10000):
    """Stand-in for engine.decode: BLOBs starting with 'ZZ' hold reversed text."""
    return [data[2:][::-1].decode("utf-8", "replace")] if data[:2] == b"ZZ" else []


class DecodedContentTest(TempDirTest):
    def _db(self):
        path = os.path.join(self.tmp, "dec.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, data BLOB, note TEXT)")
        c.executemany("INSERT INTO t VALUES (?, ?, ?)", [
            (1, b"ZZ" + "a hidden word here"[::-1].encode(), "n"),
            (2, b"plain hidden word", "n"),
            (3, b"ZZ" + b"nothing", "hidden word in text")])
        c.commit()
        c.close()
        return path

    def test_missing_decoder_is_an_error_not_an_empty_result(self):
        s = Session.open(self._db(), hash_evidence=False)
        self.addCleanup(s.close)
        with mock.patch.dict(sys.modules, {"engine.decode": None}):
            with self.assertRaises(DecodedSearchUnavailable) as cm:
                Matcher("x", "ci", False, decoded=True)
            self.assertIn("engine.decode", str(cm.exception))
            with self.assertRaises(DecodedSearchUnavailable):
                list(s.search("t", "hidden", "ci", decoded=True))
            with self.assertRaises(DecodedSearchUnavailable):
                list(s.search_tables(["t"], "hidden", "ci", decoded=True))
            self.assertTrue(list(s.search("t", "hidden", "ci")))       # still fine without it

    def test_decoded_strings_are_searched_and_marked(self):
        s = Session.open(self._db(), hash_evidence=False)
        self.addCleanup(s.close)
        with mock.patch("engine.search.load_decoder", return_value=_reversing_decoder):
            def found(mode, deep, decoded):
                return sorted((h["locator"].value, h["encoding"], h["offset"], h["value"])
                              for h in s.search("t", "HIDDEN WORD" if mode != "cs" else "hidden word",
                                                mode, 100, deep, decoded=decoded))
            self.assertEqual(found("ci", False, False), [(3, "text", None, "hidden word in text")])
            self.assertEqual(found("ci", False, True),
                             [(1, "decoded", None, "a hidden word here"),
                              (3, "text", None, "hidden word in text")])
            self.assertEqual([h[:2] for h in found("blob", False, True)],
                             [(1, "decoded"), (2, "utf-8"), (3, "text")])   # raw bytes first
            self.assertEqual([h[:2] for h in found("cs", True, True)],
                             [(1, "decoded"), (2, "utf-8"), (3, "text")])
            for mode, deep in (("ci", False), ("blob", True), ("rx", True)):
                via_sql = found(mode, deep, True)
                with full_scan():
                    self.assertEqual(found(mode, deep, True), via_sql, mode)

    @unittest.skipUnless(REAL_DECODER, "engine.decode is not available")
    def test_search_finds_what_the_decoder_finds(self):
        blobs = [zlib.compress(b"zlib hidden phrase " * 3), gzip.compress(b"gzip hidden phrase"),
                 base64.b64encode(b"base64 hidden phrase"),
                 plistlib.dumps({"k": "plist hidden phrase"}, fmt=plistlib.FMT_BINARY),
                 b'{"a": "json hidden phrase"}', os.urandom(64)]
        path = os.path.join(self.tmp, "real.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, data BLOB)")
        c.executemany("INSERT INTO t VALUES (?, ?)", list(enumerate(blobs, 1)))
        c.commit()
        c.close()
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        hits = dict((h["locator"].value, h) for h in s.search("t", "Hidden Phrase", "ci", 100,
                                                               deep_blob=True, decoded=True))
        for i, blob in enumerate(blobs, 1):
            raw = b"hidden phrase" in blob
            decoded = any("hidden phrase" in d.lower() for d in REAL_DECODER(blob))
            self.assertEqual(i in hits, raw or decoded, i)
            if i in hits and not raw:
                self.assertEqual(hits[i]["encoding"], "decoded")
                self.assertIn("hidden phrase", hits[i]["value"].lower())


if __name__ == "__main__":
    unittest.main()
