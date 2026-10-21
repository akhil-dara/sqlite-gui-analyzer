"""Queries across databases of a case: each attached by its session's own URI (WAL rows
included), joined by the query model's qualified names, and no further ATTACH possible."""

import os
import shutil
import sqlite3
import unittest

from tests.helpers import TempDirTest
from engine import backends
from engine.crossquery import CaseUnavailable, case_connection
from engine.querymodel import Join, QueryModel


class CrossQueryTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        from engine.session import Session
        a = os.path.join(self.tmp, "contacts.db")
        c = sqlite3.connect(a)
        c.execute("CREATE TABLE contacts (jid TEXT, name TEXT)")
        c.executemany("INSERT INTO contacts VALUES (?,?)",
                      [("100@s.net", "Ann"), ("200@s.net", "Bob"), ("300@s.net", "Cy")])
        c.commit()
        c.close()
        # messages: the newest row only in the WAL
        work = os.path.join(self.tmp, "w")
        os.makedirs(work)
        src, b = os.path.join(work, "msgstore.db"), os.path.join(self.tmp, "msgstore.db")
        c = sqlite3.connect(src, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, jid TEXT, body TEXT)")
        c.executemany("INSERT INTO messages VALUES (?,?,?)",
                      [(1, "100@s.net", "hi"), (2, "200@s.net", "yo")])
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("INSERT INTO messages VALUES (3, '300@s.net', 'only in the WAL')")
        for sfx in ("", "-wal"):
            shutil.copyfile(src + sfx, b + sfx)
        c.close()
        self.sa = Session.open(a, hash_evidence=False)
        self.sb = Session.open(b, hash_evidence=False)
        self.addCleanup(self.sa.close)
        self.addCleanup(self.sb.close)

    @unittest.skipUnless(backends.ram_overlay_supported(), "WAL images need Python 3.11+")
    def test_a_join_across_two_databases_sees_their_wal(self):
        conn, schemas = case_connection([("contacts.db", self.sa), ("msgstore.db", self.sb)])
        self.addCleanup(conn.close)
        m = QueryModel()
        msg = m.add_table("messages", db="msgstore.db")
        con = m.add_table("contacts", [Join(None, "jid", msg, "jid", "LEFT", "matched by value")],
                          db="contacts.db")
        m.toggle_column(msg, "body")
        m.toggle_column(con, "name")
        m.sort = [(msg, "id", False)]
        self.assertTrue(m.spans_databases())
        sql = m.to_sql(schemas)
        self.assertIn('"db2"."messages"', sql)
        self.assertIn('"db1"."contacts"', sql)
        rows = conn.execute(sql).fetchall()
        self.assertEqual(rows, [("hi", "Ann"), ("yo", "Bob"), ("only in the WAL", "Cy")])
        # nothing else can be attached or written through it
        for stmt in ("ATTACH DATABASE 'x.db' AS x", "DETACH DATABASE db1",
                     "CREATE TABLE db1.t (x)"):
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(stmt)

    def test_a_database_sqlite_cannot_read_is_named(self):
        class Native(object):
            sql = None
        with self.assertRaises(CaseUnavailable) as e:
            case_connection([("contacts.db", self.sa), ("broken.db", Native())])
        self.assertIn("broken.db", str(e.exception))


if __name__ == "__main__":
    unittest.main()
