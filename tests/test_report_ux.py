"""The forensic report reads for analysis: the records table leads with the state, table,
rowid, confidence and a preview; the recovery's numbers are words; findings' details are
lists, not JSON dumps; every record carries its byte offset."""
import os
import unittest

from tests.helpers import TempDirTest
from tests.fixtures import forensic_fixtures as ff
from engine.forensics import report as rp
from engine.session import Session


class ReportWordsTest(unittest.TestCase):
    def test_record_fields_lead_with_what_matters(self):
        self.assertEqual(rp.RECORD_FIELDS[:5],
                         ("state", "table", "rowid", "confidence", "preview"))
        self.assertEqual(rp.RECORD_FIELDS[-1], "id")
        self.assertIn("file_offset", rp.RECORD_FIELDS)

    def test_state_words(self):
        self.assertEqual(rp.record_state([]), "deleted")
        self.assertEqual(rp.record_state(["prior_version"]), "older version")
        self.assertEqual(rp.record_state(["content_live"]), "inserted again")
        self.assertEqual(rp.record_state(["unreferenced_page"]), "unreferenced page")
        self.assertEqual(rp.record_state([], index="idx"), "index entry")

    def test_carve_words(self):
        text = rp.carve_words({"pages": 3922, "candidates": 1961, "records": 1405,
                               "live_skipped": 239, "merged": 280, "seconds": 10.619,
                               "stopped": None, "errors": 0, "frames": 0})
        self.assertIn("pages looked at 3,922", text)
        self.assertIn("candidate cells 1,961", text)
        self.assertIn("copies of live rows left out 239", text)
        self.assertIn("took 10.6 s", text)
        self.assertIn("ran to the end", text)
        self.assertNotIn("WAL frames", text)            # zero: not said
        self.assertIn("page errors 0", text)            # zero errors: said
        self.assertIn("stopped: time limit reached",
                      rp.carve_words({"stopped": "time limit reached"}))

    def test_findings_details_are_readable(self):
        html = rp._details_html({"bytes": 2374841, "objects": ["a", "b"]})
        self.assertIn("<strong>bytes</strong>: 2,374,841", html)
        self.assertIn("<li>a</li>", html)
        self.assertNotIn("{", html)
        long = rp._details_html(list("abcdefghijkl"))
        self.assertIn("12 items", long)
        self.assertIn("<details", long)
        self.assertEqual(str(rp._details_html("plain")), "plain")

    def test_preview(self):
        r = {"columns": ["a", "b", "c", "d", "e", "f"], "values": [None, "", "x", 1, 2, 3]}
        self.assertEqual(rp._preview(r), "c=x, d=1, e=2, f=3")


class ReportOnRecordsTest(TempDirTest):
    def test_records_carry_state_and_byte_offsets(self):
        s = Session.open(ff.wal_history(self.tmp), hash_evidence=False)
        self.addCleanup(s.close)
        recs = list(s.forensics.carve())
        self.assertTrue(recs)
        data = rp.build(s.forensics, "test", records=recs,
                        carve_stats={"pages": 12, "records": len(recs), "seconds": 0.5})
        for d in data["records"]:
            self.assertIn(d["state"], ("deleted", "older version", "inserted again",
                                       "unreferenced page", "index entry"))
            self.assertIsNotNone(d["provenance"]["file_path"])
            if d["provenance"]["offset"] is not None:
                self.assertIsInstance(d["provenance"]["file_offset"], int)
        rows = list(rp._record_rows(data["records"]))
        self.assertEqual(len(rows), len(recs))
        self.assertEqual(len(rows[0]), len(rp.RECORD_FIELDS))
        self.assertIsInstance(rows[0][rp.RECORD_FIELDS.index("values")], dict)
        csv = rp.to_csv(data)
        self.assertIn("record,id,state,table", csv.replace("\r", ""))
        html = rp.to_html(data)
        self.assertIn("pages looked at", html)
        self.assertIn("file_offset", html)


if __name__ == "__main__":
    unittest.main()
