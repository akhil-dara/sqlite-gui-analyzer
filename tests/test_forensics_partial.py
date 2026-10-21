"""Partial records: an update to a shorter value leaves the start of the old record in a
freeblock, its first bytes overwritten by the freeblock header and its end by the new
record. What survives is recovered, flagged 'partial', low confidence - and nothing is made
of a cut text too short to mean anything."""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engine.session import Session  # noqa: E402


class PartialRecordTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def carve(self, long_text, new_text):
        p = os.path.join(self.dir, "p%d.db" % len(os.listdir(self.dir)))
        c = sqlite3.connect(p)
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO t VALUES (?,?)",
                      [(i, long_text % i) for i in range(1, 30)])
        c.execute("UPDATE t SET v = ? WHERE id = 15", (new_text,))
        c.commit()
        c.close()
        s = Session.open(p, hash_evidence=False)
        self.addCleanup(s.close)
        return [r for r in s.forensics.carve() if r.table == "t"]

    def test_the_start_of_the_old_value_is_recovered_as_partial(self):
        recs = self.carve("long original text %d " + "q" * 80, "short")
        partial = [r for r in recs if "partial" in r.flags]
        self.assertEqual(len(partial), 1)
        r = partial[0]
        self.assertTrue(str(r.values[1]).startswith("long original text 15 qqq"))
        self.assertEqual(r.confidence, "low")
        self.assertTrue(any("newer record" in n for n in r.reasons))

    def test_a_short_leftover_makes_no_record(self):
        # the old values are short: what a partial reading would keep is under 16 characters
        recs = self.carve("val %d xxxxxxxxx", "s")
        self.assertEqual([r for r in recs if "partial" in r.flags], [])


class UnallocatedPartialTest(unittest.TestCase):
    """A block of deleted rows: the last record of a page's unallocated area has its tail
    overwritten by a live cell. Its start is recovered; a cut copy of a row that is still
    live (left behind by a page split) is not reported."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        p = os.path.join(cls.dir, "places.db")
        c = sqlite3.connect(p)
        c.execute("PRAGMA page_size=1024")
        c.execute("CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
                  "visit_count INTEGER, last_visit_date INTEGER)")
        c.executemany("INSERT INTO moz_places VALUES (?,?,?,?,?)",
                      [(i, "https://site%03d.example/path/page" % i, "Page title %d" % i, 1,
                        1700000000000000 + i) for i in range(1, 401)])
        c.execute("DELETE FROM moz_places WHERE id BETWEEN 100 AND 250")
        c.commit()
        c.close()
        cls.session = Session.open(p, hash_evidence=False)
        cls.partial = [r for r in cls.session.forensics.carve() if "partial" in r.flags]

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        shutil.rmtree(cls.dir, True)

    def test_the_cut_record_is_recovered(self):
        unalloc = [r for r in self.partial if r.prov.source == "unallocated"]
        self.assertTrue(unalloc)
        for r in unalloc:
            self.assertEqual(r.confidence, "low")
            self.assertTrue(str(r.values[1]).startswith("https://site"))

    def test_no_partial_copy_of_a_live_row(self):
        for r in self.partial:
            self.assertTrue(100 <= int(r.values[0]) <= 250, r.values)


class BlobPartialTest(unittest.TestCase):
    """The same with the cut value a BLOB (JSON stored as bytes): a cut copy of a live row is
    the live row's start, not an older version of it."""

    def test_no_partial_copy_of_a_live_row_when_the_cut_value_is_a_blob(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "pay.db")
        c = sqlite3.connect(p)
        c.execute("PRAGMA page_size=1024")
        c.execute("CREATE TABLE txn (id INTEGER PRIMARY KEY, ref TEXT, state TEXT, data BLOB)")
        c.executemany("INSERT INTO txn VALUES (?,?,?,?)",
                      [(i, "T%010d" % i, "COMPLETED", ('{"amount": %d, "note": "payment '
                        'number %d for the demo"}' % (i * 13, i)).encode()) for i in
                       range(1, 401)])
        c.execute("DELETE FROM txn WHERE id BETWEEN 100 AND 250")
        c.commit()
        c.close()
        s = Session.open(p, hash_evidence=False)
        self.addCleanup(s.close)
        partial = [r for r in s.forensics.carve() if "partial" in r.flags]
        for r in partial:
            self.assertTrue(100 <= int(r.values[0]) <= 250, r.values)
            self.assertNotIn("prior_version", r.flags)


if __name__ == "__main__":
    unittest.main()
