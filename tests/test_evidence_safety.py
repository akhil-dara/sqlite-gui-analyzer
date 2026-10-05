"""Opening, browsing, searching and closing must never change the evidence directory."""
import os
import sqlite3
import unittest

from tests.helpers import TempDirTest, dir_snapshot
from tests.fixtures import make_fixtures as fx
from engine.backends import ram_overlay_supported
from engine.session import RAM_OVERLAY, Session

from database import DB


class EvidenceSafetyTest(TempDirTest):
    def exercise(self, path, **kw):
        folder = os.path.dirname(path)
        before = dir_snapshot(folder)
        db = DB()
        db.open(path, **kw)
        db.session.evidence.wait_hashing(30)
        for t in db.tables() + db.views():
            db.browse(t, 100, 0)
            db.count(t)
            db.approx_count(t)
            cols, rows = db.browse(t, 1, 0)
            if rows:
                db.full_row(t, rows[0][0])
            list(db.search(t, db.columns(t), "e", "Case-Insensitive", 20, True, None))
        if db.has_wal:
            list(db.wal.recover_all_records())
            db.wal.summary()
            db.wal.table_stats()
        db.freed_page_records()
        conn = db.new_sql_conn()
        if conn is not None:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            conn.close()
        report = db.close()
        self.assertTrue(report.unchanged, report.text())
        self.assertEqual(dir_snapshot(folder), before)

    def isolated(self, builder, *args):
        """Build a fixture alone in its own folder so the folder snapshot is exact."""
        self._n = getattr(self, "_n", 0) + 1
        sub = os.path.join(self.tmp, "%s_%d" % (builder.__name__, self._n))
        os.makedirs(sub)
        return builder(sub, *args)

    def test_every_fixture_every_mode(self):
        for builder in (fx.without_rowid, fx.quirks, fx.corrupt, fx.freelist, fx.views,
                        fx.damaged_records, fx.unreadable_by_sqlite, fx.damaged_wal_record):
            self.exercise(self.isolated(builder))
        for builder in (fx.wal_states, fx.wal_only_data):
            self.exercise(self.isolated(builder))
            self.exercise(self.isolated(builder), ram_limit=0)

    def test_every_fixture_with_safe_parse(self):
        for builder in (fx.without_rowid, fx.quirks, fx.corrupt, fx.freelist, fx.views,
                        fx.damaged_records, fx.unreadable_by_sqlite, fx.damaged_wal_record,
                        fx.wal_states, fx.wal_only_data):
            self.exercise(self.isolated(builder), safe_parse=True)

    def test_hot_journal_is_not_rolled_back(self):
        path = self.isolated(fx.freelist)
        with open(path + "-journal", "wb") as f:
            f.write(bytes.fromhex("d9d505f920a163d7") + b"\x00\x00\x00\x01" + b"\x00" * 500)
        self.exercise(path)


class SqlConnectionGuardTest(TempDirTest):
    """The SQL tab runs user SQL on the session's connections: nothing it runs may create a
    file next to the evidence or change what the other tabs show."""

    isolated = EvidenceSafetyTest.isolated

    def open_session(self, builder):
        path = self.isolated(builder)
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        return s, os.path.dirname(path)

    def test_attach_and_vacuum_into_are_refused(self):
        s, folder = self.open_session(fx.freelist)
        before = dir_snapshot(folder)
        target = folder.replace("\\", "/")
        owned = s.new_connection()
        self.addCleanup(owned.close)
        for conn in (s.conn(), owned):
            for stmt in ("ATTACH '%s/attached.db' AS x" % target,
                         "VACUUM INTO '%s/copy.db'" % target,
                         "PRAGMA temp_store_directory = '%s'" % target):
                with self.assertRaises(sqlite3.DatabaseError, msg=stmt):
                    conn.execute(stmt)
            self.assertEqual(conn.execute("SELECT count(*) FROM notes").fetchone()[0], 20)
        self.assertEqual(dir_snapshot(folder), before)

    @unittest.skipUnless(ram_overlay_supported(), "needs Python 3.11+ / SQLite 3.36+")
    def test_ram_overlay_readers_cannot_write_the_shared_image(self):
        s, _folder = self.open_session(fx.wal_only_data)
        self.assertEqual(s.mode, RAM_OVERLAY)
        owned = s.new_connection()
        self.addCleanup(owned.close)
        for conn in (s.conn(), owned):
            conn.execute("PRAGMA query_only=OFF")
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                conn.execute("DELETE FROM activity")
        s._counts.clear()
        self.assertEqual(s.count("activity"), 40)
        self.assertEqual(len(s.browse("activity", 0, 100).rows), 40)
