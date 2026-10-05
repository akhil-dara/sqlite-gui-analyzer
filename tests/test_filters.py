import sqlite3
import unittest
from unittest import mock

from tests.helpers import TempDirTest
from tests.fixtures import make_fixtures as fx
from tests.test_session import SessionTestBase, open_native
from engine.backends import Filter
from engine import filters
from engine.fileformat.record import InvalidText
from engine.filters import (FilterError, balanced, check_expr, like_text, parse_expr,
                            parse_words, real_text, value_expr)
from engine.session import IMMUTABLE, NATIVE

# Every operator, over every storage class (see fixtures.mixed_values)
EXPRESSIONS = [
    # contains / does not contain
    "abc", "ABC", "b", "5", "0.3", "1.0e+20", "ü", "Ü", "PROGRA~1", "\\Users", "_", "user_id",
    "line1", "'", '"hi"x', "!abc", "!5", "!ü", "! b",
    # LIKE patterns
    "%abc%", "a%", "%c", "%", "_%", "a_c%", "5%", "%\\%", "%0.3%", "!%b%", "!a%", "%\u00fc%",
    # regular expressions
    "/^a/", "/abc/i", "/\\d+/", "/^$/", "/ü/", "//", "/^2\\.5$/", "/e\\+20/", "/0\\.3000/",
    "/\\ufffd/", "/^\\S+$/i", "/A/",
    # NULL tests
    "NULL", "NOT NULL", "not  null",
    # comparisons (numbers, text, quoted text, BLOBs)
    ">5", ">=5", "<5", "<=5", "=5", "<>5", "!=5", "=abc", ">abc", "<abc", "=ABC", '="5"',
    '>"5"', "=''", '=""', "''", '"abc"', "=x'616263'", ">x'00'", "<x'ff'", "=2.5", ">1e10",
    "<-1", "=0", "=-0.0", ">=Z", "<=é", ">é", "=ü", "<>abc", "=9223372036854775808",
    "> 2020-01-01", "=0.30000000000000004",
    # ranges
    "0~10", "-5 ~ 5", "a~c", "A~z", '"1"~"5"', "x'00'~x'ff'", "2020-01-01~2020-12-31",
    "1e-10~1e10",
]

# The operators added for column filter dialogs (same storage classes and columns)
NEW_EXPRESSIONS = [
    # starts / ends with, and their negations
    "^=a", "^=A", "^=5", "^=0.3", "^=1.0e", "^=ü", "^=Ü", '^=""', '^=" pad"', "^=%", "^=_",
    "^=\\", "^=C:\\", "^=user_", "^=50%", "^=line1\nl", "$=c", "$=C", "$=0", "$=+20", "$=%",
    "$=id", '$=""', "$=é", "$=字", "$=bc", "$=z", "!^=a", "!$=c", "!^=5", "!$=%", '!^=""',
    "*=50%", "*=_", "*=~", "!*=_", "!*=b", '*="it\'s"',
    # empty / not empty
    "EMPTY", "NOT EMPTY", "empty", "not  empty",
    # value lists
    "IN (5)", "IN (5, 10, abc)", "IN (\"5\", 2.5, x'616263', NULL)", "IN (NULL)",
    "NOT IN (NULL)", "NOT IN (5, abc)", "NOT IN (5, \"\", NULL)",
    "IN (0.30000000000000004, 1e20, -0.0, 0)", "IN (ü, Ü, é, 字, 'a_c', \"50%\")",
    "IN (\"\", x'')", "NOT IN (x'', '')", "IN (99999999999, 1e10)", "in(zzz,ZZZ)",
    "in (\"say \"\"hi\"\"\", 'it''s')", "IN (-3, 12, 1099511627776, 3.5, abc, x, Zeta)",
    "NOT IN (0, 0.0, 2.5, -1.5, 'n/a', \"a,b\", \"(x)\")", "IN (\"PROGRA~1\", 'C:\\Users\\x')",
    # two conditions
    "{a} AND {!b}", "{a} OR {5}", "{>5} AND {<abc}", "{NULL} OR {EMPTY}", "{^=a} OR {$=c}",
    "{{a} OR {b}} AND {NOT IN (abc)}", "{/^a/i} and {!$=c}", "{NOT EMPTY} AND {!IN (1)}",
    "{=\"}\"} OR {IN ('{', \"}\", NULL)}", "{0~10} OR {a~c}", "{it's} OR {$=%}",
]


