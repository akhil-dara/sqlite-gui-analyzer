"""Every CSV the tool writes: text starting with = + - @ TAB CR gets a ' (one rule, on by
default, stated in the manifest), numbers never change, NUL is written as \\x00 (so Python
3.8-3.10 can write it), and a write that fails leaves no partial file behind."""
import csv
import io
import json
import os
import unittest
from unittest import mock

from tests.helpers import TempDirTest
from tests.test_html_report import _NODE, run_node
from engine import csvcells as cc
from engine import export as ex
from engine import timeline as tl
from engine.fileformat.record import InvalidText
from engine.forensics import report as fr
from engine.html_report import encode_cell, script_json
from engine.html_report_assets import VALUES_JS
from engine.schema import Locator
from engine.tag_export import export_csv, export_info
from engine.tags import entry_from_db_row

FORMULAS = ["=1+2", "+1", "-1+x", "@SUM(A1)", "\tx", "\rx", "=cmd|' /C calc'!A0"]
PLAIN = ["plain", "a=b", "", " lead", "x-1"]


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


class CellRuleTest(unittest.TestCase):
    def test_formula_rule(self):
        for s in FORMULAS:
            self.assertEqual(cc.formula_safe(s), "'" + s)
            self.assertEqual(cc.csv_text(s), "'" + s)
            self.assertEqual(cc.csv_text(s, formulas=False), s)
        for s in PLAIN:
            self.assertEqual(cc.csv_text(s), s)

    def test_nul(self):
        self.assertEqual(cc.csv_text("a\x00b\x00"), "a\\x00b\\x00")
        self.assertEqual(cc.csv_text("\x00=x"), "\\x00=x")
        buf = io.StringIO()
        w = cc.csv_writer(buf)
        w.writerow(["a\x00b", 5, -3, "=x", None])         # never csv.Error, even on 3.8-3.10
        self.assertEqual(buf.getvalue(), "a\\x00b,5,-3,'=x,\r\n")

    def test_csv_error_becomes_oserror(self):
        buf = io.StringIO()
        w = cc.csv_writer(buf, quoting=csv.QUOTE_NONE, escapechar=None)
        with self.assertRaises(OSError):
            w.writerow(['needs "quoting", here'])

    def test_one_rule_everywhere(self):
        from engine import tag_export
        self.assertIs(tag_export.FORMULA_PREFIXES, cc.FORMULA_PREFIXES)
        for s in FORMULAS + PLAIN:
            self.assertEqual(tag_export._csv_text(s), cc.csv_text(s))
            self.assertEqual(ex.csv_cell(s), cc.csv_text(s))


class _Bad(object):
    """A value whose text cannot be made: the export fails in the middle of a row."""

    def __init__(self, exc):
        self.exc = exc

    def __str__(self):
        raise self.exc


