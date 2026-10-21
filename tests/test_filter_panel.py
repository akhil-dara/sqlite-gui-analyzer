"""The Filter panel: what a column holds (engine.profile, through SQLite and from a list) and
the condition each kind of panel builds - checked against SQLite."""
import os
import sqlite3
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engine import querymodel as qm  # noqa: E402
from engine.profile import month_range, profile_sql, profile_values  # noqa: E402
from engine.timeline import from_utc  # noqa: E402
from tests.helpers import tk_root  # noqa: E402


def ms(*a):
    return from_utc(datetime(*a), "unix_ms")


ROWS = [(1, "SENT", ms(2025, 2, 25, 13, 0), 500), (2, "RECEIVED", ms(2025, 3, 2), 32500),
        (3, "RECEIVED", ms(2025, 3, 30), 50000), (4, None, 0, None),
        (5, "ERRORED", ms(2025, 5, 1), 2.5)]


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.addCleanup(self.c.close)
        self.c.execute("CREATE TABLE tx (id INTEGER PRIMARY KEY, type TEXT, sent INTEGER, "
                       "amount REAL)")
        self.c.executemany("INSERT INTO tx VALUES (?,?,?,?)", ROWS)

    def test_a_column_of_a_few_values(self):
        p = profile_sql(self.c, "tx", "type")
        self.assertEqual((p.total, p.nulls, p.distinct), (5, 1, 4))
        self.assertEqual(p.top[0], ("RECEIVED", 2))
        self.assertEqual(p.looks(), "list")

    def test_a_date_column_by_month_without_guessing_unset_values(self):
        p = profile_sql(self.c, "tx", "sent", "unix_ms")
        self.assertEqual(p.looks(), "date")
        self.assertEqual(p.months, [("2025-02", 1), ("2025-03", 2), ("2025-05", 1)])
        self.assertEqual(p.not_dates, 1)                 # the 0: unset, not 1970
        self.assertEqual(p.first, datetime(2025, 2, 25, 13, 0))
        self.assertEqual(p.top, [])                      # dates are shown by month
        same = profile_values([r[2] for r in ROWS], "unix_ms")
        self.assertEqual(same.months, p.months)
        self.assertEqual(same.not_dates, 1)

    def test_numbers(self):
        p = profile_sql(self.c, "tx", "amount")
        self.assertEqual((p.lo, p.hi, p.numbers), (2.5, 50000, 4))
        self.assertEqual(p.looks(), "list")              # 4 values: still a list to tick
        many = profile_values(list(range(100)))
        self.assertEqual(many.looks(), "number")
        self.assertEqual(profile_values(["a%d" % i for i in range(300)]).looks(), "text")

    def test_month_range(self):
        self.assertEqual(month_range("2025-03", "2025-03"),
                         (datetime(2025, 3, 1), datetime(2025, 3, 31, 23, 59, 59)))
        self.assertEqual(month_range("2024-12", "2025-01")[1], datetime(2025, 1, 31, 23, 59, 59))


class PanelConditionsTest(unittest.TestCase):
    """The conditions the panel builds, run against SQLite."""

    def setUp(self):
        self.root = tk_root(self)
        self.c = sqlite3.connect(":memory:")
        self.addCleanup(self.c.close)
        self.c.execute("CREATE TABLE tx (id INTEGER PRIMARY KEY, type TEXT, sent INTEGER, "
                       "amount REAL)")
        self.c.executemany("INSERT INTO tx VALUES (?,?,?,?)", ROWS)

    def panel(self, col, kind=None, current=None):
        from filter_panel import FilterPanel
        got = {}

        def load(k, done):
            done(profile_sql(self.c, "tx", col, k), None)
        p = FilterPanel(self.root, ("tx", col, "tx." + col), kind, load,
                        lambda f, done: None, lambda f: got.setdefault("f", f),
                        current=current)
        return p, got

    def ids(self, f):
        m = qm.QueryModel()
        a = m.add_table("tx")
        m.toggle_column(a, "id")
        m.filters = [f] if f is not None else []
        return [r[0] for r in self.c.execute(m.to_sql())]

    def test_months_picked_on_the_bars(self):
        p, got = self.panel("sent", "unix_ms")
        p._months_picked("2025-03", "2025-03", mark=True)
        f = p.build()
        self.assertEqual(self.ids(f), [2, 3])
        self.assertEqual(p.bars.sel, (1, 1))
        p.apply()
        self.assertEqual(got["f"].op, "between")

    def test_values_ticked_including_no_value(self):
        p, _got = self.panel("type")
        p.keep = {"RECEIVED", None}
        f = p.build()
        self.assertIsInstance(f, qm.Group)
        self.assertEqual(self.ids(f), [2, 3, 4])
        p._tick_all(True)
        self.assertIsNone(p.build())                     # everything ticked: no filter

    def test_numbers_from_to(self):
        p, _got = self.panel("amount")
        p.profile.distinct = None                        # as for a column of many numbers
        for w in p.body.winfo_children():
            w.destroy()
        p._number_view()
        p.n_from.insert(0, "1,000")
        self.assertEqual(self.ids(p.build()), [2, 3])
        p.n_to.insert(0, "40000")
        self.assertEqual(self.ids(p.build()), [2])
        p.n_from.delete(0, "end")
        p.n_from.insert(0, "x")
        with self.assertRaises(ValueError):
            p.build()

    def test_it_starts_from_the_current_condition(self):
        cur = qm.Filter("tx", "type", "in", "SENT\nERRORED")
        p, _got = self.panel("type", current=cur)
        self.assertEqual(p.keep, {"SENT", "ERRORED"})
        self.assertEqual(self.ids(p.build()), [1, 5])


if __name__ == "__main__":
    unittest.main()
