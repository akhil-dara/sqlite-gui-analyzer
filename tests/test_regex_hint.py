"""The literal a regex search pre-filters on must be in every match, or rows get dropped."""
import os
import re
import sqlite3
import unittest

from tests.helpers import TempDirTest
from engine.search import regex_literal_hint
from engine.session import Session


class RegexLiteralHintTest(unittest.TestCase):
    def test_every_match_contains_the_hint(self):
        cases = [("hel+o", ["hello", "helllo", "helo"]), ("hel{2,5}o", ["hello", "helllllo"]),
                 (r"x(\d)y", ["x5y"]), (r"x(\da)y", ["x5ay"]), ("x(a|b)c", ["xac", "xbc"]),
                 (r"1(\d)2", ["152"]), ("(ab)+c", ["abc", "ababc"]), ("ab(cd)ef", ["abcdef"]),
                 ("a(b(c)d)e", ["abcde"]), ("ab?c", ["ac", "abc"]), ("(?i)User", ["USER", "user"]),
                 (r"secret\s+plan", ["secret  plan"]), ("x(yz){2}w", ["xyzyzw"]),
                 ("x(yz){2,3}w", ["xyzyzyzw"]), ("(ab|cd)ef", ["abef", "cdef"]),
                 ("a*bcd", ["bcd", "aabcd"]), ("ab|cd", ["ab", "cd"]), (r"(\d+)-(\d+)", ["1-2"]),
                 ("x(?=y)y", ["xy"]), ("(a)(b)\\2", ["abb"])]
        for pattern, texts in cases:
            hint = regex_literal_hint(pattern)
            for text in texts:
                m = re.search(pattern, text)
                self.assertIsNotNone(m, (pattern, text))
                self.assertIn(hint.lower(), m.group(0).lower(), (pattern, text, hint))

    def test_hints_stay_useful(self):
        self.assertEqual(regex_literal_hint("hel+o"), "hel")
        self.assertEqual(regex_literal_hint("hel{2,5}o"), "hell")
        self.assertEqual(regex_literal_hint("ab(cd)ef"), "abcdef")
        self.assertEqual(regex_literal_hint("x(yz){2}w"), "xyzyzw")
        self.assertEqual(regex_literal_hint(r"secret\s+plan"), "secret")
        self.assertEqual(regex_literal_hint(r"(\d+)-order-(\d+)"), "-order-")
        self.assertEqual(regex_literal_hint("ab|cd"), "")


class RegexSearchTest(TempDirTest):
    def test_regex_search_finds_every_matching_row(self):
        path = os.path.join(self.tmp, "rx.db")
        rows = [(1, "say hello"), (2, "x5y"), (3, "a 152 b"), (4, "xac")] + \
            [(i, "other %d" % i) for i in range(5, 60)]
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
        c.executemany("INSERT INTO t VALUES (?, ?)", rows)
        c.commit()
        c.close()
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        for pattern in ("hel+o", r"x(\d)y", r"1(\d)2", "x(a|b)c", "hel{2,5}o", r"r \d+"):
            want = [i for i, text in rows if re.search(pattern, text)]
            self.assertTrue(want, pattern)
            self.assertEqual([h["locator"].value for h in s.search("t", pattern, "rx", 1000)],
                             want, pattern)


if __name__ == "__main__":
    unittest.main()
