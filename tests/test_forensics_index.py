"""Carving deleted index entries: index definitions (plain, expression, DESC, UNIQUE,
partial, autoindex), entries recovered from freeblocks, unallocated space, freed pages and
older page copies, live entries excluded, links to table records, hostile input."""
import sqlite3
import unittest

from tests.helpers import TempDirTest
from tests.fixtures import forensic_fixtures as ff
from engine.forensics.index_carve import (IndexTemplate, _cmp_value, index_specs,
                                          parse_create_index)
from engine.forensics.provenance import HIGH, LOW, MEDIUM, value_key
from engine.session import Session


class IndexTestBase(TempDirTest):
    def open(self, path):
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        return s

    @staticmethod
    def entries(records, index=None):
        return [r for r in records if r.index is not None and (index is None or r.index == index)]


class IndexDefinitionTest(IndexTestBase):
    def test_parse_create_index(self):
        got = parse_create_index('CREATE UNIQUE INDEX IF NOT EXISTS main."ix a" ON "t b"'
                                 '(a DESC, lower(b) COLLATE NOCASE, "c,d" ASC) WHERE a > 0;')
        unique, name, table, terms, where = got
        self.assertTrue(unique)
        self.assertEqual((name, table), ("ix a", "t b"))
        self.assertEqual(terms, [("a", True, None), ("lower(b)", False, "NOCASE"),
                                 ('"c,d"', False, None)])
        self.assertEqual(where, "a > 0")
        self.assertIsNone(parse_create_index("CREATE TABLE t(a)"))
        self.assertIsNone(parse_create_index("CREATE INDEX i ON t"))

    def test_specs_of_every_kind_of_index(self):
        s = self.open(ff.indexed_deleted(self.tmp))
        specs = dict((sp.name, sp) for sp in index_specs(s.schema, s.schema.entries))
        self.assertEqual(set(specs), set(ff.MEMBER_INDEXES))
        self.assertEqual(specs["m_name"].names, ["name", "rowid"])
        age = specs["m_age_name"]
        self.assertEqual(age.names, ["age", "lower(name)", "rowid"])
        self.assertEqual([c.desc for c in age.columns], [True, False, False])
        self.assertEqual([c.cid for c in age.columns], [3, -2, -1])
        city = specs["m_city_email"]
        self.assertEqual(city.where, "age > 0")
        self.assertEqual(city.columns[1].collation, "NOCASE")
        auto = specs["sqlite_autoindex_members_1"]
        self.assertTrue(auto.auto and auto.unique)
        self.assertEqual(auto.names, ["email", "rowid"])
        self.assertIn("UNIQUE index", auto.describe())
        self.assertEqual(IndexTemplate(age).n, 3)

    def test_index_of_a_without_rowid_table_stores_the_key(self):
        path = self.tmp + "/wr.db"
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE w(a TEXT, b INT, c TEXT, PRIMARY KEY(c, a)) WITHOUT ROWID")
        c.execute("CREATE INDEX w_b ON w(b, a)")
        c.commit()
        c.close()
        s = self.open(path)
        spec = index_specs(s.schema, s.schema.entries)[0]
        self.assertEqual(spec.names, ["b", "a", "c"])
        self.assertFalse(spec.has_rowid)

    def test_parser_fallback_when_the_replay_fails(self):
        path = self.tmp + "/fn.db"
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t(a TEXT, b INT)")
        try:
            c.create_function("myfn", 1, lambda v: v, deterministic=True)
            c.execute("CREATE INDEX t_fn ON t(myfn(a), b DESC)")
        except sqlite3.Error:
            c.close()
            self.skipTest("this SQLite cannot index a user function")
        c.commit()
        c.close()
        s = self.open(path)
        spec = [sp for sp in index_specs(s.schema, s.schema.entries) if sp.name == "t_fn"][0]
        self.assertEqual(spec.source, "parser")
        self.assertEqual(spec.names, ["myfn(a)", "b", "rowid"])
        self.assertEqual([c.desc for c in spec.columns], [False, True, False])

    def test_comparison_follows_sqlite(self):
        self.assertEqual(_cmp_value(None, 0, None), -1)
        self.assertEqual(_cmp_value(2, 1.5, None), 1)
        self.assertEqual(_cmp_value(9, "1", None), -1)
        self.assertEqual(_cmp_value("b", b"a", None), -1)
        self.assertEqual(_cmp_value("B", "a", None), -1)
        self.assertEqual(_cmp_value("B", "a", "NOCASE"), 1)
        self.assertEqual(_cmp_value("a  ", "a", "RTRIM"), 0)
        self.assertIsNone(_cmp_value("a", "b", "MYCOLL"))


