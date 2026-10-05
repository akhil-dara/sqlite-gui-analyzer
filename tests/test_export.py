"""The export writer (engine.export): every value type in CSV and JSON and each BLOB mode,
streaming, cancelling, the manifest (SHA-256, never replacing a file), the evidence-folder
guard and the evidence record."""
import base64
import csv
import hashlib
import json
import os
import sqlite3
import time

from tests.helpers import TempDirTest, dir_snapshot
from engine.evidence import EvidenceSet
from engine.export import (BLOB_MODES, ExportError, VALUE_ENCODING, csv_cell, evidence_record,
                           json_cell, provenance, write_manifest, write_rows)
from engine.fileformat.record import InvalidText
from engine.schema import Locator
from engine.tags import PartialBlob

BLOB = bytes(range(256)) * 3 + b"\x00\xff"
FLOATS = (0.1, 1e308, -2.5e-310, 1 / 3.0, 123456789.123456789, -0.0)


class Odd(object):
    def __str__(self):
        return "odd"


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def blob_from_csv(text):
    if text.startswith("x'"):
        return bytes.fromhex(text[2:-1])
    if text.startswith("base64:"):
        return base64.b64decode(text[len("base64:"):])
    raise AssertionError(text)


def blob_from_json(d):
    if "blob_hex" in d:
        return bytes.fromhex(d["blob_hex"])
    return base64.b64decode(d["blob_base64"])


class CellTest(TempDirTest):
    def test_csv_cells(self):
        self.assertEqual(csv_cell(None), "NULL")
        self.assertEqual(csv_cell("NULL"), "NULL")          # the documented CSV ambiguity
        self.assertEqual(csv_cell(True), "1")
        self.assertEqual(csv_cell(-(1 << 63)), str(-(1 << 63)))
        for f in FLOATS:
            self.assertEqual(float(csv_cell(f)), f)
            self.assertEqual(repr(float(csv_cell(f))), repr(f))
        self.assertEqual(csv_cell(float("inf")), "inf")
        self.assertEqual(csv_cell("=1+2 ünïcode"), "'=1+2 ünïcode")  # spreadsheet-safe
        self.assertEqual(csv_cell("=1+2 ünïcode", formulas=False), "=1+2 ünïcode")
        self.assertEqual(csv_cell(-5), "-5")                          # numbers never change
        self.assertEqual(csv_cell(InvalidText(b"ab\xffc")), "ab\\xffc")
        self.assertEqual(csv_cell(Locator("rowid", 7)), "7")
        self.assertEqual(csv_cell(Locator("pk", ("a",))), "pk='a'")
        self.assertEqual(csv_cell(Odd()), "odd")
        self.assertEqual(json_cell(Odd()), "odd")
        for mode in ("hex", "base64"):
            for v in (BLOB, bytearray(BLOB), memoryview(BLOB), b""):
                self.assertEqual(blob_from_csv(csv_cell(v, mode)), bytes(v))
        self.assertEqual(csv_cell(BLOB, "summary"), "[BLOB %d bytes, SHA-256 %s]"
                         % (len(BLOB), hashlib.sha256(BLOB).hexdigest()))

    def test_json_cells(self):
        self.assertIsNone(json_cell(None))
        self.assertEqual(json_cell("NULL"), "NULL")
        self.assertEqual(json_cell(False), 0)
        self.assertEqual(json_cell(1 << 62), 1 << 62)
        for f in FLOATS:
            back = json.loads(json.dumps(json_cell(f)))
            self.assertEqual(repr(back), repr(f))
        self.assertEqual(json_cell(float("-inf")), {"real": "-inf"})
        self.assertEqual(json_cell(float("nan")), {"real": "nan"})
        self.assertEqual(json_cell(InvalidText(b"\xc3(")), {"invalid_text_hex": "c328"})
        self.assertEqual(json_cell(Locator("ordinal", 3)), "#3")
        for mode in ("hex", "base64"):
            for v in (BLOB, bytearray(BLOB), memoryview(BLOB), b""):
                d = json.loads(json.dumps(json_cell(v, mode)))
                self.assertEqual(blob_from_json(d), bytes(v))
                self.assertEqual(d["size"], len(v))
        s = json_cell(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, "summary")
        self.assertEqual(s["size"], 38)
        self.assertEqual(len(s["sha256"]), 64)
        self.assertIsInstance(s["blob_summary"], str)
        self.assertNotIn("blob_hex", s)

    def test_partial_blob_says_so(self):
        p = PartialBlob(b"abc")
        p.size, p.sha256 = 1000, "ab" * 32
        self.assertEqual(json_cell(p)["size"], 1000)
        self.assertEqual(json_cell(p)["partial"], {"bytes_kept": 3, "sha256": "ab" * 32})
        self.assertIn("first 3 of 1000 bytes", csv_cell(p))
        self.assertIn("1000 bytes", csv_cell(p, "summary"))


