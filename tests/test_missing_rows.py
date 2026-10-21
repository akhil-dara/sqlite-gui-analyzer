"""Missing row numbers (deleted rows, recovered or not) and the encrypted-file check."""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engine.session import Session  # noqa: E402


class MissingRowsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def db(self, sql, rows=50, delete=()):
        p = os.path.join(self.dir, "m%d.db" % len(os.listdir(self.dir)))
        c = sqlite3.connect(p)
        c.execute(sql)
        c.executemany("INSERT INTO t (body) VALUES (?)", [("m%d" % i,) for i in range(rows)])
        if delete:
            c.execute("DELETE FROM t WHERE rowid IN (%s)" % ",".join(map(str, delete)))
        c.commit()
        c.close()
        s = Session.open(p, hash_evidence=False)
        self.addCleanup(s.close)
        return s

    def test_autoincrement_gaps_and_the_deleted_last_rows(self):
        s = self.db("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT)",
                    delete=(1, 2, 10, 11, 12, 30, 49, 50))
        m = s.forensics.missing_keys("t")
        self.assertEqual(m.keys, [1, 2, 10, 11, 12, 30, 49, 50])
        self.assertEqual(m.ranges, [(1, 2), (10, 12), (30, 30), (49, 50)])
        self.assertEqual((m.gaps, m.after, m.before, m.sequence), (4, 2, 2, 50))
        self.assertIn("2 after the last live row, up to 50", m.text())
        self.assertIn("2 before the first live row", m.text())

    def test_without_autoincrement_only_gaps_are_evidence(self):
        s = self.db("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)",
                    delete=(1, 2, 20, 50))
        m = s.forensics.missing_keys("t")
        self.assertEqual(m.keys, [20])                   # 1-2: not listed; 50: no trace
        self.assertEqual(m.before, 2)
        self.assertIn("Numbers below 3 are not counted", m.text())
        self.assertIn("a deleted last row leaves no gap", m.text())

    def test_recovered_rows_and_the_listing_limit(self):
        from engine.forensics.missing import missing_keys
        s = self.db("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT)",
                    rows=30, delete=range(5, 26))

        class R(object):
            table, index, rowid = "t", None, 7
        m = missing_keys(s, "t", limit=10, records=[R()])
        self.assertEqual(m.count, 21)
        self.assertEqual(len(m.keys), 10)
        self.assertTrue(m.cut)
        self.assertEqual(list(m.recovered), [7])
        self.assertIn("1 of the missing rows were recovered", m.text())

    def test_a_recovered_row_inside_a_huge_gap_is_matched(self):
        # a gap of over a million numbers was left out of the check against recovered rows
        from engine.forensics.missing import missing_keys
        p = os.path.join(self.dir, "huge.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        c.executemany("INSERT INTO t VALUES (?, 'x')", [(1,), (2,), (3000000,)])
        c.commit()
        c.close()
        s = Session.open(p, hash_evidence=False)
        self.addCleanup(s.close)

        class R(object):
            table, index, rowid = "t", None, 1500000

        class Other(object):
            table, index, rowid = "t", None, 2               # live, not missing
        m = missing_keys(s, "t", limit=10, records=[R(), Other()])
        self.assertEqual(m.ranges, [(3, 2999999)])
        self.assertEqual(list(m.recovered), [1500000])

    def test_without_rowid_has_no_numbers(self):
        p = os.path.join(self.dir, "w.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
        c.execute("INSERT INTO kv VALUES ('a', 'b')")
        c.commit()
        c.close()
        s = Session.open(p, hash_evidence=False)
        self.addCleanup(s.close)
        self.assertIn("WITHOUT ROWID", s.forensics.missing_keys("kv").text())


class EncryptedTest(unittest.TestCase):
    def test_a_readable_header_over_encrypted_pages_is_named(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "plain.db")
        c = sqlite3.connect(p)
        c.execute("PRAGMA page_size=4096")
        c.execute("CREATE TABLE t (x)")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
        c.close()
        s = Session.open(p, hash_evidence=False)
        self.assertEqual(s.encrypted, "")               # a normal file is not flagged
        s.close()
        # as SQLCipher with a plaintext header writes it: header kept, 80 reserved bytes,
        # every byte after the header scrambled
        with open(p, "rb") as f:
            raw = bytearray(f.read())
        raw[20] = 80
        for i in range(100, len(raw)):
            raw[i] = (raw[i] * 73 + i * 151 + 88) % 256
        enc = os.path.join(d, "enc.db")
        with open(enc, "wb") as f:
            f.write(bytes(raw))
        s = Session.open(enc, hash_evidence=False)
        self.addCleanup(s.close)
        self.assertIn("looks encrypted", s.encrypted)
        self.assertIn("80 reserved bytes", s.encrypted)
        self.assertEqual(s.banners()[0].short, "Encrypted")


if __name__ == "__main__":
    unittest.main()