class IndexCarveTest(IndexTestBase):
    def test_deleted_entries_are_recovered_with_their_values(self):
        s = self.open(ff.indexed_deleted(self.tmp))
        res = s.forensics.carve()
        self.assertTrue(res.complete)
        self.assertEqual(res.stats["indexes"], 4)
        self.assertEqual(res.stats["index_entries"], len(self.entries(res)))
        least = {"m_name": 20, "m_age_name": 60, "m_city_email": 60,
                 "sqlite_autoindex_members_1": 25}
        for index, n in least.items():
            got = self.entries(res, index)
            exact = set(r.rowid for r in got if r.rowid in ff.MEMBERS_DELETED
                        and r.values == ff.member_entry(index, r.rowid))
            self.assertGreaterEqual(len(exact), n, index)
            for r in got:
                self.assertEqual(r.table, "members")
                self.assertEqual(r.columns[-1], "rowid")
                self.assertIn("index_entry", r.flags)
                self.assertEqual(r.rowid, r.values[-1])
                if r.confidence != LOW:
                    self.assertEqual(r.values, ff.member_entry(index, r.rowid), r)
        # the plain index's entries on its freed pages are intact
        self.assertTrue(any(r.source == "freelist" and r.confidence == HIGH
                            for r in self.entries(res, "m_name")))

    def test_live_entries_are_not_reported(self):
        s = self.open(ff.indexed_deleted(self.tmp))
        res = s.forensics.carve()
        live = {}
        for index in ff.MEMBER_INDEXES:
            live[index] = set(tuple(value_key(v) for v in ff.member_entry(index, i))
                              for i in range(1, 401) if i not in ff.MEMBERS_DELETED)
        self.assertGreater(res.stats["live_skipped"], 0)
        for r in self.entries(res):
            self.assertNotIn(tuple(value_key(v) for v in r.values), live[r.index])

    def test_links_to_recovered_table_records(self):
        s = self.open(ff.indexed_deleted(self.tmp))
        res = s.forensics.carve()
        linked = [r for r in self.entries(res) if "table_record_found" in r.flags]
        self.assertTrue(linked)
        rows = set(r.rowid for r in res if r.index is None and r.table == "members")
        for r in linked:
            self.assertIn(r.rowid, rows)
        tables = [r for r in res if r.index is None and r.table == "members"
                  and any(x.startswith("index ") for x in r.reasons)]
        self.assertTrue(tables)

    def test_rows_that_left_only_their_index_entries(self):
        s = self.open(ff.indexed_rows_gone(self.tmp))
        res = s.forensics.carve()
        got = self.entries(res, "docs_tag")
        self.assertGreaterEqual(len(got), 3)
        for r in got:
            self.assertIn(r.rowid, ff.TAGGED_GONE)
            self.assertEqual(r.values, [ff.doc(r.rowid)[1], r.rowid])
            self.assertIn("row_gone", r.flags)
            self.assertTrue(any("what is left" in x for x in r.reasons))
        # the table rows themselves are really gone
        self.assertFalse([r for r in res if r.index is None and r.table == "docs"
                          and r.rowid in ff.TAGGED_GONE and r.values[1] == ff.doc(r.rowid)[1]])

    def test_entries_from_older_page_copies(self):
        s = self.open(ff.indexed_wal(self.tmp))
        res = s.forensics.carve()
        got = self.entries(res, "m_name")
        by_rowid = dict((r.rowid, r) for r in got)
        self.assertEqual(by_rowid[5].values, ["member 5", 5])
        self.assertEqual(by_rowid[7].values, ["member 7", 7])
        self.assertEqual(by_rowid[9].values, ["member 9", 9])
        self.assertIn("prior_version", by_rowid[9].flags)
        for r in got:
            places = [r.prov] + r.copies
            self.assertTrue(set(p.source for p in places) <= {"replaced", "wal"}, places)
            self.assertIn(r.confidence, (HIGH, MEDIUM))
        self.assertTrue(any(p.file == "wal" for r in got for p in [r.prov] + r.copies))
        self.assertNotIn("renamed 9", [r.values[0] for r in got])

    def test_secure_delete_leaves_no_entries(self):
        s = self.open(ff.indexed_deleted(self.tmp, secure_delete=True))
        self.assertEqual(self.entries(s.forensics.carve()), [])

    def test_can_be_turned_off_and_follows_the_table_filter(self):
        s = self.open(ff.indexed_deleted(self.tmp))
        self.assertEqual(self.entries(s.forensics.carve(index_entries=False)), [])
        self.assertTrue(self.entries(s.forensics.carve(tables=["members"])))
        self.assertEqual(self.entries(s.forensics.carve(tables=["sqlite_sequence"])), [])

    def test_records_serialise_and_ids_are_stable(self):
        path = ff.indexed_deleted(self.tmp)
        s = self.open(path)
        res = s.forensics.carve()
        for r in self.entries(res):
            d = r.as_dict()
            self.assertEqual(d["index"], r.index)
            self.assertIn("[index %s]" % r.index, r.label)
        again = Session.open(path, hash_evidence=False)
        try:
            self.assertEqual([r.id for r in again.forensics.carve()], [r.id for r in res])
        finally:
            again.close()

    def test_hostile_pages_do_not_raise(self):
        for seed in (3, 7, 11):
            s = self.open(ff.hostile(self.tmp, seed=seed))
            res = s.forensics.carve(time_limit=60)
            self.assertIsNotNone(res.stats)
        path = ff.indexed_deleted(self.tmp)
        with open(path, "rb") as f:
            data = bytearray(f.read())
        import random
        rnd = random.Random(5)
        for i in range(1024, len(data)):
            if rnd.random() < 0.05:
                data[i] = rnd.randrange(256)
        bad = self.tmp + "/hostile_index.db"
        with open(bad, "wb") as f:
            f.write(bytes(data))
        s = self.open(bad)
        res = s.forensics.carve(time_limit=60)
        for r in self.entries(res):
            self.assertIn("index_entry", r.flags)


if __name__ == "__main__":
    unittest.main()
