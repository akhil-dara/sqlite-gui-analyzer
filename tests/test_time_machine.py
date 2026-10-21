"""The WAL time machine: the database as it was at an earlier commit - in the engine, and in a
case beside the current state (named by commit, told apart by its key)."""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engine.session import Session, SessionError  # noqa: E402


def wal_db(d):
    """A database whose WAL holds 4 commits, each changing row 1 (copied while open)."""
    work = os.path.join(d, "_w")
    os.makedirs(work)
    w = os.path.join(work, "t.db")
    c = sqlite3.connect(w, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.execute("INSERT INTO t VALUES (1, 'version 1')")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")               # the main file: version 1
    for i in range(2, 6):
        c.execute("UPDATE t SET v = ? WHERE id = 1", ("version %d" % i,))
    c.execute("INSERT INTO t VALUES (2, 'added last')")
    out = os.path.join(d, "t.db")
    for sfx in ("", "-wal"):
        shutil.copyfile(w + sfx, out + sfx)
    c.close()
    return out


class CheckpointTest(unittest.TestCase):
    """A checkpoint that already copied later commits into the main file is said: 'as of k'
    then shows those pages at their later state."""

    def make(self, checkpoint):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        work = os.path.join(d, "_w")
        os.makedirs(work)
        w = os.path.join(work, "t.db")
        c = sqlite3.connect(w, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        for i in range(1, 4):
            c.execute("INSERT INTO t VALUES (?, 'row')", (i,))
        if checkpoint:
            c.execute("PRAGMA wal_checkpoint(PASSIVE)")    # copied, the WAL kept
        out = os.path.join(d, "t.db")
        for sfx in ("", "-wal", "-shm"):
            shutil.copyfile(w + sfx, out + sfx)
        c.close()
        return out

    def text(self, path, k):
        s = Session.open(path, hash_evidence=False, as_of=k)
        try:
            return s.as_of_text()
        finally:
            s.close()

    def test_later_commits_already_in_the_main_file_are_said(self):
        text = self.text(self.make(True), 1)
        self.assertIn("A checkpoint already copied WAL frames 1-", text)
        self.assertIn("show their state at commit 4, not 1", text)

    def test_no_checkpoint_says_nothing_more(self):
        text = self.text(self.make(False), 1)
        self.assertNotIn("checkpoint", text)

    def test_without_the_shm_it_says_it_cannot_tell(self):
        path = self.make(True)
        os.remove(path + "-shm")
        self.assertIn("No usable -shm", self.text(path, 1))


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = wal_db(self.dir)

    def rows(self, **kw):
        s = Session.open(self.path, hash_evidence=False, **kw)
        try:
            return [tuple(r.values) for r in s.iter_rows("t")], s.as_of_text(), \
                len(s.wal.commit_ends())
        finally:
            s.close()

    def test_every_commit_shows_the_rows_of_its_time(self):
        now, text, n = self.rows()
        self.assertEqual(now, [(1, "version 5"), (2, "added last")])
        self.assertEqual(text, "")
        self.assertEqual(n, 5)
        for k, want in ((1, [(1, "version 2")]), (3, [(1, "version 4")]),
                        (4, [(1, "version 5")]), (5, now)):
            got, text, n_then = self.rows(as_of=k)
            self.assertEqual(got, want, "commit %d" % k)
            self.assertIn("As of commit %d of 5" % k, text)
            self.assertEqual(n_then, k)

    def test_impossible_requests_are_refused_with_a_reason(self):
        with self.assertRaises(SessionError) as e:
            Session.open(self.path, hash_evidence=False, as_of=9)
        self.assertIn("no commit 9", str(e.exception))
        p = os.path.join(self.dir, "plain.db")
        sqlite3.connect(p).execute("CREATE TABLE x (y)").connection.close()
        with self.assertRaises(SessionError) as e:
            Session.open(p, hash_evidence=False, as_of=1)
        self.assertIn("no WAL", str(e.exception))


class CaseTest(unittest.TestCase):
    def setUp(self):
        from tests.helpers import free_tk
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = wal_db(self.dir)
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display (a CI runner on Linux)
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.geometry("1100x700+-4000+0")
        self.app._forensics.AUTO_DELAY_MS = 600000

    def settle(self, t=0.5):
        end = time.time() + t
        while time.time() < end:
            self.app.update()
            time.sleep(0.02)

    def test_a_commit_opens_beside_the_current_state(self):
        app = self.app
        app._open_db(self.path, wait=True)
        self.settle()
        live = app.case.active
        values = app._wal_frame._tm_values
        self.assertEqual(len(values), 5)
        self.assertIn("the current state", values[-1])
        m = app.open_as_of(live, 2, wait=True)
        self.settle()
        self.assertEqual([x.name for x in app.case], ["t.db", "t.db @ commit 2 of 5"])
        self.assertNotEqual(m.key, live.key)
        self.assertEqual(m.as_of, (2, 5))
        self.assertEqual([tuple(r.values) for r in m.db.session.iter_rows("t")],
                         [(1, "version 3")])
        self.assertIs(app.open_as_of(live, 2, wait=True), m)    # already open: made active
        self.assertEqual(len(app.case), 2)
        app._wal_frame.on_open()                                 # a view as of a commit
        self.assertFalse(app._wal_frame.tm_row.winfo_manager())  # offers no time machine


if __name__ == "__main__":
    unittest.main()
