"""SQL editor completion: what is offered for the word at the cursor, and the keys."""
import os
import sys
import tkinter as tk
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sql_complete as sc  # noqa: E402
from tests.helpers import tk_root  # noqa: E402

SCHEMA = {"urls": [("id", "INTEGER"), ("url", "LONGVARCHAR"), ("title", "LONGVARCHAR")],
          "visits": [("id", "INTEGER"), ("url", "INTEGER"), ("visit_time", "INTEGER")],
          "odd name": [("my col", "TEXT")]}


class CandidatesTest(unittest.TestCase):
    def test_alias_dot_offers_only_that_tables_columns(self):
        sql = "SELECT v. FROM visits AS v"
        got = sc.candidates(SCHEMA, sql, sql, "", "v")
        self.assertEqual([c[1] for c in got], ["id", "url", "visit_time"])
        self.assertIn("visits · INTEGER", got[0][2])

    def test_a_table_name_before_the_dot_works_too(self):
        got = sc.candidates(SCHEMA, "", "SELECT urls.", "ti", "urls")
        self.assertEqual([c[1] for c in got], ["title"])

    def test_prefix_matches_come_first_tables_and_keywords_included(self):
        got = [c[1] for c in sc.candidates(SCHEMA, "", "SELECT * FROM v", "v", None)]
        self.assertEqual(got[0], "visits")
        self.assertIn("visit_time", got)
        sel = [c for c in sc.candidates(SCHEMA, "", "sel", "sel", None)]
        self.assertEqual(sel[0][0], "SELECT")

    def test_columns_of_the_tables_in_the_statement_come_first(self):
        sql = "SELECT  FROM urls"
        got = sc.candidates(SCHEMA, "SELECT ", sql, "", None)
        self.assertEqual([c[1] for c in got], ["id", "url", "title"])

    def test_names_that_need_quotes_are_inserted_quoted(self):
        got = sc.candidates(SCHEMA, "", "SELECT * FROM od", "od", None)
        self.assertEqual(got[0][0], '"odd name"')
        self.assertEqual(sc.quote("order"), '"order"')
        self.assertEqual(sc.quote("url"), "url")

    def test_sources_reads_aliases(self):
        s = sc.sources('SELECT * FROM visits v JOIN "odd name" AS o ON 1 LEFT JOIN urls')
        self.assertEqual(s, {"visits": "visits", "v": "visits", "odd name": "odd name",
                             "o": "odd name", "urls": "urls"})


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        self.text = tk.Text(self.root)
        self.text.pack()
        self.c = sc.SqlCompleter(self.text, lambda: SCHEMA)

    def test_open_choose_and_insert(self):
        self.text.insert("1.0", "SELECT * FROM vis")
        self.c.open()
        self.assertTrue(self.c.visible())
        self.assertEqual(self.c.items[0][1], "visits")
        self.c.take()
        self.assertFalse(self.c.visible())
        self.assertEqual(self.text.get("1.0", "end-1c"), "SELECT * FROM visits")

    def test_down_moves_and_escape_closes(self):
        self.text.insert("1.0", "SELECT v.")
        self.text.insert("end", " FROM visits v")
        self.text.mark_set("insert", "1.9")
        self.c.open()
        self.c._move(1)
        self.c.take()
        self.assertEqual(self.text.get("1.0", "end-1c"), "SELECT v.url FROM visits v")
        self.text.mark_set("insert", "1.0 lineend")
        self.c.open(force=True)
        self.assertTrue(self.c.visible())
        self.c.close()
        self.assertFalse(self.c.visible())


if __name__ == "__main__":
    unittest.main()
