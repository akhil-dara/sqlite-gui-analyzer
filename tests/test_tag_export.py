"""Exports of tagged rows (engine.tag_export): the HTML report escapes everything and embeds
small images, the CSV folder never replaces a file and never writes into the evidence folder,
and a JSON export loads back into a tag store."""
import csv
import hashlib
import json
import os
import plistlib
import re
import struct
import zlib
from unittest import mock

from tests.helpers import TempDirTest, dir_snapshot
from tests.test_html_report import check_page
from engine.html_report import read_rows
from tests.fixtures import make_fixtures as fx
from engine import tags as T
from engine.evidence import EvidenceSet
from engine.schema import Locator
from engine.tag_export import (TagExportError, evidence_files, export_csv, export_html,
                               export_info, export_json, html_report)
from engine.tags import (PartialBlob, TagDef, TagStore, entry_from_db_row, entry_from_freelist,
                         entry_from_wal_record, inside)

HOSTILE = "<script>alert(1)</script>"


def tiny_png():
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


class ExportTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        patcher = mock.patch.dict(os.environ, {T.DATA_DIR_ENV: os.path.join(self.tmp, "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.evidence = os.path.join(self.tmp, "evidence")
        os.makedirs(self.evidence)
        self.db = fx.freelist(self.evidence)
        self.ev = EvidenceSet(self.db)
        self.protected = self.ev.is_protected
        self.png = tiny_png()
        self.plist = plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY)
        self.big = bytes(range(256)) * 4200                          # > 1 MiB
        self.store = TagStore(self.db)
        s = self.store
        s.defs.append(TagDef("<b>odd</b>", "red;background:url(http://x.test/a)"))
        self.hostile = entry_from_db_row(
            "<img src=x onerror=alert(1)>", Locator("rowid", 1), ["id", HOSTILE, "photo"],
            [1, HOSTILE, self.png])
        self.formula = entry_from_db_row("notes", Locator("rowid", 2), ["id", "body", "data"],
                                         [2, "=cmd|' /C calc'!A0", self.plist])
        self.large = entry_from_db_row("notes", Locator("rowid", 3), ["id", "body", "data"],
                                       [3, "line one\nline two", self.big])
        self.wal = entry_from_wal_record({
            "table": "notes", "locator": Locator("rowid", 9), "rowid": 9,
            "values_dict": {"id": "9", "body": "old"}, "raw_values": [9, "old body"],
            "frame_idx": 12, "page_num": 4, "category": "superseded"})
        self.free = entry_from_freelist("notes", 7, 812, ["id", "body"], [44, "deleted body"],
                                        44, "High")
        s.add([self.hostile, self.formula, self.large, self.wal, self.free], "Relevant")
        s.add([self.hostile, self.free], "<b>odd</b>")
        s.set_note(self.hostile.key, "</td><script>x()</script> & more")
        self.info = export_info("2.0.0", evidence_files(self.ev), self.db, "all tagged rows")

    def entries(self):
        return self.store.entries()

    # -- details -------------------------------------------------------------------------------
    def test_evidence_details(self):
        with open(self.db, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        files = evidence_files(self.ev)
        self.assertEqual([(d["role"], d["sha256"]) for d in files], [("main", want)])
        ev = EvidenceSet(self.db)
        ev.start_hashing()
        self.assertEqual(evidence_files(ev, wait=30)[0]["sha256"], want)
        info = export_info("9.9", files, scope="x")
        self.assertEqual((info["tool"]["version"], info["database"], info["scope"]),
                         ("9.9", self.db, "x"))
        self.assertTrue(info["exported_utc"].endswith("Z"))

    # -- HTML ------------------------------------------------------------------------------------
    def test_html_escapes_and_embeds(self):
        text = html_report(self.entries(), self.store.defs, self.info)
        low = text.lower()
        check_page(self, text)                  # one script (the report's own), JSON data
        self.assertNotIn("<script>alert", low)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)      # value and column
        self.assertIn("&lt;/td&gt;&lt;script&gt;x()&lt;/script&gt; &amp; more", text)   # note
        self.assertNotIn("<img src=x", low)
        self.assertNotIn("<b>odd</b>", text)
        self.assertIn("&lt;b&gt;odd&lt;/b&gt;", text)
        self.assertNotIn("url(", text)                                   # colour sanitised
        self.assertIsNone(re.search(r"""(src|href)\s*=\s*["']?\s*(https?:|//)""", low))
        self.assertIn("content-security-policy", low)
        self.assertIn("default-src 'none'", text)
        # the PNG embedded as a data URI (its row is under both of its tags)
        self.assertEqual(low.count("<img "), 2)
        self.assertIn('src="data:image/png;base64,', text)
        # the evidence and its hash, the tool and the export time
        self.assertIn(self.info["evidence"][0]["sha256"], text)
        self.assertIn("SQLite GUI Analyzer 2.0.0", text)
        self.assertIn(self.info["exported_utc"], text)
        # BLOB summary and size; the large BLOB kept in part says so with its hash
        self.assertIn("bplist: dict (1 key)", text)
        self.assertIn(hashlib.sha256(self.big).hexdigest(), text)
        self.assertIn(format(len(self.big), ","), text)
        # a row with two tags is under each tag's section; provenance is shown
        tables = read_rows(text)
        self.assertEqual(sum(1 for t in tables.values() for r in t["rows"]
                             if "deleted body" in r), 2)
        self.assertEqual(text.count("<td>deleted body</td>"), 2)         # the plain tables
        self.assertIn("WAL frame 12 (superseded)", text)
        self.assertIn("page 7 @ 812", text)
        by_table = html_report(self.entries(), self.store.defs, self.info, layout="table")
        self.assertEqual(by_table.count("<td>deleted body</td>"), 1)
        self.assertEqual(sum(1 for t in read_rows(by_table).values() for r in t["rows"]
                             if "deleted body" in r), 1)
        self.assertEqual(by_table.lower().count("<img "), 1)
        check_page(self, by_table)

    def test_html_file_and_refusal(self):
        out = os.path.join(self.tmp, "report.html")
        self.assertEqual(export_html(out, self.entries(), self.store.defs, self.info,
                                     protected=self.protected), out)
        with open(out, encoding="utf-8") as f:
            check_page(self, f.read())
        before = dir_snapshot(self.evidence)
        with self.assertRaises(TagExportError):
            export_html(os.path.join(self.evidence, "r.html"), self.entries(), self.store.defs,
                        self.info, protected=self.protected)
        with self.assertRaises(TagExportError):
            export_json(os.path.join(self.evidence, "r.json"), self.entries(), self.store.defs,
                        self.info, protected=self.protected)
        self.assertEqual(dir_snapshot(self.evidence), before)

    # -- CSV -------------------------------------------------------------------------------------
    def read_csv(self, path):
        with open(path, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf", "no BOM in %s" % path)
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f))

    def test_csv_folder(self):
        out = os.path.join(self.tmp, "csv")
        full = {}

        def loader(entry, ci):
            return full.get((entry.key, ci))
        full[(self.large.key, 2)] = self.big
        res = export_csv(out, self.entries(), self.store.defs, self.info, self.protected, loader)
        names = sorted(os.listdir(out))
        self.assertIn("index.csv", names)
        self.assertIn("export_info.csv", names)
        self.assertIn("notes.csv", names)
        self.assertIn("blobs", names)
        index = self.read_csv(res["index"])
        self.assertEqual(len(index), 1 + len(self.entries()))
        head = index[0]
        for col in ("Tags", "Note", "Source", "Table", "Row", "Tagged at", "frame", "page",
                    "cell_offset", "confidence", "Key"):
            self.assertIn(col, head)
        rows = dict((r[head.index("Key")], r) for r in index[1:])
        free = rows[self.free.key]
        self.assertEqual((free[head.index("Tags")], free[head.index("page")],
                          free[head.index("cell_offset")], free[head.index("confidence")]),
                         ("Relevant; <b>odd</b>", "7", "812", "High"))
        self.assertEqual(rows[self.wal.key][head.index("frame_state")], "superseded")
        notes = self.read_csv(res["tables"]["notes"])
        self.assertEqual(notes[0][:6], ["#", "Row", "Source", "Tags", "Note", "Tagged at"])
        self.assertEqual(notes[0][6:], ["id", "body", "data"])
        body = dict((r[1], r) for r in notes[1:])
        self.assertEqual(body["2"][7], "'=cmd|' /C calc'!A0")          # not a formula
        self.assertEqual(body["3"][7], "line one\nline two")
        cell = body["3"][8]
        self.assertTrue(cell.startswith("blobs/") and "%s bytes" % format(len(self.big), ",")
                        in cell, cell)
        blob_path = os.path.join(out, cell.split(" ")[0])
        with open(blob_path, "rb") as f:
            self.assertEqual(f.read(), self.big)                        # whole, from the loader
        self.assertTrue(body["2"][8].startswith("blobs/") and "bplist" in body["2"][8])
        info = self.read_csv(res["info"])
        self.assertIn(self.info["evidence"][0]["sha256"], [c for r in info for c in r])
        self.assertIn(["tool", "SQLite GUI Analyzer 2.0.0"], info)
        self.assertEqual(res["blobs"], 3)                               # png, bplist, big
        self.assertEqual(res["blob_errors"], [])

        # a second export into the same folder replaces nothing
        before = dir_snapshot(out)
        before_blobs = dir_snapshot(os.path.join(out, "blobs"))
        res2 = export_csv(out, self.entries(), self.store.defs, self.info, self.protected)
        self.assertEqual(os.path.basename(res2["index"]), "index_2.csv")
        self.assertEqual(os.path.basename(res2["tables"]["notes"]), "notes_2.csv")
        after = dir_snapshot(out)
        for name, fp in before.items():
            self.assertEqual(after[name], fp)
        after_blobs = dir_snapshot(os.path.join(out, "blobs"))
        for name, fp in before_blobs.items():
            self.assertEqual(after_blobs[name], fp)
        self.assertEqual(len(after_blobs), 2 * len(before_blobs))
        # without the loader, only the part the tag kept is written, and named so
        partial = [n for n in after_blobs if "_first64KiB" in n]
        self.assertEqual(len(partial), 1)
        with open(os.path.join(out, "blobs", partial[0]), "rb") as f:
            self.assertEqual(f.read(), self.big[:T.BLOB_HEAD])

    def test_csv_refuses_the_evidence_folder(self):
        before = dir_snapshot(self.evidence)
        for target in (self.evidence, os.path.join(self.evidence, "sub")):
            with self.assertRaises(TagExportError):
                export_csv(target, self.entries(), self.store.defs, self.info, self.protected)
        self.assertEqual(dir_snapshot(self.evidence), before)
        self.assertEqual(os.listdir(self.evidence), ["freelist.db"])
        self.assertTrue(inside(os.path.join(self.evidence, "sub", "x.csv"), self.evidence))

    # -- JSON ------------------------------------------------------------------------------------
    def test_json_round_trip_through_load_tags(self):
        out = os.path.join(self.tmp, "export.json")
        export_json(out, self.entries(), self.store.defs, self.info, self.protected)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual((data["format"], data["tool"]["version"]),
                         ("sqlite-gui-analyzer-tags", "2.0.0"))
        self.assertEqual(data["evidence"]["sha256"], self.info["evidence"][0]["sha256"])
        self.assertEqual(data["evidence_files"], self.info["evidence"])
        fresh = TagStore(self.db, path=os.path.join(self.tmp, "fresh.json"))
        self.assertEqual(fresh.merge_file(out), (len(self.entries()), 0))
        self.assertEqual([e.key for e in fresh.entries()], [e.key for e in self.entries()])
        for e in self.entries():
            g = fresh.get(e.key)
            self.assertEqual((g.tags, g.note, g.source, g.table, g.rowid, g.provenance),
                             (e.tags, e.note, e.source, e.table, e.rowid, e.provenance))
            self.assertEqual(g.values, e.values)
        self.assertEqual(fresh.color_of("<b>odd</b>"), "#8993a4")
        big = fresh.get(self.large.key).row_values()[2]
        self.assertIsInstance(big, PartialBlob)
        self.assertEqual(big.sha256, hashlib.sha256(self.big).hexdigest())
        # loading it again changes nothing
        self.assertEqual(fresh.merge_file(out), (0, 0))
