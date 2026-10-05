"""Copy with related in a case of several databases: the trusted links between databases
(engine.crossdb, 'matched by value') are followed too, every row names its database, and the
SQL selects the rows of the other database there."""

import json
import os
import shutil
import sqlite3
import tempfile

from tests.helpers import TempDirTest, copy_with_sidecars, norm
from tests.fixtures import case_fixtures as cf
from engine import datamap as dm
from engine import related_copy as rc
from engine.crossdb import find_links
from engine.relations import relation_map
from engine.schema import Locator
from engine.session import Session


class CaseCopyTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.paths = cf.build(self.tmp)
        names = ["messages.db", "contacts.db", "settings.db"]
        self.sessions = [Session.open(p, hash_evidence=False) for p in self.paths]
        for s in self.sessions:
            self.addCleanup(s.close)
        for s in self.sessions:
            relation_map(s).map_links()
        self.cross = find_links([(i + 1, s) for i, s in enumerate(self.sessions)])
        self.dbs = [rc.Database(i + 1, n, s) for i, (n, s) in
                    enumerate(zip(names, self.sessions))]
        self.case = rc.CaseSpec(1, self.dbs, self.cross.links)
        self.out = tempfile.mkdtemp(prefix="sga_rcc_out_")
        self.addCleanup(shutil.rmtree, self.out, True)

    def bundle(self, table="message", rowids=(3,), **kw):
        return rc.related_bundle(None, None, table, [Locator("rowid", r) for r in rowids],
                                 case=self.case, **kw)

    def test_follows_links_between_databases(self):
        b = self.bundle()
        groups = dict(((g.db, g.table), g) for g in b.roots[0].related)
        self.assertIn((1, "chat"), groups)              # inside messages.db
        g = groups[(2, "wa_contacts")]                   # in contacts.db, by value
        self.assertTrue(g.via.cross)
        self.assertEqual(g.via.text(), "messages.db › message.key_remote_jid → contacts.db › "
                                       "wa_contacts.jid, matched by value 100%")
        self.assertEqual(g.rows[0].values[1], cf.JIDS[3])
        self.assertNotIn(3, [k[0] for k in groups])      # settings.db shares nothing
        self.assertIn("contacts.db › wa_contacts", b.tables())
        md = b.render("markdown")
        self.assertIn("## messages.db › message row 3", md)
        self.assertIn("### contacts.db › wa_contacts (1 of 1 row), via messages.db › "
                      "message.key_remote_jid → contacts.db › wa_contacts.jid, matched by "
                      "value 100%", md)
        self.assertIn("### contacts.db › wa_contacts", md.split("## Schema")[1])
        d = json.loads(b.render("json"))
        self.assertEqual(d["databases"], ["messages.db", "contacts.db", "settings.db"])
        self.assertEqual(d["rows"][0]["database"], "messages.db")
        wa = [r for r in d["rows"][0]["related"] if r["table"] == "wa_contacts"][0]
        self.assertEqual((wa["database"], wa["via"]["kind"], wa["via"]["to_database"]),
                         ("contacts.db", "matched by value", "contacts.db"))
        self.assertIn("contacts.db › wa_contacts", d["schema"])

    def test_two_hops_across_and_back(self):
        b = self.bundle(hops=2)
        seen = [(n.db, n.table, n.locator) for n in b.roots]
        for _p, g in b.groups():
            seen.extend((g.db, g.table, r.locator) for r in g.rows)
        self.assertEqual(len(seen), len(set(seen)))
        # from the contact back to messages.db: the jid row with the same text
        dbs = set((g.db, g.table) for _p, g in b.groups())
        self.assertIn((1, "jid"), dbs)

    def test_sql_runs_in_each_database(self):
        copies = {}
        for key, p in zip((1, 2, 3), self.paths):
            d = os.path.join(self.out, str(key))
            os.makedirs(d)
            copies[self.dbs[key - 1].name] = copy_with_sidecars(p, d)
        b = self.bundle(rowids=(3, 4, 5), hops=2)
        text = b.render("sql")
        self.assertIn("-- Tables involved in contacts.db", text)
        queries = rc.chunk_queries(b.ctx, b.roots)
        self.assertTrue(any("matched by value" in q[0] for q in queries))
        for title, sql, rows, db in queries:
            self.assertIn("-- in %s\n%s" % (db, sql), text)
            c = sqlite3.connect(copies[db])
            try:
                got = c.execute(sql).fetchall()
            finally:
                c.close()
            ncols = len(rows[0].columns)
            want = sorted(rows, key=lambda n: n.locator.value)
            self.assertEqual([tuple(norm(v) for v in r[:ncols]) for r in got],
                             [tuple(norm(v) for v in n.values) for n in want], title)

    def test_streamed_export_and_a_bad_case(self):
        s = self.sessions[0]
        path = os.path.join(self.out, "all.json")
        res = rc.export_related_file(None, None, "message", s.iter_rows("message"), path,
                                     "json", case=self.case, total=s.count("message"))
        self.assertTrue(res.complete)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["rows_written"], 120)
        linked = [r for r in d["rows"] for g in r["related"] if g["database"] == "contacts.db"]
        self.assertGreater(len(linked), 0)
        with self.assertRaises(ValueError):
            rc.CaseSpec(9, self.dbs, [])

    def test_a_single_database_is_unchanged(self):
        b = rc.related_bundle(self.sessions[0], relation_map(self.sessions[0]), "message",
                              [Locator("rowid", 3)])
        self.assertNotIn("wa_contacts", [g.table for g in b.roots[0].related])
        self.assertNotIn("database", json.loads(b.render("json"))["rows"][0])
        self.assertIsInstance(dm.database_name(self.sessions[0]), str)
