import os
import sqlite3
import time

from tests.helpers import TempDirTest
from engine.evidence import EvidenceSet, JOURNAL_MAGIC, sqlite_uri


class UriTest(TempDirTest):
    def test_awkward_names_open_read_only(self):
        d = os.path.join(self.tmp, "case #1 100% é")
        os.makedirs(d)
        p = os.path.join(d, "a b#c%d.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (42)")
        c.commit()
        c.close()
        conn = sqlite3.connect(sqlite_uri(p), uri=True)
        self.assertEqual(conn.execute("SELECT x FROM t").fetchone(), (42,))
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")
        conn.close()

    def test_uri_forms(self):
        self.assertTrue(sqlite_uri(os.path.join(self.tmp, "x.db")).endswith("?mode=ro&immutable=1"))
        self.assertIn("%23", sqlite_uri(os.path.join(self.tmp, "a#b.db")))
        if os.name == "nt":
            self.assertTrue(sqlite_uri(r"C:\x\y.db").startswith("file:///C:/x/y.db"))
            self.assertTrue(sqlite_uri(r"\\server\share\y.db").startswith("file:////server/share/y.db"))


class EvidenceSetTest(TempDirTest):
    def make(self, sidecars=("-wal",)):
        p = os.path.join(self.tmp, "ev.db")
        with open(p, "wb") as f:
            f.write(b"x" * 5000)
        for s in sidecars:
            with open(p + s, "wb") as f:
                f.write(b"y" * 100)
        return p

    def test_discovers_sidecars_and_hashes(self):
        ev = EvidenceSet(self.make(("-wal", "-shm")))
        self.assertEqual(sorted(ev.paths), ["main", "shm", "wal"])
        ev.start_hashing()
        self.assertTrue(ev.wait_hashing(10))
        self.assertEqual(len(ev.fingerprints["main"].sha256), 64)
        self.assertTrue(ev.verify(rehash=True).unchanged)

    def test_verify_detects_modification_and_new_files(self):
        p = self.make()
        ev = EvidenceSet(p)
        time.sleep(0.02)
        with open(p, "ab") as f:
            f.write(b"!")
        with open(p + "-shm", "wb") as f:
            f.write(b"new")
        report = ev.verify()
        self.assertFalse(report.unchanged)
        text = report.text()
        self.assertIn("size", text)
        self.assertIn("ev.db-shm", text)

    def test_write_guard(self):
        ev = EvidenceSet(self.make())
        self.assertTrue(ev.is_protected(os.path.join(self.tmp, "export.csv")))
        self.assertTrue(ev.is_protected(os.path.join(self.tmp, "sub", "x.bin")))
        self.assertFalse(ev.is_protected(os.path.join(os.path.dirname(self.tmp), "elsewhere.csv")))

    def test_hot_journal(self):
        p = self.make(("-journal",))
        self.assertFalse(EvidenceSet(p).journal_is_hot())
        with open(p + "-journal", "wb") as f:
            f.write(JOURNAL_MAGIC + b"\x00\x00\x00\x01" + b"\x00" * 100)
        self.assertTrue(EvidenceSet(p).journal_is_hot())

    def test_hash_error_is_recorded(self):
        p = self.make()
        ev = EvidenceSet(p)
        os.remove(p)
        ev.start_hashing()
        self.assertFalse(ev.wait_hashing(10))
        self.assertIsNotNone(ev.hash_error)
        self.assertIn("ev.db", ev.hash_error)
        self.assertFalse(ev.hashing_done)

    def test_cancel_before_start(self):
        ev = EvidenceSet(self.make())
        ev.cancel_hashing()
        ev.start_hashing()
        self.assertFalse(ev.wait_hashing(10))
        self.assertEqual(ev.hashed_bytes, 0)
        for fp in ev.fingerprints.values():
            self.assertIsNone(fp.sha256)
        self.assertIsNone(ev.hash_error)

    def test_progress_reaches_total(self):
        ev = EvidenceSet(self.make())
        ev.start_hashing()
        self.assertTrue(ev.wait_hashing(10))
        self.assertEqual(ev.hashed_bytes, ev.total_bytes)
