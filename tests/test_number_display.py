"""Browse 'Show as number': a stored amount drawn divided by 100 with a badge, the stored
value untouched, remembered per table and column, and added as its own column to exports."""
import os
import sqlite3
import unittest

from tests.test_review_ui import AppCase


def make(tmp):
    path = os.path.join(tmp, "amounts.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE txn (id INTEGER PRIMARY KEY, amount INTEGER, note TEXT)")
    con.executemany("INSERT INTO txn VALUES (?, ?, ?)",
                    [(1, 34000, "a"), (2, 34005, "b"), (3, -1250, "c"), (4, None, "d"),
                     (5, 1234567, "e")])
    con.commit()
    con.close()
    return path


class DecimalsTextTest(unittest.TestCase):
    def test_exact_integer_scaling(self):
        from number_display import decimals_text, formatter
        self.assertEqual(decimals_text(34005, 100, 2), "340.05")
        self.assertEqual(decimals_text(-1250, 100, 2), "-12.50")
        self.assertEqual(decimals_text(1234567, 1, 0), "1,234,567")
        self.assertEqual(decimals_text(123456789, 1000000, 6), "123.456789")
        f = formatter("c100")
        self.assertEqual(f(34000), "340.00")
        self.assertEqual(f("34000"), "340.00")     # a numeric text
        self.assertIsNone(f("abc"))
        self.assertIsNone(f(b"\x00"))
        self.assertIsNone(f(True))
        self.assertEqual(formatter("k")(1.5), "0.002")


class BrowseNumbersTest(AppCase):
    def test_scaled_column_badge_memory_and_export(self):
        path = make(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._nb.select(app._browse_frame)
            app._browse_table_var.set("txn")
            app._load_browse_table()
            self.pump(lambda: False, 0.5)
            g = app._browse_grid
            c = g.columns().index("amount")
            app._browse_numbers.set("txn", "amount", "c100")
            self.assertEqual(g.display_text(34005, c), "340.05")
            self.assertEqual(g.display_text("x", c), "x")
            self.assertIn("÷100", g._header_text(c))
            # the stored value is what the source holds
            vals = [v for v, _f in g.source.rows(0, 5)]
            self.assertEqual(vals[0][c], 34000)
            # remembered: loading the table again applies it
            app._load_browse_table()
            self.pump(lambda: False, 0.5)
            c = app._browse_grid.columns().index("amount")
            self.assertIsNotNone(app._browse_grid.column_formatter(c))
            # exports add a column beside the stored one
            extra = app._browse_numbers.export_columns("txn", ["id", "amount", "note"])
            self.assertEqual([(i, n) for i, n, _f in extra], [(1, "amount (÷100)")])
            self.assertEqual(extra[0][2](34005), "340.05")
            # off again: no formatter, nothing remembered
            app._browse_numbers.set("txn", "amount", None)
            self.assertIsNone(app._browse_grid.column_formatter(c))
            self.assertEqual(app._browse_numbers.saved("txn"), {})
        self.run_app(scenario)


if __name__ == "__main__":
    unittest.main()