class ParseTest(unittest.TestCase):
    def kind(self, text):
        e = parse_expr(text)
        return e.kind if e is not None else None

    def test_kinds(self):
        cases = {"": None, "   ": None, "abc": "contains", "!abc": "notcontains",
                 "a%": "like", "!%a": "notlike", "/x/": "regex", "/x/i": "regex",
                 "NULL": "null", "null": "null", "NOT NULL": "notnull", "not   Null": "notnull",
                 ">5": "cmp", ">=5": "cmp", "<5": "cmp", "<=5": "cmp", "=x": "cmp",
                 "<>x": "cmp", "!=x": "cmp", "5~10": "range", "a ~ b": "range",
                 "''": "cmp", '""': "cmp", '"abc"': "cmp",
                 # a number and a text end are not a range (Windows short names)
                 "PROGRA~1": "contains", "5~": "contains", "~5": "contains",
                 "user_id": "contains", "C:\\Users": "contains", "50\\%": "like",
                 # added operators
                 "^=a": "starts", "$=a": "ends", "!^=a": "notstarts", "!$=a": "notends",
                 "*=a": "contains", "!*=a": "notcontains", "*=50%": "contains",
                 "EMPTY": "empty", "Empty": "empty", "NOT EMPTY": "notempty",
                 "not\tempty": "notempty", "IN (1)": "in", "in(1)": "in", "NOT IN (1)": "notin",
                 "not in ( 1 )": "notin", "{a} AND {b}": "and", "{a}or{b}": "or",
                 # still their old meaning
                 "a^=b": "contains", "EMPTYish": "contains", "INDEX (1)": "contains",
                 "IN": "contains", "IN 1": "contains", "!{a}": "notcontains",
                 "a{b} AND {c}": "contains", "!in (1)": "notcontains"}
        for text, want in cases.items():
            self.assertEqual(self.kind(text), want, text)

    def test_operands(self):
        self.assertEqual(parse_expr(">5").operand, ("num", 5))
        self.assertEqual(parse_expr(">-2.5").operand, ("num", -2.5))
        self.assertEqual(parse_expr(">1e3").operand, ("num", 1000.0))
        self.assertEqual(parse_expr('>"5"').operand, ("text", "5"))
        self.assertEqual(parse_expr(">abc").operand, ("text", "abc"))
        self.assertEqual(parse_expr("=x'00FF'").operand, ("blob", b"\x00\xff"))
        self.assertEqual(parse_expr("= 9223372036854775807").operand, ("num", (1 << 63) - 1))
        self.assertEqual(parse_expr("=9223372036854775808").operand, ("num", float(1 << 63)))
        self.assertEqual(parse_expr('"a""b"').operand, ("text", 'a"b'))
        self.assertEqual(parse_expr("''").operand, ("text", ""))
        self.assertEqual(parse_expr("!= x").op, "<>")
        rng = parse_expr("1~5")
        self.assertEqual((rng.lo, rng.hi), (("num", 1), ("num", 5)))
        self.assertEqual(parse_expr("/abc/i").pattern, "(?i)abc")

    def test_semantics(self):
        def picks(text, values):
            e = parse_expr(text)
            return [v for v in values if e.match(v)]
        vals = [None, -1, 4, 5, 5.0, 6.5, "", "5", "1", "abc", "ABC", "abd", b"", b"5"]
        self.assertEqual(picks(">5", vals), [6.5, "", "5", "1", "abc", "ABC", "abd", b"", b"5"])
        self.assertEqual(picks("<=5", vals), [-1, 4, 5, 5.0])
        self.assertEqual(picks("=5", vals), [5, 5.0])
        self.assertEqual(picks('="5"', vals), ["5"])
        self.assertEqual(picks("<>5", vals), [-1, 4, 6.5, "", "5", "1", "abc", "ABC", "abd",
                                              b"", b"5"])
        self.assertEqual(picks("''", vals), [""])
        self.assertEqual(picks("NULL", vals), [None])
        self.assertEqual(picks("4~5", vals), [4, 5, 5.0])
        self.assertEqual(picks("abc~abd", vals), ["abc", "abd"])
        self.assertEqual(picks(">abc", vals), ["abd", b"", b"5"])       # binary: 'ABC' < 'abc'
        self.assertEqual(picks("ab", vals), ["abc", "ABC", "abd"])
        self.assertEqual(picks("!ab", vals), [None, -1, 4, 5, 5.0, 6.5, "", "5", "1", b"", b"5"])
        self.assertEqual(picks("ab_", vals), [])                        # no %: plain text
        self.assertEqual(picks("ab_%", vals), ["abc", "ABC", "abd"])
        self.assertEqual(picks("%C", vals), ["abc", "ABC"])
        self.assertEqual(picks("/^a/", vals), ["abc", "abd"])
        self.assertEqual(picks("/^a/i", vals), ["abc", "ABC", "abd"])
        self.assertEqual(picks("/^5/", vals), [5, 5.0, "5", b"5"])       # 5.0 reads '5.0'
        self.assertEqual(picks("5", vals), [5, 5.0, 6.5, "5", b"5"])
        self.assertTrue(parse_expr("Ü").match("xüx") is False)          # ASCII-only case folding
        self.assertEqual(parse_expr("b").describe(), "contains 'b' (any case)")

    def test_malformed_input_is_a_clear_error(self):
        for text, fragment in (("/abc", "must end with /"), ("/", "must end with /"),
                               ("/i", "must end with /"), ("/a/b", "must end with /"),
                               ("/[/", "Invalid regular expression"),
                               ("/(?P<x>/", "Invalid regular expression"),
                               ("/a{2,1}/", "Invalid regular expression"),
                               (">", "needs a value"), (">=  ", "needs a value"),
                               ("=", "needs a value"), ("!", "needs text"),
                               ("a\x00b", "NUL"), ("x\ud800", "surrogates"),
                               ("^=", "needs text"), ("$=  ", "needs text"),
                               ("!^=", "needs text"), ("*=", "needs text"),
                               ("IN ()", "at least one value"), ("NOT IN ( )", "at least one"),
                               ("IN (a,,b)", "is empty"), ("IN (a", "no closing )"),
                               ('IN ("a, b)', "not closed"), ("IN ('a)", "not closed"),
                               ("IN (a) b", "may follow"), ('IN ("a" "b")', "commas"),
                               ("IN (f(x))", "in quotes"), ("{a} AND {b", "Unbalanced"),
                               ("{a", "Unbalanced"), ("{a}}", "Unbalanced"),
                               ("{a}", "Braces join"), ("{} AND {b}", "is empty"),
                               ("{a} AND {b} OR {c}", "nest braces"),
                               ("{a} AND {/x}", "must end with /"),
                               ("{a} OR {>}", "needs a value")):
            with self.assertRaises(FilterError) as cm:
                parse_expr(text)
            self.assertIn(fragment, str(cm.exception), text)
            self.assertEqual(check_expr(text), str(cm.exception))
        self.assertIsNone(check_expr(">5"))
        with self.assertRaises(FilterError):
            Filter(col_exprs={"a": "/(/"})

    def test_new_operator_semantics(self):
        def picks(text, values):
            e = parse_expr(text)
            return [v for v in values if e.match(v)]
        vals = [None, 5, 5.0, 2.5, "", "5", "abc", "ABC", "xab", b"", b"ab", "a\x00", "\x00",
                InvalidText(b"\xffab")]
        self.assertEqual(picks("^=ab", vals), ["abc", "ABC", b"ab"])
        self.assertEqual(picks("$=AB", vals), ["xab", b"ab", InvalidText(b"\xffab")])
        self.assertEqual(picks("!^=ab", vals), [None, 5, 5.0, 2.5, "", "5", "xab", b"",
                                                "a\x00", "\x00", InvalidText(b"\xffab")])
        self.assertEqual(picks("^=5", vals), [5, 5.0, "5"])              # 5.0 reads '5.0'
        self.assertEqual(picks('^=""', vals), vals[1:])                # every non-NULL value
        # EMPTY: NULL or exactly the empty text; not x'', not text holding only a NUL
        self.assertEqual(picks("EMPTY", vals), [None, ""])
        self.assertEqual(picks("NOT EMPTY", vals), [v for v in vals if not (v is None or
                                                    (isinstance(v, str) and v == ""))])
        self.assertEqual(picks("IN (5, abc)", vals), [5, 5.0, "abc"])
        self.assertEqual(picks('IN ("5", NULL)', vals), [None, "5"])
        self.assertEqual(picks("IN (x'', '')", vals), ["", b""])
        self.assertEqual(picks("NOT IN (5, abc)", vals),
                         [v for v in vals if v not in (5, "abc")])       # NULL included
        self.assertEqual(picks("NOT IN (5, NULL)", vals),
                         [v for v in vals if v is not None and v != 5])
        self.assertEqual(picks("{^=a} AND {!$=c}", vals), [b"ab", "a\x00"])
        self.assertEqual(picks("{NULL} OR {=2.5}", vals), [None, 2.5])
        self.assertEqual(parse_expr("{a} AND {!b}").describe(),
                         "contains 'a' (any case) and does not contain 'b' (any case)")
        e = parse_expr("IN (1, \"a,b)\", 'x''y', x'00', NULL, two words)")
        self.assertEqual(e.items, (("num", 1), ("text", "a,b)"), ("text", "x'y"),
                                   ("blob", b"\x00"), ("text", "two words")))
        self.assertTrue(e.has_null)
        e = parse_expr("{=\"a} AND {b\"} OR {='{'}")
        self.assertEqual((e.kind, e.left.text, e.right.text), ("or", '="a} AND {b"', "='{'"))

    def test_words(self):
        self.assertEqual(parse_words('  alpha "two words"  beta ""  '),
                         ["alpha", "two words", "beta"])
        self.assertEqual(parse_words(""), [])

    def test_real_text_is_sqlite_cast(self):
        conn = sqlite3.connect(":memory:")
        try:
            for f in (0.1, 0.1 + 0.2, 1e20, 1e-5, 123456789012345678.0, 1 / 3.0, 2.0, -0.0,
                      1e300, 5e-324, 100.0, 1e15, 1e16, 12345.678, 1.5e-7, 2.0 ** 53, -7.25,
                      3.14159265358979, 1e21, 1.7976931348623157e308, 4.35, float("inf"),
                      float("-inf"), -1e-300, 0.5, 65536.0):
                want = conn.execute("SELECT CAST(? AS TEXT)", (f,)).fetchone()[0]
                self.assertEqual(real_text(f), want, repr(f))
        finally:
            conn.close()

    def test_like_text_stops_at_nul_like_sqlite(self):
        self.assertEqual(like_text("a\x00bc"), "a")
        self.assertEqual(like_text(b"\x00abc"), "")
        self.assertEqual(like_text(InvalidText(b"\xff\xfeA")), "\ufffd\ufffdA")
        self.assertIsNone(like_text(None))

    def test_value_expr_selects_exactly_that_value(self):
        def storage(x):
            return 0 if x is None else 1 if isinstance(x, (int, float)) else \
                3 if isinstance(x, bytes) else 2

        values = fx.MIXED_A + [InvalidText(b"\xffA"), b"x" * 300, 1 << 62]
        for v in values:
            text = value_expr(v)
            if text is None:
                self.assertTrue(isinstance(v, bytes) or "\x00" in str(v), repr(v))
                continue
            e = parse_expr(text)
            self.assertTrue(e.match(v), "%r -> %r" % (v, text))
            for other in values:
                if isinstance(other, InvalidText):
                    continue
                same = storage(other) == storage(v) and (v is None or other == v)
                self.assertEqual(e.match(other), same, "%r on %r" % (text, other))

    def test_balanced_keeps_expression_depth_low(self):
        self.assertEqual(balanced("OR", []), "")
        self.assertEqual(balanced("OR", ["a"]), "a")
        self.assertEqual(balanced("OR", ["a", "b", "c"]), "((a OR b) OR c)")
        # a global filter over 1100 columns: a flat OR chain exceeds SQLite's depth limit
        cols = ["c%d" % i for i in range(1100)]
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE w(%s)" % ", ".join(cols))
            conn.execute("INSERT INTO w(c1099) VALUES ('needle and hay')")
            where, params = Filter(words=["needle", "hay"]).where_sql(cols)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM w WHERE " + where,
                                          params).fetchone()[0], 1)
        finally:
            conn.close()


