"""Formulas (engine.formula): what they can do, what they refuse, and how empty cells are
explained; the Formula step of engine.calc."""
import unittest

import tests.helpers  # noqa: F401 - the src path

from engine import calc
from engine import formula as fm


def ev(text, x=None, row=None):
    row = row or {}
    return fm.compile_formula(text).eval(x, lambda n: row[n])


class FormulaTest(unittest.TestCase):
    def test_numbers_and_text(self):
        self.assertEqual(ev("x / 100", 34005), 340.05)
        self.assertEqual(ev("x / 100", "34005"), 340.05)       # a numeric text
        self.assertEqual(ev("round(x / 100, 2)", 34005), 340.05)
        self.assertEqual(ev("thousands(x / 100, 2)", 1234567), "12,345.67")
        self.assertEqual(ev("x * 1.18", 100), 118)             # a whole float reads as int
        self.assertEqual(ev("-x + 1", 5), -4)
        self.assertEqual(ev("x ** 2", 12), 144)
        self.assertEqual(ev("x % 7", 10), 3)
        self.assertEqual(ev("upper(left(x, 3))", "kadambari"), "KAD")
        self.assertEqual(ev("mid(x, 2, 3)", "abcdef"), "bcd")
        self.assertEqual(ev("right(x, 2)", "abcdef"), "ef")
        self.assertEqual(ev("x[0:2] + '-' + x[-2:]", "abcdef"), "ab-ef")
        self.assertEqual(ev("split(x, '@', 2)", "a@b.com"), "b.com")
        self.assertEqual(ev("digits(x)", "+91 (98) 765"), "9198765")
        self.assertEqual(ev("len(x)", "abc"), 3)
        self.assertEqual(ev("'Mr ' + x", "X"), "Mr X")
        self.assertEqual(ev("x + 1", "2"), 3)                  # numeric texts add as numbers

    def test_conditions_and_other_columns(self):
        self.assertEqual(ev("x if x > 0 else 0", -5), 0)
        self.assertEqual(ev("iif(x >= 100, 'big', 'small')", 100), "big")
        self.assertTrue(ev("contains(x, 'ENRICH')", "P2P_ENRICHMENT"))
        self.assertTrue(ev("'ENRICH' in x", "P2P_ENRICHMENT"))
        self.assertEqual(ev("coalesce(x, nick, '-')", None, {"nick": "n"}), "n")
        self.assertEqual(ev("x * qty + fee", 10, {"qty": 3, "fee": 0.5}), 30.5)
        self.assertEqual(ev('col("Full name")', 1, {"Full name": "A B"}), "A B")
        f = fm.compile_formula("x * qty + col(\"Full name\")")
        self.assertEqual(f.names, ["qty", "Full name"])
        self.assertEqual(ev("x == None", None), True)
        self.assertEqual(ev("x != None", None), False)
        self.assertEqual(ev("1 < x <= 3", 3), True)

    def test_dates_and_json(self):
        self.assertEqual(ev('date(x, "ms")', 1740753705766), "2025-02-28 14:41:45.766")
        self.assertEqual(ev('date(x, "s")', 1740753705), "2025-02-28 14:41:45")
        self.assertEqual(ev('json(x, "to.name")', '{"to": {"name": "Grocer"}}'), "Grocer")
        self.assertEqual(ev("hex(x)", 255), "ff")
        self.assertEqual(ev("hex(x)", b"\x00\xff"), "00ff")

    def test_sizes_durations_dates_more(self):
        self.assertEqual(ev("size(x)", 1536), "1.5 KB")
        self.assertEqual(ev("size(x)", 27300000), "26.0 MB")
        self.assertEqual(ev("size(x)", 512), "512 B")
        self.assertEqual(ev('size(x, "MB")', 27300000), 26.035)
        self.assertEqual(ev("size(x)", b"\x00" * 2048), "2.0 KB")
        self.assertEqual(ev('duration(x, "ms")', 3723000), "01:02:03")
        self.assertEqual(ev("duration(x)", 90061), "1d 01:01:01")
        self.assertEqual(ev('duration(x, "min")', 2.5), "00:02:30")
        self.assertEqual(ev('date(x, "ms", "UTC+05:30")', 1740753705766),
                         "2025-02-28 20:11:45.766")
        self.assertEqual(ev('dateformat(x, "ms", "%d %b %Y")', 1740753705766), "28 Feb 2025")
        self.assertEqual(ev('year(x, "ms")', 1740753705766), 2025)
        self.assertEqual(ev('weekday(x, "ms")', 1740753705766), "Friday")
        self.assertEqual(ev('hour(x, "ms", "+0530")', 1740753705766), 20)
        self.assertEqual(ev('quarter(x, "ms")', 1740753705766), 1)
        self.assertEqual(ev('datediff(x, later, "ms", "h")', 1740753705766,
                            {"later": 1740753705766 + 7200000}), 2)
        self.assertEqual(ev('unix(x, "ms", "s")', 1740753705766), 1740753705.766)
        self.assertEqual(ev('unix(x, "s", "webkit")', 1740753705), 13385227305000000)
        self.assertEqual(ev("md5(x)", "abc"), "900150983cd24fb0d6963f7d28e17f72")
        self.assertEqual(ev("sha256(x)", b"abc")[:12], "ba7816bf8f01")
        self.assertEqual(ev("base64(x)", "hi"), "aGk=")
        self.assertEqual(ev("unbase64(x)", "aGk="), "hi")
        self.assertEqual(ev("fromhex(x)", "68 69"), "hi")
        self.assertEqual(ev("utf8(x)", b"caf\xc3\xa9"), "caf\u00e9")
        self.assertEqual(ev("urldecode(x)", "a%20b+c"), "a b c")
        self.assertEqual(ev(r'regex(x, "R(\d+)_", 1)', "R102_P2P"), "102")
        self.assertTrue(ev(r'matches(x, "^T\d+$")', "T2502"))
        self.assertEqual(ev('lpad(x, 5, "0")', 42), "00042")
        self.assertEqual(ev("wordcount(x)", "a b  c"), 3)
        self.assertEqual(ev("percent(x, total)", 25, {"total": 200}), 12.5)
        self.assertEqual(ev("kind(x)", None), "NULL")
        self.assertEqual(ev("kind(x)", 1.5), "REAL")
        import plistlib
        blob = plistlib.dumps({"to": {"name": "Grocer"}}, fmt=plistlib.FMT_BINARY)
        self.assertIn("plist", ev("kind(x)", blob).lower())
        self.assertEqual(ev('field(x, "a.b")', '{"a": {"b": 7}}'), 7)
        self.assertEqual(ev("decoded(x)", '{"a": 1}'), '{"a": 1}')
        self.assertTrue(ev("isnull(x)", None))
        self.assertEqual(ev("sign(x)", -3), -1)
        with self.assertRaises(fm.NotApplicable):
            ev('size(x, "lightyears")', 1)
        with self.assertRaises(fm.NotApplicable):
            ev('regex(x, "(", 0)', "a")
        with self.assertRaises(fm.NotApplicable):
            ev('regex(x, "z", 0)', "a")
        with self.assertRaises(fm.NotApplicable):
            ev('date(x, "ms", "Mars")', 1)
        with self.assertRaises(fm.NotApplicable):
            ev("decoded(x)", b"\x00\x01")
        # a plist read like JSON (the one decoder)
        self.assertEqual(ev('json(x, "to.name")', blob), "Grocer")
        self.assertEqual(ev('field(x, "to.name")', blob), "Grocer")
        # every function named in the help exists
        import re
        for names, _what in fm.FUNCTION_HELP:
            for part in re.sub(r"\([^)]*\)", "", names).split(","):
                name = part.strip().split("(")[0].strip()
                if name and name.isidentifier() and name not in ("x", "and", "or", "not",
                                                                  "in", "col"):
                    self.assertIn(name, fm.FUNCS, name)

    def test_refused_and_not_applicable(self):
        for bad in ("import os", "x.__class__", "__import__('os')", "open('f')",
                    "lambda: 1", "[1, 2]", "{1: 2}", "x = 1", "f'{x}'", "(x for x in y)",
                    "x if", "col(name)", "unknown(x)", "", "x @ y"):
            self.assertIsNotNone(fm.check(bad), bad)
        self.assertIn("character", fm.check("x +"))
        with self.assertRaises(fm.NotApplicable):
            ev("x / 0", 1)
        with self.assertRaises(fm.NotApplicable):
            ev("x / 100", "abc")
        with self.assertRaises(fm.NotApplicable):
            ev("x ** 1000", 2)
        with self.assertRaises(fm.NotApplicable):
            ev("x * 100000", "a")
        with self.assertRaises(fm.NotApplicable):
            ev("upper", "a")                                  # a function not called
        with self.assertRaises(fm.NotApplicable):
            ev("x + 1", None)
        with self.assertRaises(fm.NotApplicable):
            ev("missing + 1", 1, {})
        self.assertIsNone(fm.check("x" * 1))
        self.assertIsNotNone(fm.check("x+" * 1200 + "1"))     # too long

    def test_calc_step(self):
        steps = [["expr", "round(x / 100, 2)"]]
        self.assertEqual(calc.apply(34005, steps), 340.05)
        self.assertEqual(calc.describe(steps), "Formula (x is this value; other columns by "
                                               "their name) 'round(x / 100, 2)'")
        self.assertEqual(calc.columns_needed([["expr", "x * qty + fee"]]), ["qty", "fee"])
        self.assertIsNone(calc.check_step(steps[0]))
        self.assertIn("character", calc.check_step(["expr", "x +"]))
        result, why = calc.explain("abc", steps)
        self.assertIsNone(result)
        self.assertIn("not a number", why)
        result, why = calc.explain(5, [["expr", "x +"]])
        self.assertIn("formula", why)
        # another column through the row lookup
        self.assertEqual(calc.apply(10, [["expr", "x * qty"]], {"qty": 4}.get), 40)


if __name__ == "__main__":
    unittest.main()
