"""Rows written before an ALTER TABLE ADD COLUMN have fewer values: deleted ones are still
recovered, and only tables whose live rows show such records get the shorter shapes."""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engine.session import Session  # noqa: E402


class AlterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def open(self, alter):
        p = os.path.join(self.dir, "a%d.db" % alter)
        c = sqlite3.connect(p)
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE msg (id INTEGER PRIMARY KEY, body TEXT)")
        c.executemany("INSERT INTO msg VALUES (?,?)", [(i, "old body %d" % i)
                                                      for i in range(1, 31)])
        if alter:
            c.execute("ALTER TABLE msg ADD COLUMN extra TEXT")
            c.executemany("INSERT INTO msg VALUES (?,?,?)",
                          [(i, "new body %d" % i, "added %d" % i) for i in range(31, 41)])
        c.execute("DELETE FROM msg WHERE id IN (5, 6%s)" % (", 35" if alter else ""))
        c.commit()
        c.close()
        s = Session.open(p, hash_evidence=False)
        self.addCleanup(s.close)
        return s

    def test_rows_from_before_the_alter_are_recovered(self):
        s = self.open(alter=True)
        tpls = [t for t in s.forensics.templates() if t.name == "msg"]
        self.assertEqual(sorted(t.n for t in tpls), [2, 3])      # the table, and its old shape
        recs = [r for r in s.forensics.carve() if r.table == "msg"]
        found = dict((r.values[1], r) for r in recs)
        for text in ("old body 5", "old body 6", "new body 35"):
            self.assertIn(text, found)
        self.assertIn("pre_alter", found["old body 5"].flags)
        self.assertIsNone(found["old body 5"].values[2])         # the added column: no value

    def test_no_shorter_shape_without_evidence_of_an_alter(self):
        s = self.open(alter=False)
        tpls = [t for t in s.forensics.templates() if t.name == "msg"]
        self.assertEqual([t.n for t in tpls], [2])


if __name__ == "__main__":
    unittest.main()
