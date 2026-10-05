"""condition_text() / combine(): structured input to filter text that reads back exactly, and
the new operators (^= $= EMPTY IN {..} AND/OR) agreeing between SQL and Python."""

import random
import re
import sqlite3
import struct
import unittest

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from engine import filters
from engine.filters import (FilterError, ascii_lower, combine, condition_text, like_text,
                            parse_expr, regex_text, regexp_value)

_ALPHABET = (list("aAbBzZ09 .-+,(){}\"'%_\\~=<>!^$*/|;:#") +
             ["é", "Ü", "ü", "字", "\U0001F600", "\t", "\n", "ſ", "K", "ı"])
_WORDS = ["NULL", "null", "EMPTY", "NOT EMPTY", "IN (", "not in (1)", "x'00'", "5", "1e3",
          "-2.5", "{a} AND {b}", "} OR {", "\"\"", "''", "a~b", "^=", "$=", "*=", "!", "/x/"]


def rand_text(rng):
    if rng.random() < 0.2:
        parts = [rng.choice(_WORDS) for _ in range(rng.randint(1, 2))]
        if rng.random() < 0.5:
            parts.append(rng.choice(_ALPHABET))
        return "".join(parts)
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 8)))


def rand_float(rng):
    r = rng.random()
    if r < 0.3:
        return struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0]
    if r < 0.5:
        return rng.choice([0.0, -0.0, 0.1 + 0.2, 1e20, 2.5, 5.0, -1.5, 5e-324, 1e-310,
                           2.0 ** 63, 2.0 ** 53 + 2, 1.7976931348623157e308, float("inf"),
                           float("-inf"), 1 / 3.0, 12345.678, 9.223372036854775e18])
    return rng.uniform(-1e6, 1e6) * 10 ** rng.randint(-20, 20)


def rand_value(rng):
    r = rng.random()
    if r < 0.08:
        return None
    if r < 0.3:
        return rng.choice([0, 5, -5, 10, 1 << 40, (1 << 63) - 1, -(1 << 63), rng.randint(-99, 99)])
    if r < 0.5:
        f = rand_float(rng)
        return f if f == f else 1.5
    if r < 0.85:
        return rand_text(rng)
    return bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 4)))