class WriteRowsTest(TempDirTest):
    COLUMNS = ["id", "n", "r", "t", "b", "bad", "loc"]

    def rows(self):
        return [
            (1, None, 0.1, "NULL", BLOB, InvalidText(b"\xff\xfe"), Locator("rowid", 1)),
            (2, 42, float("inf"), "line\nbreak, \"quoted\"", b"", None, Locator("pk", (1, "x"))),
            (3, True, -0.0, "=cmd|' /C calc'!A0", bytearray(b"\x00\x01"), "ok", None),
        ]

    def info(self, **kw):
        return provenance("2.0.0", [("a.db", [{"role": "main", "path": "a.db"}])],
                          "Browse table 't' (DB, WAL applied)", scope="all rows",
                          filters="none", columns=self.COLUMNS, **kw)

    def test_every_type_both_formats_all_blob_modes(self):
        for mode in BLOB_MODES:
            for fmt in ("csv", "json"):
                path = os.path.join(self.tmp, "out_%s.%s" % (mode, fmt))
                res = write_rows(path, fmt, self.COLUMNS, iter(self.rows()), self.info(),
                                 blob_mode=mode)
                self.assertTrue(res.complete)
                self.assertEqual(res.rows, 3)
                with open(res.manifest, encoding="utf-8") as f:
                    man = json.load(f)
                self.assertEqual(man["provenance"]["blob_lossless"], mode != "summary")
                self.assertEqual(man["provenance"]["blob_mode"], mode)
                if fmt == "csv":
                    got = read_csv(path)
                    self.assertEqual(got[0], self.COLUMNS)
                    self.assertEqual(len(got), 4)
                    self.assertEqual(got[1][1], "NULL")
                    self.assertEqual(got[1][3], "NULL")
                    self.assertEqual(got[1][5], "\\xff\\xfe")
                    self.assertEqual(got[2][3], "line\nbreak, \"quoted\"")
                    self.assertEqual(got[3][3], "'=cmd|' /C calc'!A0")      # spreadsheet-safe
                    self.assertEqual(got[2][2], "inf")
                    self.assertEqual(got[3][2], "-0.0")
                    if mode != "summary":
                        self.assertEqual(blob_from_csv(got[1][4]), BLOB)
                        self.assertEqual(blob_from_csv(got[3][4]), b"\x00\x01")
                    else:
                        self.assertIn("SHA-256", got[1][4])
                else:
                    data = read_json(path)
                    self.assertEqual(data["format"], "sqlite-gui-analyzer-export")
                    self.assertEqual(data["columns"], self.COLUMNS)
                    self.assertEqual(data["end"], {"rows": 3, "complete": True})
                    self.assertNotIn("complete", data["provenance"])
                    self.assertNotIn("rows", data["provenance"])
                    self.assertEqual(data["provenance"]["value_encoding"],
                                     json.loads(json.dumps(VALUE_ENCODING)))
                    r = data["rows"]
                    self.assertIsNone(r[0][1])
                    self.assertEqual(r[0][3], "NULL")        # NULL and 'NULL' differ in JSON
                    self.assertEqual(r[0][5], {"invalid_text_hex": "fffe"})
                    self.assertEqual(r[1][2], {"real": "inf"})
                    self.assertEqual(r[1][6], "pk=(1, 'x')")
                    self.assertEqual(r[2][1], 1)
                    if mode != "summary":
                        self.assertEqual(blob_from_json(r[0][4]), BLOB)
                    else:
                        self.assertEqual(r[0][4]["size"], len(BLOB))

    def test_empty_export_is_valid(self):
        path = os.path.join(self.tmp, "e.json")
        res = write_rows(path, "json", ["a"], [], self.info())
        self.assertEqual(read_json(path)["rows"], [])
        self.assertTrue(res.complete)

    def test_streams_a_large_generator(self):
        made = {"n": 0}

        def gen():
            for i in range(200000):
                made["n"] += 1
                yield (i, "text %d" % i, i * 0.5)
        seen = []
        for fmt in ("csv", "json"):
            path = os.path.join(self.tmp, "big." + fmt)
            made["n"] = 0
            t = time.time()
            res = write_rows(path, fmt, ["i", "t", "r"], gen(), self.info(), progress=seen.append,
                             every=50000)
            self.assertLess(time.time() - t, 60)
            self.assertEqual((res.rows, res.complete, made["n"]), (200000, True, 200000))
        self.assertIn(50000, seen)
        data = read_json(os.path.join(self.tmp, "big.json"))
        self.assertEqual(len(data["rows"]), 200000)
        self.assertEqual(data["rows"][-1], [199999, "text 199999", 99999.5])

    def test_cancel_gives_valid_incomplete_files(self):
        for fmt in ("json", "csv"):
            path = os.path.join(self.tmp, "c." + fmt)
            count = {"n": 0}

            def gen():
                for i in range(100000):
                    count["n"] = i + 1
                    yield (i,)
            res = write_rows(path, fmt, ["i"], gen(), self.info(),
                             cancel=lambda: count["n"] >= 1000)
            self.assertFalse(res.complete)
            self.assertEqual(res.rows, 1000)
            self.assertEqual(res.stopped, "by the user after 1,000 rows")
            with open(res.manifest, encoding="utf-8") as f:
                man = json.load(f)
            self.assertFalse(man["provenance"]["complete"])
            self.assertEqual(man["provenance"]["rows"], 1000)
            if fmt == "json":
                data = read_json(path)
                self.assertEqual(len(data["rows"]), 1000)
                self.assertEqual(data["end"], {"rows": 1000, "complete": False,
                                               "stopped": "by the user after 1,000 rows"})
            else:
                self.assertEqual(len(read_csv(path)), 1001)

    def test_error_reading_rows_ends_the_export_marked(self):
        def gen():
            yield (1,)
            raise sqlite3.DatabaseError("disk image is malformed")
        path = os.path.join(self.tmp, "err.json")
        res = write_rows(path, "json", ["i"], gen(), self.info())
        self.assertFalse(res.complete)
        self.assertIn("malformed", read_json(path)["end"]["stopped"])

    def test_manifest_hash_and_never_replaces(self):
        path = os.path.join(self.tmp, "m.csv")
        r1 = write_rows(path, "csv", self.COLUMNS, self.rows(), self.info())
        self.assertEqual(r1.manifest, path + ".manifest.json")
        with open(path, "rb") as f:
            body = f.read()
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(r1.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(r1.size, len(body))
        with open(r1.manifest, encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(man["format"], "sqlite-gui-analyzer-export-manifest")
        self.assertEqual(man["files"], [{"path": "m.csv", "size": len(body),
                                         "sha256": r1.sha256}])
        self.assertEqual(man["provenance"]["tool"]["name"], "SQLite GUI Analyzer")
        self.assertEqual(man["provenance"]["source"], "Browse table 't' (DB, WAL applied)")
        self.assertTrue(man["provenance"]["complete"])
        first = dir_snapshot(self.tmp)[os.path.basename(r1.manifest)]
        r2 = write_rows(path, "csv", self.COLUMNS, self.rows(), self.info())
        self.assertEqual(r2.manifest, os.path.join(self.tmp, "m.csv_2.manifest.json"))
        r3 = write_rows(path, "csv", self.COLUMNS, self.rows(), self.info())
        self.assertEqual(r3.manifest, os.path.join(self.tmp, "m.csv_3.manifest.json"))
        self.assertEqual(dir_snapshot(self.tmp)[os.path.basename(r1.manifest)], first)

    def test_folder_manifest(self):
        folder = os.path.join(self.tmp, "blobs")
        os.makedirs(folder)
        a = os.path.join(folder, "a.bin")
        with open(a, "wb") as f:
            f.write(b"abc")
        m1 = write_manifest(folder, self.info(), [a])
        self.assertEqual(m1, os.path.join(folder, "export_manifest.json"))
        with open(m1, encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(man["files"], [{"path": "a.bin", "size": 3,
                                         "sha256": hashlib.sha256(b"abc").hexdigest()}])
        m2 = write_manifest(folder, self.info(), [a], complete=False)
        self.assertEqual(m2, os.path.join(folder, "export_manifest_2.json"))

    def test_protected_path_refused_before_any_file(self):
        evidence = os.path.join(self.tmp, "evidence")
        os.makedirs(evidence)
        db = os.path.join(evidence, "x.db")
        sqlite3.connect(db).close()
        ev = EvidenceSet(db)
        before = dir_snapshot(evidence)

        def gen():
            raise AssertionError("rows must not be read")
            yield ()
        for target in (os.path.join(evidence, "out.csv"), os.path.join(evidence, "s", "o.json")):
            with self.assertRaises(ExportError):
                write_rows(target, "json" if target.endswith("json") else "csv", ["a"], gen(),
                           self.info(), protected=ev.is_protected)
        with self.assertRaises(ExportError):
            write_manifest(evidence, self.info(), [], protected=ev.is_protected)
        self.assertEqual(dir_snapshot(evidence), before)
        self.assertEqual(os.listdir(evidence), ["x.db"])
        with self.assertRaises(ExportError):
            write_rows(os.path.join(self.tmp, "x.txt"), "xml", ["a"], [], self.info())
        with self.assertRaises(ExportError):
            write_rows(os.path.join(self.tmp, "x.csv"), "csv", ["a"], [], self.info(),
                       blob_mode="raw")


class EvidenceRecordTest(TempDirTest):
    def make(self, size=3 << 20):
        p = os.path.join(self.tmp, "ev.db")
        with open(p, "wb") as f:
            f.write(os.urandom(size))
        with open(p + "-wal", "wb") as f:
            f.write(b"w" * 1000)
        return p

    def test_computes_hashes_without_background_thread(self):
        p = self.make()
        before = dir_snapshot(self.tmp)
        seen = []
        rec = evidence_record(EvidenceSet(p), progress=lambda a, b: seen.append((a, b)))
        with open(p, "rb") as f:
            self.assertEqual(rec[0]["sha256"], hashlib.sha256(f.read()).hexdigest())
        self.assertEqual([r["role"] for r in rec], ["main", "wal"])
        self.assertTrue(all(len(r["sha256"]) == 64 for r in rec))
        self.assertRegex(rec[0]["mtime_utc"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC$")
        self.assertEqual(seen[-1], (rec[0]["size"] + 1000,) * 2)
        self.assertEqual(dir_snapshot(self.tmp), before)

    def test_waits_for_background_hashing(self):
        ev = EvidenceSet(self.make())
        ev.start_hashing()
        rec = evidence_record(ev)
        self.assertTrue(ev.hashing_done)
        self.assertEqual([r["sha256"] for r in rec],
                         [fp.sha256 for fp in ev.fingerprints.values()])

    def test_without_wait_and_cancel(self):
        ev = EvidenceSet(self.make())
        rec = evidence_record(ev, wait=False)
        self.assertEqual([r["note"] for r in rec], ["not computed yet"] * 2)
        rec = evidence_record(ev, cancel=lambda: True)
        self.assertEqual([(r["sha256"], r["note"]) for r in rec],
                         [(None, "not computed: stopped")] * 2)
        ev.cancel_hashing()             # a background thread that never finishes on its own
        ev.start_hashing()
        rec = evidence_record(ev, cancel=lambda: True)
        self.assertTrue(all(r["sha256"] is None for r in rec))
        self.assertIsNone(evidence_record(None) or None)

    def test_provenance_fields(self):
        ev = EvidenceSet(self.make(10))
        info = provenance("2.0.0", [("ev.db", evidence_record(ev))], "SQL query",
                          extra={"sql": "SELECT 1"})
        for key in ("tool", "exported_utc", "python", "sqlite", "databases", "source", "scope",
                    "filters", "rows", "blob_mode", "blob_lossless", "value_encoding", "extra",
                    "complete"):
            self.assertIn(key, info)
        self.assertEqual(info["databases"][0]["label"], "ev.db")
        self.assertEqual(info["extra"], {"sql": "SELECT 1"})
        json.dumps(info)
