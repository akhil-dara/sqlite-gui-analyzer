"""Calculations: the steps, what they do to values that do not fit, and calculated columns
in the query (worked out after the query, the stored column unchanged)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engine import calc  # noqa: E402
from engine import querymodel as qm  # noqa: E402


class StepsTest(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(calc.apply(32500, [["div", 100]]), 325)
        self.assertEqual(calc.apply(32550, [["div", 100]]), 325.5)
        self.assertEqual(calc.apply("1,234", [["mul", 2]]), 2468)
        self.assertEqual(calc.apply(2.345, [["round", 2]]), 2.35)
        self.assertEqual(calc.apply(-7.9, [["int"], ["abs"]]), 7)
        self.assertEqual(calc.apply(1234567.891, [["thousands", 2]]), "1,234,567.89")
        self.assertEqual(calc.apply(32550, [["div", 100], ["thousands", 2],
                                            ["prefix", "Rs "]]), "Rs 325.50")

    def test_values_that_do_not_fit_are_left_empty_with_a_reason(self):
        self.assertEqual(calc.explain("abc", [["div", 100]]),
                         (None, "Divide by: 'abc' is not a number"))
        self.assertEqual(calc.explain(5, [["div", 0]])[1], "Divide by: division by 0")
        self.assertEqual(calc.explain(None, [["div", 2]]), (None, "empty"))
        self.assertEqual(calc.apply(None, [["default", "(none)"]]), "(none)")
        self.assertEqual(calc.explain("a@b", [["split", "@", 3]])[1],
                         "Piece of the text (split at): no piece 3")

    def test_text(self):
        self.assertEqual(calc.apply("8125458944-1@axl", [["split", "@", 1]]), "8125458944-1")
        self.assertEqual(calc.apply("+91 81254 58944", [["digits"], ["right", 10]]),
                         "8125458944")
        self.assertEqual(calc.apply("  ab cd ", [["trim"], ["title"]]), "Ab Cd")
        self.assertEqual(calc.apply("abcdef", [["mid", 2, 3]]), "bcd")
        self.assertEqual(calc.apply("a-b", [["replace", "-", "+"]]), "a+b")
        self.assertEqual(calc.apply(b"hi", [["upper"]]), "HI")

    def test_with_another_column(self):
        row = {"b": 4, "n": None, "t": "x"}.get
        self.assertEqual(calc.apply(10, [["col_div", "b"]], row), 2.5)
        self.assertEqual(calc.apply(10, [["col_sub", "b"]], row), 6)
        self.assertEqual(calc.apply("A", [["col_join", "t", " / "]], row), "A / x")
        self.assertEqual(calc.apply("A", [["col_join", "n", " / "]], row), "A")
        self.assertEqual(calc.apply(None, [["coalesce", "t"]], row), "x")
        self.assertEqual(calc.columns_needed([["col_div", "b"], ["div", 2]]), ["b"])

    def test_every_operation_has_a_name_a_group_and_defaults_that_run(self):
        for key, group, label, settings, _fn, _w in calc.OPS:
            self.assertTrue(label and group)
            st = calc.new_step(key)
            self.assertEqual(len(st), 1 + len(settings))
            calc.explain("12", [st], {"": 1}.get)       # never raises
            self.assertTrue(calc.step_text(st))

    def test_settings_typed_in_boxes(self):
        self.assertEqual(calc.coerce_setting("number", "1,000"), 1000)
        self.assertEqual(calc.coerce_setting("int", " 3 "), 3)
        with self.assertRaises(ValueError):
            calc.coerce_setting("number", "x")


class ColumnsTest(unittest.TestCase):
    def test_a_calculated_column_follows_its_source_and_saves(self):
        m = qm.QueryModel()
        a = m.add_table("tx")
        m.toggle_column(a, "amount")
        src = m.columns[0]
        c = m.add_calc(src, [["div", 100]], "Rupees")
        self.assertEqual(m.columns, [src, c])
        self.assertTrue(m.has_column(a, "amount"))
        self.assertEqual(m.headings(), ["amount", "Rupees"])
        self.assertIn('"tx"."amount" AS "Rupees"', m.to_sql())
        self.assertIsNone(qm.Column("t", "x", agg="sum", calc=[["div", 2]]).agg)
        back = qm.QueryModel.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(back.columns[1].calc, [["div", 100]])
        self.assertIn("Rupees: tx.amount, Divide by 100", qm.describe(m))

    def test_rows_are_worked_out_in_column_order(self):
        from query_tab import apply_calcs
        rows = [(32500, 32500, 2), ("x", "x", 2)]
        got = apply_calcs(rows, [(1, [["div", 100]]), (2, [["col_mul", "rupees"]])],
                          {"amount": 0, "rupees": 1, "times": 2})
        self.assertEqual(got, [[32500, 325, 650], ["x", None, None]])


if __name__ == "__main__":
    unittest.main()