class ExportCsvTest(TempDirTest):
    COLUMNS = ["=col", "n", "t"]

    def info(self, **kw):
        return ex.provenance("9.9.9", [], "test", columns=self.COLUMNS, **kw)

    def test_default_is_spreadsheet_safe_and_stated(self):
        path = os.path.join(self.tmp, "out.csv")
        rows = [[s, -5, "a\x00b"] for s in FORMULAS + PLAIN] + [[1.5, -2.25, InvalidText(b"-\xff")]]
        res = ex.write_rows(path, "csv", self.COLUMNS, rows, self.info())
        got = read_csv(path)
        self.assertEqual(got[0][0], "'=col")
        for s, r in zip(FORMULAS + PLAIN, got[1:]):
            want = "'" + s if s in FORMULAS else s
            # the csv module reads a lone CR inside a quoted field back as written
            self.assertEqual(r[0], want, s)
            self.assertEqual(r[1], "-5")                     # a number is never changed
            self.assertEqual(r[2], "a\\x00b")
        self.assertEqual(got[-1], ["1.5", "-2.25", "'-\\xff"])
        with open(res.manifest, encoding="utf-8") as f:
            prov = json.load(f)["provenance"]
        self.assertIs(prov["spreadsheet_safe"], True)
        text = prov["value_encoding"]["csv"]["TEXT"]
        self.assertIn("spreadsheet-safe", text)
        self.assertIn("\\x00", text)

    def test_option_off_writes_text_as_it_is(self):
        path = os.path.join(self.tmp, "raw.csv")
        res = ex.write_rows(path, "csv", self.COLUMNS, [["=1+2", 1, "\x00"]],
                            self.info(spreadsheet_safe=False))
        got = read_csv(path)
        self.assertEqual(got, [["=col", "n", "t"], ["=1+2", "1", "\\x00"]])
        with open(res.manifest, encoding="utf-8") as f:
            prov = json.load(f)["provenance"]
        self.assertIs(prov["spreadsheet_safe"], False)
        self.assertIn("no spreadsheet formula escaping", prov["value_encoding"]["csv"]["TEXT"])

    def test_failed_write_removes_the_partial_file(self):
        for exc in (csv.Error("need to escape"), OSError(28, "No space left on device")):
            path = os.path.join(self.tmp, "fail.csv")
            rows = [["ok", 1, "x"]] * 50 + [["x", 1, _Bad(exc)]]
            with self.assertRaises(ex.ExportError) as cm:
                ex.write_rows(path, "csv", self.COLUMNS, rows, self.info())
            self.assertIn("removed", str(cm.exception))
            self.assertFalse(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".manifest.json"))

    def test_failed_open_keeps_an_existing_file(self):
        path = os.path.join(self.tmp, "keep.csv")
        with open(path, "w") as f:
            f.write("mine")
        with mock.patch("builtins.open", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(ex.ExportError):
                ex.write_rows(path, "csv", self.COLUMNS, [], self.info())
        with open(path) as f:
            self.assertEqual(f.read(), "mine")


class OtherWritersTest(TempDirTest):
    def test_tagged_rows_csv(self):
        db = os.path.join(self.tmp, "x.db")
        e = entry_from_db_row("t", Locator("rowid", 1), ["a", "b", "c"], ["=1+2", "a\x00b", -7])
        out = os.path.join(self.tmp, "tagged")
        with mock.patch("engine.tags.data_dir", return_value=os.path.join(self.tmp, "data")):
            res = export_csv(out, [e], [], export_info("9", [], db, "s"))
        rows = read_csv(res["tables"]["t"])
        self.assertEqual(rows[1][6:], ["'=1+2", "a\\x00b", "-7"])

    def test_timeline_csv(self):
        path = os.path.join(self.tmp, "tl.csv")
        evs = [_event("=HYPERLINK(\"x\")", "-1"), _event("a\x00b", -1)]
        n = tl.export_events(path, "csv", evs)
        self.assertEqual(n, 2)
        rows = read_csv(path)
        head = rows[0]
        self.assertEqual(rows[1][head.index("description")], "'=HYPERLINK(\"x\")")
        self.assertEqual(rows[1][head.index("raw")], "'-1")        # text, not a number
        self.assertEqual(rows[2][head.index("description")], "a\\x00b")
        self.assertEqual(rows[2][head.index("raw")], "-1")         # a number stays

    def test_forensic_report_csv(self):
        data = {"title": "=t", "tool": {"name": "n", "version": "v", "python": "p",
                                        "sqlite": "s"},
                "generated_utc": "g", "database": {"path": "@x"}, "evidence": [],
                "findings": [{"level": "info", "code": "c", "message": "a\x00b",
                              "details": {"k": "v"}}],
                "records": [], "wal_history": [], "dropped_schema": [], "carve": {"n": -3}}
        rows = list(csv.reader(io.StringIO(fr.to_csv(data))))
        flat = [c for r in rows for c in r]
        self.assertIn("'=t", flat)
        self.assertIn("'@x", flat)
        self.assertIn("a\\x00b", flat)
        self.assertIn("-3", flat)


def _event(description, raw):
    from datetime import datetime
    return tl.Event(datetime(2024, 1, 1), "t", "c", "unix_s", None, "1", description, "DB", raw)


@unittest.skipUnless(_NODE, "node is not installed")
class ReportCsvDownloadTest(unittest.TestCase):
    def test_in_browser_csv_matches_the_export_rule(self):
        values = FORMULAS + PLAIN + ["a\x00b", -5, 2.5, 2 ** 60, None, InvalidText(b"=\xff")]
        enc = [encode_cell(v, "hex") for v in values]
        prog = VALUES_JS + "\nvar V=%s;process.stdout.write(JSON.stringify(V.map(csvValue)));" \
            % script_json(enc).replace("\\u003c", "<")
        out = run_node(prog)

        def quote(s):
            return '"%s"' % s.replace('"', '""') if any(c in s for c in '",\r\n') or \
                s[:1].isspace() or s[-1:].isspace() else s
        for v, got in zip(values, out):
            self.assertEqual(got, quote(ex.csv_cell(v)), repr(v))
