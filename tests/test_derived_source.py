"""Derived columns in Browse: columns the tool computes from a BLOB / JSON column (fields
inside it), after the table's own; the engine still serves the table, the derived values are
sorted and filtered in memory (up to the limit derived_scan_rows)."""
import json
import os
import sqlite3
import unittest

from tests.helpers import TempDirTest
from browse_sources import DerivedColumn, DerivedSource, TableSource
from database import DB, RID
from engine import limits
from engine.backends import Filter
from engine.decode import fields as fl


def make(tmp, n=300):
    path = os.path.join(tmp, "derived.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE txn (id INTEGER PRIMARY KEY, kind TEXT, data TEXT)")
    for i in range(1, n + 1):
        data = json.dumps({"from": {"name": "Person %03d" % (i % 17)}, "amount": i * 10,
                           "tags": ["a", "b"]}) if i % 5 else "not json"
        con.execute("INSERT INTO txn VALUES (?, ?, ?)", (i, "SENT" if i % 2 else "RECEIVED",
                                                         data))
    con.commit()
    con.close()
    return path


class DerivedSourceTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.db = DB()
        self.db.open(make(self.tmp))
        self.addCleanup(self.db.close)
        inner = TableSource(self.db, "txn", total=300)
        self.src = DerivedSource(inner, [
            DerivedColumn("ƒ sender", "data", lambda v: fl.read(v, ["from.name"])[0]),
            DerivedColumn("ƒ amount", "data", lambda v: fl.read(v, ["amount"])[0])])

    def test_columns_and_rows_carry_the_derived_values(self):
        self.assertEqual(self.src.columns(), [RID, "id", "kind", "data", "ƒ sender", "ƒ amount"])
        rows = self.src.rows(0, 5)
        self.assertEqual(len(rows), 5)
        values = rows[0][0]
        self.assertEqual(values[1], 1)
        self.assertEqual(values[4], "Person 001")
        self.assertEqual(values[5], 10)
        self.assertEqual(self.src.rows(4, 1)[0][0][4], None)      # 'not json': no field
        self.assertEqual(self.src.row_count(), 300)

    def test_sort_and_filter_on_a_derived_column_happen_in_memory(self):
        self.src.sort("ƒ amount", True)
        # row 300 holds no JSON ('not json'): its derived amount is NULL and sorts last
        self.assertEqual([r[0][5] for r in self.src.rows(0, 3)], [2990, 2980, 2970])
        self.src.set_filters({"ƒ sender": "Person 003"}, "")
        rows = list(self.src.iter_rows())
        self.assertTrue(rows)
        self.assertTrue(all(r[4] == "Person 003" for r in rows))
        self.assertEqual(self.src.row_count(), len(rows))
        self.assertTrue(self.src.filtered)
        # back to the engine when no derived column is sorted or filtered
        self.src.set_filters({"kind": "SENT"}, "")
        self.src.sort("id", False)
        self.assertIsNone(self.src._mem)
        self.assertTrue(all(r[0][2] == "SENT" for r in self.src.rows(0, 20)))

    def test_a_filter_on_a_real_column_still_narrows_the_engine_read(self):
        self.src.set_filters({"kind": "SENT", "ƒ sender": "Person 003"}, "")
        rows = list(self.src.iter_rows())
        self.assertTrue(rows)
        self.assertTrue(all(r[2] == "SENT" and r[4] == "Person 003" for r in rows))

    def test_the_memory_limit_is_said(self):
        old = limits.get("derived_scan_rows")
        limits.set_for_test("derived_scan_rows", 1000) if hasattr(limits, "set_for_test") \
            else None
        self.src.sort("ƒ amount", False)
        self.assertEqual(self.src.note, "")
        self.src._cut = 100
        self.assertIn("first 100 rows only", self.src.note)
        self.assertIn("derived_scan_rows", self.src.note)
        self.src._cut = None
        self.assertEqual(limits.get("derived_scan_rows"), old if not hasattr(
            limits, "set_for_test") else 1000)

    def test_popover_helpers_answer_for_derived_columns(self):
        d = self.src.distinct_values("ƒ sender", None, 50, 10000)
        names = [v for v, _n in getattr(d, "values", d)] if not isinstance(d, list) else d
        self.assertTrue(names)
        n = self.src.count_matching(Filter(col_exprs={"ƒ sender": "Person 003"}), None)
        self.assertGreater(n, 0)
        self.assertEqual(self.src.count_matching(Filter(col_exprs={"kind": "SENT"}), None), 150)

    def test_exports_see_the_derived_values(self):
        rows = list(self.src.iter_all())
        self.assertEqual(len(rows), 300)
        self.assertEqual(rows[0][4], "Person 001")


if __name__ == "__main__":
    unittest.main()