def storage(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return 1
    return 2 if isinstance(v, str) else 3


def cmp_values(a, b):
    """SQLite's comparison without affinity, BINARY collation (UTF-8 byte order is code point
    order); None when either side is NULL."""
    if a is None or b is None:
        return None
    ra, rb = storage(a), storage(b)
    if ra != rb:
        return -1 if ra < rb else 1
    return (a > b) - (a < b)


ENCODING = ["utf-8"]       # the database encoding the predicates read BLOBs in


def lower_text(v):
    t = like_text(v, ENCODING[0])
    return None if t is None else ascii_lower(t)


PREDICATES = {
    "contains": lambda v, t: lower_text(v) is not None and ascii_lower(t) in lower_text(v),
    "not_contains": lambda v, t: not (lower_text(v) is not None
                                      and ascii_lower(t) in lower_text(v)),
    "starts": lambda v, t: lower_text(v) is not None and lower_text(v).startswith(ascii_lower(t)),
    "not_starts": lambda v, t: not (lower_text(v) is not None
                                    and lower_text(v).startswith(ascii_lower(t))),
    "ends": lambda v, t: lower_text(v) is not None and lower_text(v).endswith(ascii_lower(t)),
    "not_ends": lambda v, t: not (lower_text(v) is not None
                                  and lower_text(v).endswith(ascii_lower(t))),
    "equals": lambda v, x: v is None if x is None else cmp_values(v, x) == 0,
    "not_equals": lambda v, x: v is not None if x is None else cmp_values(v, x) not in (0, None),
    "gt": lambda v, x: (cmp_values(v, x) or 0) > 0,
    "ge": lambda v, x: cmp_values(v, x) is not None and cmp_values(v, x) >= 0,
    "lt": lambda v, x: (cmp_values(v, x) or 0) < 0,
    "le": lambda v, x: cmp_values(v, x) is not None and cmp_values(v, x) <= 0,
    "between": lambda v, a, b: (cmp_values(v, a) is not None and cmp_values(v, a) >= 0
                                and cmp_values(v, b) <= 0),
    "in": lambda v, xs: any(v is None if x is None else cmp_values(v, x) == 0 for x in xs),
    "not_in": lambda v, xs: not any(v is None if x is None else cmp_values(v, x) == 0
                                    for x in xs),
    "empty": lambda v: v is None or (isinstance(v, str) and v == ""),
    "not_empty": lambda v: not (v is None or (isinstance(v, str) and v == "")),
    "null": lambda v: v is None,
    "not_null": lambda v: v is not None,
    "regex": lambda v, p, i: (regex_text(v) is not None
                              and re.search(p, regex_text(v), re.I if i else 0) is not None),
}


class Table(object):
    """The values in an untyped column of an in-memory database, to run SQL fragments on."""

    def __init__(self, values, encoding=None):
        self.values = list(values)
        self.conn = sqlite3.connect(":memory:")
        if encoding:
            self.conn.execute("PRAGMA encoding='%s'" % encoding)
        self.encoding = {"UTF-16le": "utf-16-le", "UTF-16be": "utf-16-be"}.get(encoding, "utf-8")
        self.conn.create_function("sga_regexp", 4, regexp_value)
        self.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v)")
        self.conn.executemany("INSERT INTO t VALUES (?, ?)", list(enumerate(self.values)))

    def close(self):
        self.conn.close()

    def sql_ids(self, expr):
        frag, params = expr.sql("v", self.encoding)
        return sorted(r[0] for r in self.conn.execute("SELECT id FROM t WHERE " + frag, params))

    def match_ids(self, expr):
        return [i for i, v in enumerate(self.values) if expr.match(v, self.encoding)]


def rand_args(rng, kind):
    if kind in ("contains", "not_contains", "starts", "not_starts", "ends", "not_ends"):
        return (rand_text(rng),)
    if kind in ("equals", "not_equals"):
        return (rand_value(rng),)
    if kind in ("gt", "ge", "lt", "le"):
        v = rand_value(rng)
        return (v if v is not None else 0,)
    if kind == "between":
        a, b = rand_value(rng), rand_value(rng)
        a, b = (a if a is not None else -1), (b if b is not None else "m")
        if storage(a) == storage(b) and rng.random() < 0.7 and cmp_values(a, b) > 0:
            a, b = b, a
        return (a, b)
    if kind in ("in", "not_in"):
        return ([rand_value(rng) for _ in range(rng.randint(1, 6))],)
    if kind == "regex":
        return (re.escape(rand_text(rng)[:3]) or "a", rng.random() < 0.5)
    return ()


KINDS = sorted(PREDICATES)


