"""Conditions of the Query tab: every SQLite comparison in plain words, run against SQLite
itself (so the SQL shown is SQL that works and finds the right rows)."""
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engine import querymodel as qm  # noqa: E402
from engine.timeline import from_utc  # noqa: E402

ROWS = [(1, "Ann", "RECEIVED", 32500, from_utc(datetime(2024, 11, 21, 7, 51), "unix_ms"), 10),
        (2, "bob", "SENT", 50000, from_utc(datetime(2024, 11, 22, 9, 0), "unix_ms"), 50000),
        (3, None, "SENT", 200, from_utc(datetime(2025, 1, 1, 0, 0), "unix_ms"), 0),
        (4, "", "MANDATE", None, None, 1),
        (5, "Annabel", "RECEIVED", 11400, from_utc(datetime(2024, 11, 21, 23, 59), "unix_ms"),
         11400)]


class ConditionsTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.execute("CREATE TABLE tx (id INTEGER PRIMARY KEY, name TEXT, type TEXT, "
                       "amount INTEGER, sent INTEGER, other INTEGER)")
        self.c.executemany("INSERT INTO tx VALUES (?,?,?,?,?,?)", ROWS)
        self.addCleanup(self.c.close)

    def ids(self, *filters, match="all", model=None):
        m = model or qm.QueryModel()
        if model is None:
            a = m.add_table("tx")
            m.toggle_column(a, "id")
        m.filters = list(filters)
        m.match = match
        sql = m.to_sql()
        return [r[0] for r in self.c.execute(sql)]

    def f(self, *a, **k):
        return qm.Filter("tx", *a, **k)

    def test_comparisons(self):
        self.assertEqual(self.ids(self.f("amount", ">", "20000")), [1, 2])
        self.assertEqual(self.ids(self.f("amount", "<=", "11400")), [3, 5])
        self.assertEqual(self.ids(self.f("type", "!=", "SENT")), [1, 4, 5])
        self.assertEqual(self.ids(self.f("amount", "between", "200", "11400")), [3, 5])
        self.assertEqual(self.ids(self.f("amount", "not between", "200", "11400")), [1, 2])

    def test_lists_patterns_and_capitals(self):
        self.assertEqual(self.ids(self.f("type", "in", "SENT, MANDATE")), [2, 3, 4])
        self.assertEqual(self.ids(self.f("type", "not in", "SENT\nMANDATE")), [1, 5])
        self.assertEqual(self.ids(self.f("name", "contains", "ann")), [1, 5])   # LIKE: no case
        self.assertEqual(self.ids(self.f("name", "not contains", "ann")), [2, 4])
        self.assertEqual(self.ids(self.f("name", "like", "A_n")), [1])
        self.assertEqual(self.ids(self.f("name", "glob", "A*")), [1, 5])
        self.assertEqual(self.ids(self.f("name", "glob", "a*")), [])            # capitals count
        self.assertEqual(self.ids(self.f("name", "=", "BOB")), [])
        self.assertEqual(self.ids(self.f("name", "=", "BOB", nocase=True)), [2])

    def test_no_value_and_empty_are_told_apart(self):
        self.assertEqual(self.ids(self.f("name", "is null")), [3])
        self.assertEqual(self.ids(self.f("name", "is not null")), [1, 2, 4, 5])
        self.assertEqual(self.ids(self.f("name", "is empty")), [3, 4])
        self.assertEqual(self.ids(self.f("name", "is not empty")), [1, 2, 5])

    def test_dates_are_typed_as_dates_and_compared_as_stored(self):
        on_21 = self.f("sent", "<=", "2024-11-21", date="unix_ms")   # the whole day counts
        self.assertEqual(self.ids(on_21), [1, 5])
        self.assertEqual(self.ids(self.f("sent", ">", "2024-11-21", date="unix_ms")), [2, 3])
        self.assertEqual(self.ids(self.f("sent", "between", "2024-11-21 08:00",
                                         "2024-11-22", date="unix_ms")), [2, 5])
        self.assertIn("dates in UTC, stored as Unix milliseconds",
                      self.f("sent", ">", "2024-11-21", date="unix_ms").text())
        with self.assertRaises(ValueError):
            self.f("sent", ">", "21/11/2024", date="unix_ms").sql()

    def test_another_column(self):
        self.assertEqual(self.ids(self.f("amount", "=", other="tx.other")), [2, 5])
        self.assertEqual(self.ids(self.f("amount", ">", other="tx.other")), [1, 3])

    def test_any_and_groups(self):
        self.assertEqual(self.ids(self.f("type", "=", "SENT"), self.f("amount", ">", "40000"),
                                  match="any"), [2, 3])
        g = qm.Group("any", [self.f("type", "=", "MANDATE"), self.f("name", "is null")])
        self.assertEqual(self.ids(g), [3, 4])
        self.assertEqual(self.ids(g, self.f("type", "=", "SENT")), [3])
        self.assertIn("any of: tx.type is 'MANDATE'; tx.name has no value", g.text())

    def test_summaries_offset_and_saving(self):
        m = qm.QueryModel()
        a = m.add_table("tx")
        m.toggle_column(a, "type")
        m.toggle_column(a, "amount")
        m.columns[1].agg = "count"
        heading = m.headings()[1]
        m.filters = [qm.Filter(a, "amount", ">=", "2", summary=heading)]
        sql = m.to_sql()
        self.assertIn("HAVING COUNT", sql)
        self.assertEqual(sorted(r[0] for r in self.c.execute(sql)), ["RECEIVED", "SENT"])
        m2 = qm.QueryModel()
        a2 = m2.add_table("tx")
        m2.toggle_column(a2, "id")
        m2.sort = [(a2, "id", False)]
        m2.offset = 2
        self.assertEqual([r[0] for r in self.c.execute(m2.to_sql())], [3, 4, 5])
        m2.limit = 2
        self.assertEqual([r[0] for r in self.c.execute(m2.to_sql())], [3, 4])
        m.filters.append(qm.Group("any", [qm.Filter(a, "type", "in", "A, B", nocase=True)]))
        m.match = "any"
        back = qm.QueryModel.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(back.to_sql(), m.to_sql())
        old = qm.QueryModel.from_dict({"tables": [["tx", "tx"]],
                                       "filters": [["tx", "type", "=", "SENT"]]})
        self.assertIn("WHERE \"tx\".\"type\" = 'SENT'", old.to_sql())


if __name__ == "__main__":
    unittest.main()
