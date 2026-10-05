"""Regression tests for the 2026-10-02 full security audit (round 4).

Covers the confirmed findings: TSV clipboard formula injection, tag-export raw
fields, HTML partial-file cleanup, the WAL page_count clamp, the activity-log
tail read, and the spreadsheet_safe manifest scoping.
"""
import csv
import json
import os
import sqlite3
import unittest
from unittest import mock

from tests.helpers import TempDirTest
from engine import export as ex
from engine.activity import ActivityLog
from engine.tag_export import export_csv

FORMULAS = ["=1+2", "+cmd", "-x", "@SUM(A1)", "\tx", "\rx", "=cmd|'/c calc'!A0"]


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


class TagExportFormulaTest(TempDirTest):
    def _entry(self):
        from engine.schema import Locator
        from engine.tags import entry_from_db_row
        return entry_from_db_row("t", Locator("rowid", 1), ["a"], ["=1+2"])

    def test_export_info_wraps_formula_like_fields(self):
        e = self._entry()
        info = {"tool": {"name": "T", "version": "1"},
                "exported_utc": "=2+2",
                "database": "=evil.db",
                "scope": "s",
                "evidence": [{"role": "=role", "path": "=path.db", "size": 1,
                              "mtime_ns": 2, "sha256": "ab"}]}
        res = export_csv(self.tmp, [e], {}, info)
        with open(res["info"], encoding="utf-8-sig", newline="") as f:
            rows = {r[0]: r[1] for r in csv.reader(f) if len(r) == 2}
        self.assertEqual(rows["exported_utc"], "'=2+2")
        self.assertEqual(rows["database"], "'=evil.db")
        ev_rows = [r for r in read_csv(res["info"]) if len(r) == 6 and r[0] != "file"]
        self.assertTrue(ev_rows)
        self.assertEqual(ev_rows[0][0], "'=role")
        self.assertEqual(ev_rows[0][1], "'=path.db")

    def test_index_csv_file_column_is_formula_safe(self):
        from engine import tag_export as te
        e = self._entry()
        info = {"tool": {"name": "T", "version": "1"}, "exported_utc": "u",
                "database": "=weird.db", "scope": "s", "evidence": []}
        with mock.patch.object(te.os.path, "basename", return_value="=evil.db"):
            res = export_csv(self.tmp, [e], {}, info)
        rows = read_csv(res["index"])
        header = rows[0]
        file_col = header.index("File")
        self.assertEqual(rows[1][file_col], "'=evil.db")


class HtmlPartialCleanupTest(TempDirTest):
    def info(self):
        return ex.provenance("1", [], "t", "s", "", ["a"])

    def test_drop_html_partial_removes_parts(self):
        from engine.export import _drop_html_partial
        main = os.path.join(self.tmp, "r.html")
        part1 = os.path.join(self.tmp, "r_part002.html")
        part2 = os.path.join(self.tmp, "r_part003_2.html")
        other = os.path.join(self.tmp, "other_part002.html")
        for p in (main, part1, part2, other):
            with open(p, "w") as f:
                f.write("x")
        note = _drop_html_partial(main)
        self.assertFalse(os.path.exists(main))
        self.assertFalse(os.path.exists(part1))
        self.assertFalse(os.path.exists(part2))
        self.assertTrue(os.path.exists(other))  # different stem: untouched
        self.assertIn("part file", note)

    def test_failed_html_export_leaves_no_partial(self):
        # a part file the failed export wrote is removed; one an earlier export left next
        # to it (same name pattern) is somebody's report and stays
        from engine import html_report
        path = os.path.join(self.tmp, "fail.html")
        earlier = os.path.join(self.tmp, "fail_part002.html")
        with open(earlier, "w") as f:
            f.write("an earlier report's part")
        written = os.path.join(self.tmp, "fail_part003.html")

        def write(*_a, **_k):
            with open(written, "w") as f:
                f.write("x")
            raise OSError(28, "No space left on device")
        with mock.patch.object(html_report.Report, "write", side_effect=write):
            with self.assertRaises(ex.ExportError) as cm:
                ex.write_rows(path, "html", ["a"], [["ok"]], self.info())
        self.assertIn("removed", str(cm.exception))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(written))
        self.assertTrue(os.path.exists(earlier))
        self.assertFalse(os.path.exists(path + ".manifest.json"))


