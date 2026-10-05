import os
import sqlite3
import unittest

from tests.helpers import TempDirTest
from tests.fixtures import make_fixtures as fx
from engine.fileformat.pager import Pager
from engine.schema import (Locator, SchemaModel, collation_names, column_affinity,
                           rename_create_table, replace_collations)


class RenameCreateTableTest(unittest.TestCase):
    def test_forms(self):
        cases = [
            ("CREATE TABLE foo(a)", 'CREATE TABLE "t"(a)'),
            ('CREATE TABLE "we""ird"(a)', 'CREATE TABLE "t"(a)'),
            ("CREATE TABLE [br ack](a)", 'CREATE TABLE "t"(a)'),
            ("CREATE TABLE `bt`(a)", 'CREATE TABLE "t"(a)'),
            ("CREATE TABLE IF NOT EXISTS main.x (a)", 'CREATE TABLE IF NOT EXISTS "t" (a)'),
            ("create temp table x(a)", 'create temp table "t"(a)'),
            ("CREATE VIRTUAL TABLE f USING fts5(body)", 'CREATE VIRTUAL TABLE "t" USING fts5(body)'),
            ("CREATE TABLE sqlite_sequence(name,seq)", 'CREATE TABLE "t"(name,seq)'),
        ]
        for sql, want in cases:
            self.assertEqual(rename_create_table(sql), want, sql)
        self.assertIsNone(rename_create_table("CREATE VIEW v AS SELECT 1"))


class HelpersTest(unittest.TestCase):
    def test_collation_names(self):
        sql = ['CREATE TABLE a(x TEXT COLLATE NOCASE, y COLLATE "UNICODE_en-US_LINGUISTIC_IGNORECASE")',
               "CREATE INDEX i ON a(x COLLATE [MyColl])"]
        self.assertEqual(collation_names(sql), {"UNICODE_en-US_LINGUISTIC_IGNORECASE", "MyColl"})

    def test_replace_collations_only_rewrites_the_named_ones(self):
        sql = 'CREATE TABLE "t"(a TEXT COLLATE "X-Y.z", b COLLATE [MyColl], c COLLATE x-y.z)'
        self.assertEqual(replace_collations(sql, {"X-Y.z", "x-y.z"}),
                         'CREATE TABLE "t"(a TEXT COLLATE NOCASE, b COLLATE [MyColl], '
                         'c COLLATE NOCASE)')
        self.assertEqual(replace_collations(sql, set()), sql)

    def test_affinity_rules(self):
        cases = {"INTEGER": "INTEGER", "BIGINT": "INTEGER", "VARCHAR(10)": "TEXT", "CLOB": "TEXT",
                 "BLOB": "BLOB", "": "BLOB", "REAL": "REAL", "DOUBLE PRECISION": "REAL",
                 "FLOAT": "REAL", "NUMERIC": "NUMERIC", "DATETIME": "NUMERIC", "POINT": "INTEGER"}
        for decl, want in cases.items():
            self.assertEqual(column_affinity(decl), want, decl)

    def test_locator_identity_ignores_cell(self):
        self.assertEqual(Locator("rowid", 3, (5, 100)), Locator("rowid", 3))
        self.assertEqual(Locator("pk", ("a", 1)).display(), "pk=('a', 1)")
        self.assertEqual(Locator("pk", (b"\x01",)).display(), "pk=b'\\x01'")
        self.assertEqual(str(Locator("ordinal", 4)), "#4")


class SchemaModelTest(TempDirTest):
    def load(self, path):
        pager = Pager(path)
        self.addCleanup(pager.close)
        return SchemaModel.load(pager)

    def test_without_rowid_storage_order_puts_pk_first(self):
        s = self.load(fx.without_rowid(self.tmp))
        t = s.get("pk_last")
        self.assertTrue(t.without_rowid)
        self.assertEqual(t.column_names, ["a", "b", "c"])
        self.assertEqual(t.pk_columns, [2, 0])          # PRIMARY KEY(c, a)
        self.assertEqual(t.storage_order, [2, 0, 1])
        self.assertEqual(t.locator_kind, "pk")
        self.assertEqual(t.pk_index_order, [(False, "BINARY"), (False, "BINARY")])
        self.assertEqual(s.get("desc_pk").pk_index_order, [(True, "BINARY")])   # PRIMARY KEY(k DESC)

    def test_rowid_names_and_aliases(self):
        s = self.load(fx.quirks(self.tmp))
        self.assertIsNone(s.get("shadow").rowid_name)            # rowid, _rowid_, oid all taken
        self.assertEqual(s.get("partial_shadow").rowid_name, "_rowid_")
        self.assertEqual(s.get("altered").rowid_alias, 0)       # INTEGER PRIMARY KEY
        self.assertIsNone(s.get("ipk_desc").rowid_alias)        # INTEGER PRIMARY KEY DESC quirk

    def test_alter_defaults_fill_old_rows(self):
        t = self.load(fx.quirks(self.tmp)).get("altered")
        row, flags = t.record_to_row(1, [None, "old row"])
        self.assertEqual(row, [1, "old row", "dflt"])
        self.assertIn("pre_alter", flags)

    def test_damaged_record_is_not_filled_with_defaults(self):
        t = self.load(fx.quirks(self.tmp)).get("altered")
        row, flags = t.record_to_row(1, [None, "old row"], damaged=True)
        self.assertEqual(row, [1, "old row", None])       # not the DEFAULT 'dflt'
        self.assertEqual(flags, {"damaged_record"})
        row, flags = t.record_to_row(7, [], damaged=True)   # header length 0: all-NULL row
        self.assertEqual(row, [7, None, None])

    def test_only_constant_defaults_are_evaluated(self):
        path = os.path.join(self.tmp, "defaults.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE d(id INTEGER PRIMARY KEY, s TEXT DEFAULT 'it''s', "
                  "ts TEXT DEFAULT CURRENT_TIMESTAMP, td TEXT DEFAULT CURRENT_DATE, "
                  "tt TEXT DEFAULT CURRENT_TIME, fn TEXT DEFAULT (datetime('now')), "
                  "rnd INTEGER DEFAULT (random()), n INTEGER DEFAULT -5, r REAL DEFAULT +1.5e3, "
                  "h INTEGER DEFAULT 0x10, b BLOB DEFAULT x'00ff', z DEFAULT NULL, "
                  "tr INTEGER DEFAULT TRUE, nd TEXT)")
        c.close()
        t = self.load(path).get("d")
        self.assertEqual(t.defaults, [None, "it's", None, None, None, None, None, -5, 1500.0,
                                      16, b"\x00\xff", None, 1, None])

    def test_real_affinity_restores_float(self):
        t = self.load(fx.quirks(self.tmp)).get("real_aff")
        row, _ = t.record_to_row(1, [None, 2])
        self.assertEqual(row[1], 2.0)
        self.assertIsInstance(row[1], float)

    def test_collations_and_views(self):
        s = self.load(fx.quirks(self.tmp))
        self.assertEqual(s.collations, {"MYCOLL"})
        self.assertEqual(s.get("v_shadow").kind, "view")
        self.assertEqual(len(s.get("wide").columns), 250)


if __name__ == "__main__":
    unittest.main()
