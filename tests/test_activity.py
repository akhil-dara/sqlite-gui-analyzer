"""The examiner's activity log (engine.activity): one append-only file per case, thread-safe,
torn lines skipped, never inside an evidence folder, exported as CSV, JSON or text."""
import csv
import json
import os
import threading
from unittest import mock

from tests.helpers import TempDirTest, dir_snapshot
from engine.activity import ActivityError, ActivityLog, log_path


class ActivityLogTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = os.path.join(self.tmp, "appdata")
        self.dbs = [os.path.join(self.tmp, "ev", "b.db"), os.path.join(self.tmp, "ev", "A.db")]

    def test_same_databases_same_file(self):
        p1 = log_path(self.dbs, self.data)
        p2 = log_path(list(reversed(self.dbs)), self.data)
        self.assertEqual(p1, p2)
        self.assertRegex(os.path.basename(p1), r"^activity-2db-[0-9a-f]{12}\.jsonl$")
        self.assertEqual(os.path.dirname(p1), os.path.join(self.data, "activity"))
        if os.name == "nt":
            self.assertEqual(p1, log_path([d.upper() for d in self.dbs], self.data))
        self.assertNotEqual(p1, log_path(self.dbs[:1], self.data))
        with mock.patch.dict(os.environ, {"SGA_DATA_DIR": self.data}):
            self.assertEqual(log_path(self.dbs), p1)

    def test_append_only_across_objects(self):
        a = ActivityLog(self.dbs, self.data)
        self.assertTrue(a.log("open", paths=self.dbs))
        b = ActivityLog(list(reversed(self.dbs)), self.data)
        self.assertEqual(a.path, b.path)
        self.assertTrue(b.log("note", text="second"))
        self.assertTrue(a.log("close"))
        kinds = [e["kind"] for e in b.entries()]
        self.assertEqual(kinds, ["open", "note", "close"])
        self.assertEqual(b.entries()[1]["text"], "second")
        self.assertRegex(b.entries()[0]["utc"],
                         r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{6} UTC$")
        self.assertEqual([e["kind"] for e in a.entries(limit=2)], ["note", "close"])

    def test_threads(self):
        log = ActivityLog(self.dbs, self.data)

        def work(t):
            for i in range(200):
                log.log("search", thread=t, i=i, text="x" * (i % 50))
        threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(log.path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1600)
        entries = [json.loads(l) for l in lines]
        self.assertEqual(sorted((e["thread"], e["i"]) for e in entries),
                         sorted((t, i) for t in range(8) for i in range(200)))

    def test_torn_last_line_skipped_and_closed_off(self):
        log = ActivityLog(self.dbs, self.data)
        log.log("open")
        with open(log.path, "ab") as f:
            f.write(b'{"utc": "2026-01-01 00:00:00.000000 UTC", "kind": "ha')
        self.assertEqual([e["kind"] for e in log.entries()], ["open"])
        log.log("close")
        self.assertEqual([e["kind"] for e in log.entries()], ["open", "close"])

    def test_refuses_evidence_folder(self):
        evidence = os.path.join(self.tmp, "ev")
        os.makedirs(evidence)
        with open(os.path.join(evidence, "b.db"), "wb") as f:
            f.write(b"x")
        before = dir_snapshot(evidence)
        log = ActivityLog(self.dbs, directory=os.path.join(evidence, "appdata"),
                          refuse_in=[evidence])
        self.assertFalse(log.log("open"))
        self.assertIn("evidence folder", log.error)
        ok = ActivityLog(self.dbs, self.data, refuse_in=[evidence])
        self.assertTrue(ok.log("open"))
        self.assertIsNone(ok.error)
        with self.assertRaises(ActivityError):
            ok.export(os.path.join(evidence, "log.csv"), "csv")
        self.assertEqual(dir_snapshot(evidence), before)
        self.assertEqual(os.listdir(evidence), ["b.db"])

    def test_write_error_is_kept_not_raised(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as f:
            f.write("x")
        log = ActivityLog(self.dbs, directory=blocker)     # a file where a folder is needed
        self.assertFalse(log.log("open"))
        self.assertTrue(log.error.startswith("not logged"))
        self.assertEqual(log.entries(), [])

    def test_non_json_values(self):
        log = ActivityLog(self.dbs, self.data)
        self.assertTrue(log.log("export", blob=b"\x00\xff", obj=object(), nan=float("nan"),
                                nested={1: (b"\x01", {2, 3})}, kind_=1, utc="mine"))
        e = log.entries()[0]
        self.assertEqual(e["blob"], "00ff")
        self.assertTrue(e["obj"].startswith("<object"))
        self.assertEqual(e["nan"], "nan")
        self.assertEqual(e["nested"], {"1": ["01", [2, 3]]})
        self.assertEqual(e["field_utc"], "mine")
        self.assertEqual(e["kind"], "export")

    def test_export_formats(self):
        log = ActivityLog(self.dbs, self.data)
        log.log("open", path="C:\\x\\a.db", size=10)
        log.log("hash", sha256="ab" * 32)
        log.log("note", text="ünïcode, \"quoted\"")
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        self.assertEqual(log.export(os.path.join(out, "a.csv"), "csv"), 3)
        with open(os.path.join(out, "a.csv"), encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["utc", "kind", "details"])
        self.assertEqual(rows[1][1], "open")
        self.assertEqual(json.loads(rows[1][2]), {"path": "C:\\x\\a.db", "size": 10})
        self.assertEqual(log.export(os.path.join(out, "a.json"), "json"), 3)
        with open(os.path.join(out, "a.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["format"], "sqlite-gui-analyzer-activity")
        self.assertEqual([e["kind"] for e in data["entries"]], ["open", "hash", "note"])
        self.assertEqual(log.export(os.path.join(out, "a.txt"), "txt"), 3)
        with open(os.path.join(out, "a.txt"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("  note  text=ünïcode, \"quoted\"", lines[2])
        self.assertIn("size=10", lines[0])
        with self.assertRaises(ActivityError):
            log.export(os.path.join(out, "a.xml"), "xml")