class SpreadsheetSafeScopeTest(TempDirTest):
    def test_html_manifest_does_not_claim_spreadsheet_safe(self):
        info = ex.provenance("1", [], "t", "s", "", ["a"])
        res = ex.write_rows(os.path.join(self.tmp, "r.html"), "html",
                            ["a"], [["=1+2"]], info)
        with open(res.manifest, encoding="utf-8") as f:
            prov = json.load(f)["provenance"]
        self.assertIs(prov["spreadsheet_safe"], False)

    def test_csv_manifest_still_claims_spreadsheet_safe(self):
        info = ex.provenance("1", [], "t", "s", "", ["a"])
        res = ex.write_rows(os.path.join(self.tmp, "r.csv"), "csv",
                            ["a"], [["=1+2"]], info)
        with open(res.manifest, encoding="utf-8") as f:
            prov = json.load(f)["provenance"]
        self.assertIs(prov["spreadsheet_safe"], True)


class ActivityTailTest(TempDirTest):
    def test_entries_limit_reads_the_tail(self):
        log = ActivityLog([os.path.join(self.tmp, "ev.db")], directory=self.tmp)
        for i in range(5000):
            self.assertTrue(log.log("note", text="entry %d" % i))
        got = log.entries(limit=100)
        self.assertEqual(len(got), 100)
        self.assertEqual(got[0]["text"], "entry 4900")
        self.assertEqual(got[-1]["text"], "entry 4999")
        # a torn final line (no trailing newline) is still read
        with open(log.path, "ab") as f:
            f.write(b'{"utc": "t", "kind": "n", "text": "torn"}')
        got = log.entries(limit=2)
        self.assertEqual([e["text"] for e in got], ["entry 4999", "torn"])


class PagerClampTest(TempDirTest):
    def test_wal_page_no_cannot_inflate_page_count(self):
        # A WAL whose frame declares page_no/db_size = 2**32-1 must not inflate the
        # pager's page_count beyond the bytes the files hold.
        from engine.fileformat.pager import Pager
        db = os.path.join(self.tmp, "x.db")
        c = sqlite3.connect(db)
        c.execute("PRAGMA page_size=4096")
        c.execute("CREATE TABLE t(a)")
        c.commit()
        c.close()
        main_size = os.path.getsize(db)

        class FakeWal(object):
            page_size = 4096
            overlay = {0xFFFFFFFF: 0}
            db_size_pages = 0xFFFFFFFF
            frames_total = 2

        issues = []
        class Issues(object):
            def add(self, *a):
                issues.append(a)
        pager = Pager(db, wal=FakeWal(), issues=Issues())
        try:
            # bytes-backed: main pages + frames, plus the 1% slack
            byte_backed = -(-main_size // 4096) + 2
            allowed = byte_backed + max(16, byte_backed // 100)
            self.assertLessEqual(pager.page_count, allowed)
            self.assertTrue(any(i[0] == "page_count_clamped" for i in issues))
        finally:
            pager.close()


class TsvFormulaTest(TempDirTest):
    def setUp(self):
        super(TsvFormulaTest, self).setUp()
        try:
            import tkinter as tk
            from tests.helpers import tk_root
            self.root = tk_root(self)
        except Exception as e:
            raise unittest.SkipTest("no display: %s" % e)

    def test_tsv_copy_is_formula_safe(self):
        from browse_sources import ListSource
        from grid import DataGrid
        rows = [([i, "=cmd|'/c calc'!A0", "+2", 7, "-x"], set()) for i in range(3)]
        src = ListSource(["n", "f1", "f2", "num", "f3"], rows)
        g = DataGrid(self.root)
        g.pack()
        try:
            g.set_source(src)
            g.set_current_cell(0, 0)
            g.set_current_cell(2, 4, extend=True)
            tsv = g.rows_copy_text("tsv").splitlines()
            cells = tsv[1].split("\t")
            self.assertEqual(cells[1], "'=cmd|'/c calc'!A0")
            self.assertEqual(cells[2], "'+2")
            self.assertEqual(cells[3], "7")      # numbers untouched
            self.assertEqual(cells[4], "'-x")
            self.assertEqual(tsv[0].split("\t")[1], "f1")
        finally:
            g.destroy()


if __name__ == "__main__":
    unittest.main()
