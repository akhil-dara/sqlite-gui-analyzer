"""A file with another product's signature in place of 'SQLite format 3' and no readable
table (an encrypted database format over SQLite's layout) is said to be encrypted - never
shown as an empty, healthy database."""
import os
import random
import sqlite3
import unittest

from tests.helpers import TempDirTest
from engine.session import Session


def foreign(tmp):
    path = os.path.join(tmp, "crypt.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size=4096")
    con.execute("CREATE TABLE t (a, b)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 50) for i in range(300)])
    con.commit()
    con.close()
    data = bytearray(open(path, "rb").read())
    data[:16] = b"SQLitecrypt.com!"
    data[20] = 255                                  # reserved bytes per page, as such formats set
    rnd = random.Random(7)
    for i in range(100, len(data)):                 # every page's content: unreadable
        data[i] = rnd.randrange(256)
    data[100] = 13                                  # even a byte that looks like a leaf page type
    with open(path, "wb") as f:
        f.write(data)
    return path


class ForeignHeaderTest(TempDirTest):
    def test_said_to_be_encrypted(self):
        s = Session.open(foreign(self.tmp), hash_evidence=False)
        self.addCleanup(s.close)
        self.assertEqual(s.tables(), [])
        self.assertIn("SQLitecrypt.com!", s.encrypted)
        self.assertIn("not a plain SQLite database", s.encrypted)
        self.assertIn("encrypted", s.encrypted)
        errors = [b for b in s.banners() if b.level == "error"]
        self.assertTrue(any("SQLitecrypt.com!" in b.text for b in errors))


if __name__ == "__main__":
    unittest.main()
