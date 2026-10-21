"""Overview: every loaded database's facts whole and copyable - the SHA-256 and the path in
the table, Copy / Export of every field; links between copies of the same table are not
links."""
import csv
import io
import json
import os
import shutil

from tests.test_review_ui import AppCase
from tests.fixtures import make_fixtures as fx


class OverviewFactsTest(AppCase):
    def test_facts_whole_and_exportable(self):
        src = fx.freelist(self.tmp)
        a = os.path.join(self.tmp, "userA", "Default")
        b = os.path.join(self.tmp, "userB", "Default")
        os.makedirs(a)
        os.makedirs(b)
        pa, pb = os.path.join(a, "History"), os.path.join(b, "History")
        shutil.copyfile(src, pa)
        shutil.copyfile(src, pb)

        def scenario():
            app = self.app
            app._open_paths([pa, pb], wait=True)
            ov = app._overview
            app._nb.select(ov)
            self.assertTrue(self.pump(lambda: all(m.sha256() for m in app.case), 60))
            ov.fill()
            self.pump(lambda: False, 0.3)
            names = sorted(m.name for m in app.case)
            self.assertEqual(names, ["History (userA)", "History (userB)"])   # not '#2'
            rows = ov.rows()
            cols = [c[0] for c in __import__("overview_tab").COLUMNS]
            path_at, hash_at = cols.index("path"), cols.index("hash")
            for r in rows:
                self.assertTrue(os.path.isabs(r[path_at]))
                self.assertEqual(len(r[hash_at]), 2 + 64)            # '✓ ' + the whole hash
            facts = ov.facts_rows()
            self.assertEqual(len(facts), 2)
            self.assertEqual(sorted(f["database"] for f in facts), names)
            for f in facts:
                self.assertEqual(len(f["sha256"]), 64)
                self.assertEqual(f["path"], pa if f["database"].endswith("(userA)") else pb)
                self.assertIsInstance(f["size_bytes"], int)
            text = ov.facts_text(facts, "csv")
            got = list(csv.reader(io.StringIO(text)))
            self.assertEqual(got[0], list(ov.FIELDS))
            self.assertEqual(len(got), 3)
            data = json.loads(ov.facts_text(facts, "json"))
            self.assertEqual(data[0]["sha256"], facts[0]["sha256"])
            self.assertIn("sha256: " + facts[0]["sha256"], ov.facts_text(facts, "text"))
            ov.copy_lines(every=True)
            self.assertIn(facts[1]["sha256"], app.clipboard_get())
        self.run_app(scenario)


class SameTableNotALinkTest(AppCase):
    def test_copies_of_one_table_are_listed_apart(self):
        from engine.crossdb import find_links
        from engine.session import Session
        import sqlite3
        paths = []
        for user in ("u1", "u2", "u3"):
            d = os.path.join(self.tmp, user)
            os.makedirs(d)
            p = os.path.join(d, "History")
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE meta (key TEXT, value TEXT)")
            c.executemany("INSERT INTO meta VALUES (?, ?)", [
                ("version", "58"), ("last_compatible_version", "16"), ("early_expiration", "0"),
                ("typed_url_sync", "1"), ("mmap_status", "-1")])
            c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
            c.executemany("INSERT INTO urls VALUES (?, ?)", [
                (i, "https://example.org/%s/%d" % (user, i)) for i in range(1, 30)])
            c.commit()
            c.close()
            paths.append(p)
        sessions = [(p, Session.open(p, hash_evidence=False)) for p in paths]
        try:
            res = find_links(sessions)
        finally:
            for _p, s in sessions:
                s.close()
        self.assertEqual([l for l in res.links if l.src_table == "meta"], [])
        self.assertIn(("meta", "key"), res.same_tables)
        self.assertEqual(len(res.same_tables[("meta", "key")]), 3)
        self.assertIn("meta.key in 3 databases", res.same_text())
