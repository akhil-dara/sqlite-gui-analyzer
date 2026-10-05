"""Audit findings on crafted headers, report files (HTML / CSV / JSON), refusal to write into
the evidence folder, hostile pages, and the DB adapter methods."""
import csv
import json
import os
import sqlite3
import struct
import unittest

from tests.helpers import TempDirTest, dir_snapshot
from tests.test_html_report import check_page
from engine.html_report import read_rows
from tests.fixtures import forensic_fixtures as ff
from tests.fixtures import make_fixtures as fx
from engine.forensics import ReportError
from engine.schema import Locator
from engine.session import Session


class Base(TempDirTest):
    def open(self, path):
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        return s

    def codes(self, path):
        return dict((f.code, f) for f in self.open(path).forensics.audit())


class AuditTest(Base):
    def test_clean_database(self):
        found = self.codes(ff.freeblock_rows(self.tmp))
        self.assertIn("HEADER", found)
        self.assertEqual(found["APPLICATION_ID"].details["application_id"], 0)
        for bad in ("RESERVED_BYTES", "PAYLOAD_FRACTIONS", "FREELIST_COUNT", "HEADER_PAGE_COUNT",
                    "BTREE_ANOMALY", "ROOT_PAGE_SHARED"):
            self.assertNotIn(bad, found)
        self.assertIn("FREE_SPACE", found)
        for f in found.values():
            self.assertIn(f.level, ("info", "warning", "error"))
            json.dumps(f.as_dict())

    def test_reserved_bytes(self):
        found = self.codes(ff.crafted_header(self.tmp, "reserved.db", [(20, b"\x10")]))
        self.assertEqual(found["RESERVED_BYTES"].details["reserved_bytes"], 16)

    def test_payload_fractions(self):
        found = self.codes(ff.crafted_header(self.tmp, "fractions.db", [(21, b"\x00\x00\x00")]))
        self.assertEqual(found["PAYLOAD_FRACTIONS"].level, "error")

    def test_freelist_count_mismatch(self):
        found = self.codes(ff.crafted_header(self.tmp, "flcount.db", [(36, struct.pack(">I", 99))]))
        self.assertEqual(found["FREELIST_COUNT"].details["header"], 99)

    def test_data_beyond_header_page_count(self):
        found = self.codes(ff.crafted_header(self.tmp, "trailing.db", append=b"\xAB" * 2048))
        self.assertEqual(found["HEADER_PAGE_COUNT"].level, "warning")
        self.assertEqual(found["HEADER_PAGE_COUNT"].details["extra_bytes"], 2048)

    def test_user_version_and_application_id(self):
        path = ff.crafted_header(self.tmp, "ids.db", [(60, struct.pack(">I", 7)),
                                                       (68, struct.pack(">I", 0x0F055112))])
        found = self.codes(path)
        self.assertEqual(found["APPLICATION_ID"].details,
                         {"application_id": 0x0F055112, "user_version": 7})

    def test_shared_root_page(self):
        path = ff.crafted_header(self.tmp, "shared.db")
        c = sqlite3.connect(path)
        root_a = c.execute("SELECT rootpage FROM sqlite_master WHERE name='a'").fetchone()[0]
        root_b = c.execute("SELECT rootpage FROM sqlite_master WHERE name='b'").fetchone()[0]
        c.close()
        with open(path, "rb") as f:
            data = bytearray(f.read())
        # sqlite_master rows are on page 1; patch b's 1-byte rootpage value to a's
        needle = b"CREATE TABLE b("
        at = data.find(needle, 0, 1024)
        pos = data.rfind(bytes([root_b]), 0, at)
        data[pos] = root_a
        with open(path, "wb") as f:
            f.write(bytes(data))
        found = self.codes(path)
        self.assertIn("ROOT_PAGE_SHARED", found)

    def test_wal_findings(self):
        found = self.codes(fx.wal_states(self.tmp))
        self.assertIn("WAL", found)
        self.assertIn("WAL_STALE_FRAMES", found)
        self.assertIn("WAL_UNCOMMITTED_FRAMES", found)
        self.assertEqual(found["WAL"].details["states"]["stale"],
                         found["WAL_STALE_FRAMES"].details["frames"])

    def test_dropped_schema_is_a_finding(self):
        found = self.codes(ff.dropped_table(self.tmp))
        self.assertIn("table secrets (dropped)", found["DROPPED_SCHEMA"].details["objects"])