def locators(page):
    return [r.locator for r in page.rows]


class DifferentialTest(SessionTestBase):
    """Every filter selects the same rows from a table served by SQLite and from the same
    table read natively."""

    def sessions(self, path):
        sql = self.open(path)
        native = open_native(self, path)
        self.assertEqual((sql.mode, native.mode), (IMMUTABLE, NATIVE))
        return sql, native

    def compare(self, sql, native, table, flt, label):
        self.assertEqual(sql.source(table), "sql")
        self.assertEqual(native.source(table), "native")
        a = sorted(locators(sql.browse(table, 0, 10000, flt=flt)), key=repr)
        b = sorted(locators(native.browse(table, 0, 10000, flt=flt)), key=repr)
        self.assertEqual(a, b, label)
        self.assertEqual(sql.count(table, flt), len(a), label)
        self.assertEqual(native.count(table, flt), len(b), label)
        return a

    def test_every_operator_selects_the_same_rows(self):
        sql, native = self.sessions(fx.mixed_values(self.tmp))
        nonempty = 0
        for col in ("a", "t", "i", "r"):
            for text in EXPRESSIONS:
                got = self.compare(sql, native, "vals", Filter(col_exprs={col: text}),
                                   "%s %r" % (col, text))
                nonempty += bool(got)
        self.assertGreater(nonempty, len(EXPRESSIONS))      # the fixture really exercises them
        self.assertNotIn("sql_table_failed", [i.kind for i in sql.issues])

    def test_every_new_operator_selects_the_same_rows(self):
        sql, native = self.sessions(fx.mixed_values(self.tmp))
        nonempty = 0
        for col in ("a", "t", "i", "r"):
            for text in NEW_EXPRESSIONS:
                got = self.compare(sql, native, "vals", Filter(col_exprs={col: text}),
                                   "%s %r" % (col, text))
                nonempty += bool(got)
        self.assertGreater(nonempty, len(NEW_EXPRESSIONS))
        for text, want in (("EMPTY", 1), ("NOT EMPTY", 2), ("IN (1, NULL)", 2),
                           ("NOT IN (1)", 2), ("NOT IN (NULL, 3)", 1), ("{NULL} OR {=3}", 2)):
            self.assertEqual(len(self.compare(sql, native, "nn", Filter(col_exprs={"k": text}),
                                              text)), want, text)
        self.assertNotIn("sql_table_failed", [i.kind for i in sql.issues])

    def test_long_value_lists_are_written_as_literals(self):
        # past _IN_PARAMS_MAX a list is written inline; force that path for every list
        sql, native = self.sessions(fx.mixed_values(self.tmp))
        with mock.patch.object(filters, "_IN_PARAMS_MAX", 0):
            for col in ("a", "t", "i", "r"):
                for text in NEW_EXPRESSIONS:
                    if "IN" in text.upper():
                        self.compare(sql, native, "vals", Filter(col_exprs={col: text}),
                                     "inline %s %r" % (col, text))

    def test_combined_and_global_filters(self):
        sql, native = self.sessions(fx.mixed_values(self.tmp))
        for flt in (Filter(col_exprs={"a": "NOT NULL", "i": ">0"}),
                    Filter(col_exprs={"t": "!x", "r": "<100"}, words=["5"]),
                    Filter(words=["a", "b"]), Filter(words=["ABC"]), Filter(words=["zeta 7"]),
                    Filter(any_term="bc"), Filter(col_terms={"a": "B"}),
                    Filter(any_term="c", col_terms={"t": "a"}, col_exprs={"i": "NOT NULL"},
                           words=["a"]),
                    Filter(col_exprs={"missing_column": ">5"})):
            self.compare(sql, native, "vals", flt, repr(flt.key()))
        self.assertEqual(sql.count("vals", Filter(col_exprs={"missing_column": ">5"})),
                         sql.count("vals"))

    def test_null_in_not_null_column(self):
        # SQLite drops 'k IS NULL' for a NOT NULL column; the filter must still find the row
        sql, native = self.sessions(fx.mixed_values(self.tmp))
        for text, want in (("NULL", 1), ("NOT NULL", 2), (">0", 2), ("!zzz", 3)):
            got = self.compare(sql, native, "nn", Filter(col_exprs={"k": text}), text)
            self.assertEqual(len(got), want, text)
        self.assertEqual(len(self.compare(sql, native, "nn", Filter(col_exprs={"v": "NULL"}),
                                          "v NULL")), 1)

    def test_utf16_text_order_is_the_database_byte_order(self):
        sql, native = self.sessions(fx.mixed_values(self.tmp, "UTF-16le"))
        for text in (">m", "<m", ">=Āb", "<字", "a~ſx", ">ü", "=字", "Ā", "/^.b$/", ">5", "%b"):
            self.compare(sql, native, "vals", Filter(col_exprs={"a": text}), text)
        for col in ("a", "t"):
            for text in NEW_EXPRESSIONS + ["IN (Āb, 字, ſx)", "NOT IN (Āb, NULL)", "^=Ā",
                                           "$=字"]:
                self.compare(sql, native, "vals", Filter(col_exprs={col: text}),
                             "utf-16 %s %r" % (col, text))
        # U+0100 sorts before 'a' in UTF-16LE bytes (00 01 < 61 00), unlike code point order
        got = sql.browse("vals", 0, 100, flt=Filter(col_exprs={"a": "<a"})).rows
        self.assertIn("Āb", [r.values[1] for r in got])

    def test_sorted_and_filtered_reads_agree(self):
        sql, native = self.sessions(fx.without_rowid(self.tmp))
        flt = Filter(col_exprs={"b": "100~150"}, words=["a01"])
        for s in (sql, native):
            page = s.browse("pk_last", 0, 1000, order_by="b", desc=True, flt=flt)
            self.assertEqual([r.values[1] for r in page.rows], list(range(150, 99, -1)))
            self.assertEqual(s.count("pk_last", flt), 51)
            self.assertEqual([r.locator for r in s.iter_rows("pk_last", flt, "b", True)],
                             locators(page))
            window = s.browse("pk_last", 10, 5, order_by="b", desc=True, flt=flt)
            self.assertEqual(locators(window), locators(page)[10:15])

    def test_native_sorted_read_is_reused_by_the_next_window(self):
        native = open_native(self, fx.without_rowid(self.tmp))
        flt = Filter(col_exprs={"b": ">=10"})
        native.browse("pk_last", 0, 50, order_by="c", flt=flt)
        first = native._native_sorted
        native.browse("pk_last", 50, 50, order_by="c", flt=Filter(col_exprs={"b": ">=10"}))
        self.assertIs(native._native_sorted, first)
        native.browse("pk_last", 0, 50, order_by="c", flt=Filter(col_exprs={"b": ">=11"}))
        self.assertIsNot(native._native_sorted, first)

    def test_regex_over_invalid_text_does_not_fail_the_table(self):
        s = self.open(fx.quirks(self.tmp))
        page = s.browse("bad_text", 0, 10, flt=Filter(col_exprs={"s": "/A$/"}))
        self.assertEqual([r.locator.value for r in page.rows], [2])
        self.assertEqual(s.source("bad_text"), "sql")
        self.assertNotIn("sql_table_failed", [i.kind for i in s.issues])

    def test_iter_rows_with_filter_streams_every_native_match(self):
        sql, native = self.sessions(fx.freelist(self.tmp))
        flt = Filter(col_exprs={"id": "<=10"})
        self.assertEqual([r.locator.value for r in native.iter_rows("notes", flt)],
                         list(range(1, 11)))
        for s in (sql, native):
            self.assertEqual([r.locator.value for r in s.iter_rows("notes", flt, desc=True)],
                             list(range(10, 0, -1)))


if __name__ == "__main__":
    unittest.main()