class ConditionTextTest(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(20260930)
        self.pool = ([rand_value(self.rng) for _ in range(300)] +
                     [None, "", b"", 0, 5, 5.0, "5", "abc", "ABC", "a,b", "(x)", "{", "}",
                      '"', "'", "%", "_", "\\", "NULL", "EMPTY", " pad ", "x'00'", b"\x00"])
        self.table = Table(self.pool)
        self.addCleanup(self.table.close)

    def check(self, kind, args, text, table=None):
        table = table or self.table
        e = parse_expr(text)
        self.assertIsNotNone(e, text)
        pred = PREDICATES[kind]
        ENCODING[0] = table.encoding
        try:
            want = [i for i, v in enumerate(table.values) if pred(v, *args)]
        finally:
            ENCODING[0] = "utf-8"
        self.assertEqual(table.match_ids(e), want, "%s%r -> %r" % (kind, args, text))
        self.assertEqual(table.sql_ids(e), want, "SQL %s%r -> %r" % (kind, args, text))
        return e

    def test_every_kind_reads_back_as_written(self):
        for kind in KINDS:
            for _ in range(60 if kind not in ("empty", "not_empty", "null", "not_null") else 1):
                args = rand_args(self.rng, kind)
                text = condition_text(kind, *args)
                self.check(kind, args, text)

    def test_values_keep_their_storage_class(self):
        self.assertEqual(condition_text("equals", "5"), '="5"')
        self.assertEqual(condition_text("equals", 5), "=5")
        self.assertEqual(condition_text("equals", 5.0), "=5.0")
        self.assertEqual(condition_text("equals", "abc"), "=abc")
        self.assertEqual(condition_text("equals", ""), '=""')
        self.assertEqual(condition_text("equals", b"\x00\xff"), "=x'00ff'")
        self.assertEqual(condition_text("equals", "x'00'"), '="x\'00\'"')
        self.assertEqual(condition_text("equals", None), "NULL")
        self.assertEqual(condition_text("not_equals", None), "NOT NULL")
        self.assertEqual(condition_text("lt", "=5"), '<"=5"')
        self.assertEqual(condition_text("equals", float("inf")), "=1e999")
        self.assertEqual(condition_text("contains", "abc"), "abc")
        self.assertEqual(condition_text("contains", "50%"), "*=50%")
        self.assertEqual(condition_text("contains", " a"), '*=" a"')
        self.assertEqual(condition_text("not_contains", "b"), "!b")
        self.assertEqual(condition_text("not_contains", "a%"), "!*=a%")
        self.assertEqual(condition_text("starts", "ab"), "^=ab")
        self.assertEqual(condition_text("ends", 'say "x"'), '$="say ""x"""')
        self.assertEqual(condition_text("between", 1, 5), "1~5")
        self.assertEqual(condition_text("between", "a", "c"), "a~c")
        self.assertEqual(condition_text("between", 1, "c"), "{>=1} AND {<=c}")
        self.assertEqual(condition_text("in", [1, "1", None, "a,b", b""]),
                         'IN (1, "1", NULL, "a,b", x\'\')')
        self.assertEqual(condition_text("not_in", ["NULL"]), 'NOT IN ("NULL")')
        self.assertEqual(condition_text("regex", "^a/b$", True), "/^a/b$/i")
        self.assertEqual(condition_text("empty"), "EMPTY")
        self.assertEqual(condition_text("not_empty"), "NOT EMPTY")

    def test_unwritable_input_is_refused(self):
        for args, exc in ((("equals", float("nan")), ValueError),
                          (("equals", 1 << 64), ValueError),
                          (("equals", "a\x00"), FilterError),
                          (("contains", "x\ud800"), FilterError),
                          (("gt", None), ValueError), (("in", []), ValueError),
                          (("not_in", iter(())), ValueError), (("equals", object()), TypeError),
                          (("regex", "("), FilterError), (("bogus", 1), ValueError),
                          (("contains",), TypeError), (("contains", 5), TypeError)):
            with self.assertRaises(exc, msg=repr(args)):
                condition_text(*args)
        with self.assertRaises(ValueError):
            combine("a", "XOR", "b")
        with self.assertRaises(ValueError):
            combine("a", "AND", " ")
        with self.assertRaises(FilterError):
            combine("a} AND {b", "OR", "c")

    def test_combine_reads_back_as_both_conditions(self):
        for _ in range(250):
            k1, k2 = self.rng.choice(KINDS), self.rng.choice(KINDS)
            a1, a2 = rand_args(self.rng, k1), rand_args(self.rng, k2)
            t1, t2 = condition_text(k1, *a1), condition_text(k2, *a2)
            op = self.rng.choice(["AND", "OR", "and", "Or"])
            text = combine(t1, op, t2)
            e = parse_expr(text)
            self.assertEqual((e.kind, e.left.text, e.right.text), (op.lower(), t1, t2), text)
            p1, p2 = PREDICATES[k1], PREDICATES[k2]
            if op.upper() == "AND":
                want = [i for i, v in enumerate(self.pool) if p1(v, *a1) and p2(v, *a2)]
            else:
                want = [i for i, v in enumerate(self.pool) if p1(v, *a1) or p2(v, *a2)]
            self.assertEqual(self.table.match_ids(e), want, text)
            self.assertEqual(self.table.sql_ids(e), want, text)
            # nested once more
            outer = combine(text, "OR", condition_text("null"))
            self.assertEqual(parse_expr(outer).left.text, text)

    def test_utf16_databases_agree(self):
        for enc in ("UTF-16le", "UTF-16be"):
            table = Table(self.pool, enc)
            try:
                for kind in KINDS:
                    for _ in range(15):
                        args = rand_args(self.rng, kind)
                        if kind in ("gt", "ge", "lt", "le", "between"):
                            continue    # order differs from code point order in UTF-16
                        self.check(kind, args, condition_text(kind, *args), table)
                for text in ("<字", ">Āb", "{>=a} AND {<字}", "IN (字, Āb)",
                             "NOT IN (Āb, NULL)"):
                    e = parse_expr(text)
                    self.assertEqual(table.sql_ids(e), table.match_ids(e), text)
            finally:
                table.close()


class ValueListTest(unittest.TestCase):
    def test_five_thousand_values(self):
        rng = random.Random(5000)
        wanted = []
        for i in range(5000):
            r = i % 4
            wanted.append(i if r == 0 else rand_float(rng) if r == 1 else
                          "t%d,(%s)'\"{}" % (i, rng.choice(["é", "%", "_", "\\"])) if r == 2 else
                          struct.pack("<I", i))
        wanted = [v for v in wanted if v == v]
        others = [rand_value(rng) for _ in range(2000)] + ["t1", 1.5, b"", None, ""]
        values = wanted[::3] + others
        table = Table(values)
        try:
            for kind, extra in (("in", []), ("in", [None]), ("not_in", []), ("not_in", [None])):
                text = condition_text(kind, wanted + extra)
                e = parse_expr(text)
                self.assertEqual(len(e.items), len(wanted))
                frag, params = e.sql("v")
                self.assertEqual(params, [])            # written inline, no host parameters
                # the same predicate as PREDICATES["in"], by (storage class, value) keys
                keys = set((storage(x), x) for x in wanted + extra)
                want = [i for i, v in enumerate(values)
                        if ((storage(v), v) in keys) == (kind == "in")]
                self.assertEqual(table.match_ids(e), want, kind)
                self.assertEqual(table.sql_ids(e), want, kind)
            self.assertIn("5000 values", parse_expr(condition_text("in", wanted[:4999] + [None]))
                          .describe())
        finally:
            table.close()

    def test_inline_reals_are_exact(self):
        rng = random.Random(7)
        floats = [rand_float(rng) for _ in range(3000)]
        floats += [5e-324, -5e-324, 2.2250738585072014e-308, 2.225073858507201e-308,
                   1.7976931348623157e308, 0.1, 1 / 3.0, 2.0 ** 63, -(2.0 ** 63), 2.0 ** 64,
                   123456789.123456789, 1e-300, float("inf"), float("-inf"), -0.0]
        conn = sqlite3.connect(":memory:")
        try:
            for f in floats:
                if f != f:
                    continue
                got = conn.execute("SELECT %s" % filters._real_sql(f)).fetchone()[0]
                self.assertEqual(struct.pack("<d", float(got)), struct.pack("<d", f + 0.0),
                                 repr(f))
        finally:
            conn.close()

    def test_quoted_items_hold_any_character(self):
        items = ["a,b", "(x)", ")", "(", "'", '"', "{}", " sp ", "", "NULL", "5", "x'00'",
                 "it's", 'say "hi"', "\\", "%_", "é字"]
        e = parse_expr(condition_text("in", items))
        self.assertEqual(e.items, tuple(("text", s) for s in items))
        self.assertFalse(e.has_null)
        e = parse_expr("IN ( 'a,b' ,\"c)\"  , NULL,x'0A' , plain text,-7 )")
        self.assertEqual(e.items, (("text", "a,b"), ("text", "c)"), ("blob", b"\n"),
                                   ("text", "plain text"), ("num", -7)))
        self.assertTrue(e.has_null)


if __name__ == "__main__":
    unittest.main()