class ReportTest(Base):
    HOSTILE = "<script>alert('x')</script> & \"quoted\" =cmd"

    def evidence_with_hostile_text(self):
        ev = os.path.join(self.tmp, "evidence")
        os.makedirs(ev)
        path = os.path.join(ev, "h.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA page_size=1024")
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE msg(id INTEGER PRIMARY KEY, body TEXT, extra TEXT)")
        c.executemany("INSERT INTO msg VALUES (?,?,?)",
                      [(i, "%s #%d" % (self.HOSTILE, i), "e%d" % i) for i in range(1, 40)])
        c.commit()
        c.execute("DELETE FROM msg WHERE id IN (3, 9, 21)")
        c.commit()
        c.close()
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        return path, out

    def test_all_three_formats(self):
        path, out = self.evidence_with_hostile_text()
        ev_before = dir_snapshot(os.path.dirname(path))
        s = self.open(path)
        fx_ = s.forensics
        res = fx_.carve()
        self.assertTrue(any(self.HOSTILE in str(r.values[1]) for r in res))
        hist = [fx_.history_summary("msg")]
        for fmt in ("html", "csv", "json"):
            target = os.path.join(out, "report." + fmt)
            self.assertEqual(fx_.write_report(target, fmt, "9.9", records=res, history=hist), target)
            self.assertTrue(os.path.getsize(target) > 0)
        with open(os.path.join(out, "report.html"), encoding="utf-8") as f:
            page = f.read()
        check_page(self, page)                  # the report design: one script, ours
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", page)
        recovered = [t for t in read_rows(page).values() if t["name"] == "Recovered records"]
        self.assertEqual(len(recovered[0]["rows"]), len(res))
        self.assertTrue(any(self.HOSTILE in str(r[10]) for r in recovered[0]["rows"]))
        self.assertIn("9.9", page)
        with open(os.path.join(out, "report.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["tool"]["version"], "9.9")
        self.assertEqual(data["database"]["open_mode"], s.mode)
        self.assertEqual(len(data["records"]), len(res))
        self.assertTrue(any(self.HOSTILE in str(r["values"][1]) for r in data["records"]))
        main = [e for e in data["evidence"] if e["role"] == "main"][0]
        self.assertEqual(len(main["sha256"]), 64)
        self.assertTrue(data["findings"])
        self.assertEqual(data["wal_history"][0]["table"], "msg")
        with open(os.path.join(out, "report.csv"), encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        sections = set(r[0] for r in rows)
        self.assertTrue({"meta", "evidence", "finding", "record"} <= sections)
        self.assertEqual(sum(1 for r in rows if r[0] == "record"), len(res) + 1)
        for r in rows:
            for cell in r[1:]:
                self.assertFalse(cell.startswith(("=", "+", "@")), cell)
        self.assertEqual(dir_snapshot(os.path.dirname(path)), ev_before)

    def test_refuses_the_evidence_folder(self):
        path, _out = self.evidence_with_hostile_text()
        s = self.open(path)
        ev = os.path.dirname(path)
        for target in (os.path.join(ev, "r.html"), os.path.join(ev, "sub", "r.json"), ev):
            with self.assertRaises(ReportError):
                s.forensics.write_report(target, "html", "1.0")
        self.assertEqual(sorted(os.listdir(ev)), ["h.db"])

    def test_unknown_format(self):
        path, out = self.evidence_with_hostile_text()
        s = self.open(path)
        with self.assertRaises(ReportError):
            s.forensics.write_report(os.path.join(out, "r.txt"), "txt", "1.0")


class HostileTest(Base):
    def test_hostile_pages_never_raise(self):
        ev = os.path.join(self.tmp, "ev")
        os.makedirs(ev)
        for seed in (1, 2, 3, 4):
            path = ff.hostile(ev, seed=seed)
            try:
                s = Session.open(path, hash_evidence=False)
            except Exception:
                continue            # not openable at all: nothing to test for this seed
            try:
                fx_ = s.forensics
                res = fx_.carve(time_limit=60)
                self.assertEqual(res.stats["errors"], 0)
                fx_.dropped_schema()
                fx_.audit()
                for t in s.tables():
                    fx_.history_summary(t)
                    fx_.row_history(t, Locator("rowid", 3))
                fx_.journal()
                out = os.path.join(self.tmp, "out%d" % seed)
                os.makedirs(out)
                fx_.write_report(os.path.join(out, "r.json"), "json", "1.0", records=res)
            finally:
                s.close()

    def test_truncated_and_empty_files(self):
        path = ff.deleted_rows(self.tmp)
        with open(path, "rb") as f:
            data = f.read()
        cut = os.path.join(self.tmp, "cut.db")
        with open(cut, "wb") as f:
            f.write(data[:len(data) // 2 + 100])
        s = self.open(cut)
        res = s.forensics.carve()
        self.assertEqual(res.stats["errors"], 0)
        s.forensics.audit()
        s.forensics.dropped_schema()


class AdapterTest(Base):
    def test_db_adapter_methods(self):
        from database import DB
        ev = os.path.join(self.tmp, "ev")
        os.makedirs(ev)
        db = DB()
        db.open(ff.wal_history(ev))
        self.addCleanup(db.close)
        recs = db.carve_records()
        rows = db.record_rows(recs)
        self.assertEqual(len(rows), len(recs))
        self.assertTrue(all("where" in r and "values" in r for r in rows))
        h = db.row_history("acct", 5)
        self.assertTrue(h.versions)
        self.assertTrue(db.history_summary("acct").multi_version)
        self.assertEqual(db.dropped_schema(), [])
        self.assertIsNone(db.journal_info())
        self.assertTrue(db.audit_findings())
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        target = db.write_forensic_report(os.path.join(out, "r.html"), "html", records=recs)
        self.assertTrue(os.path.isfile(target))
        with self.assertRaises(ReportError):
            db.write_forensic_report(os.path.join(ev, "r.html"), "html")


if __name__ == "__main__":
    unittest.main()
