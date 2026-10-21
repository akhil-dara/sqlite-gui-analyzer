"""The visual query builder's model: joins suggested from trusted links, the SQL it writes
(run read-only), filters with literal values, LIKE escaping, saving and loading."""

import os
import sqlite3
import unittest

from tests.helpers import TempDirTest
from engine.querymodel import Filter, Join, QueryModel, literal, suggest_joins


def history(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
    c.executemany("INSERT INTO urls VALUES (?,?,?)",
                  [(i, "https://site%d.example/" % i, "Site 1%d_%%" % i if i == 3 else
                    "Site %d" % i) for i in range(1, 21)])
    c.executemany("INSERT INTO visits VALUES (?,?,?)",
                  [(i, 1 + i % 20, 13344473607000000 + i) for i in range(1, 41)])
    c.commit()
    c.close()
    return path


class QueryModelTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        from engine.session import Session
        self.path = history(os.path.join(self.tmp, "History"))
        self.s = Session.open(self.path, hash_evidence=False)
        self.addCleanup(self.s.close)

    def run_sql(self, model):
        return self.s.conn().execute(model.to_sql()).fetchall()

    def test_a_join_is_suggested_from_the_trusted_link_and_runs(self):
        from engine.relations import relation_map
        m = QueryModel()
        v = m.add_table("visits")
        rmap = relation_map(self.s)
        rmap.build()
        joins = suggest_joins(rmap, m, "urls")
        self.assertEqual([(j.left_col, j.right, j.right_col) for j in joins],
                         [("id", v, "url")])
        u = m.add_table("urls", joins)
        self.assertEqual(m.unjoined(), [])
        m.toggle_column(v, "visit_time")
        m.toggle_column(u, "url")
        m.sort = [(v, "id", False)]
        rows = self.run_sql(m)
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[0], (13344473607000001, "https://site2.example/"))
        self.assertIn("LEFT JOIN \"urls\" AS \"urls\" ON", m.to_sql())

    def test_filters_write_literals_and_escape_like(self):
        m = QueryModel()
        u = m.add_table("urls")
        m.toggle_column(u, "title")
        m.filters = [Filter(u, "title", "contains", "3_%")]       # % and _ taken literally
        self.assertEqual(self.run_sql(m), [("Site 13_%",)])
        m.filters = [Filter(u, "title", "=", "O'Brien")]
        self.assertIn("'O''Brien'", m.to_sql())
        self.assertEqual(self.run_sql(m), [])
        m.filters = [Filter(u, "id", ">=", "19")]
        self.assertEqual(len(self.run_sql(m)), 2)
        m.filters = [Filter(u, "title", "starts with", "Site 2")]
        self.assertEqual(sorted(self.run_sql(m)), [("Site 2",), ("Site 20",)])
        m.filters = [Filter(u, "title", "is not empty")]
        self.assertEqual(len(self.run_sql(m)), 20)
        with self.assertRaises(ValueError):
            Filter(u, "title", "DROP TABLE", "x")
        self.assertEqual(literal("12"), "12")
        self.assertEqual(literal("1e5"), "'1e5'")
        self.assertEqual(literal(2.5), "2.5")

    def test_unjoined_tables_are_named_and_cross_joined(self):
        m = QueryModel()
        m.add_table("urls")
        v = m.add_table("visits")
        self.assertEqual(m.unjoined(), [v])
        self.assertIn("CROSS JOIN", m.to_sql())
        self.assertEqual(len(self.run_sql(m)), 20 * 40)

    def test_a_second_copy_of_a_table_gets_its_own_alias(self):
        m = QueryModel()
        a = m.add_table("urls")
        b = m.add_table("urls", [Join(None, "id", a, "id", "INNER", "same row")])
        self.assertNotEqual(a, b)
        m.toggle_column(b, "url")
        m.columns[0].label = "the url"
        m.distinct = True
        m.limit = 5
        rows = self.s.conn().execute(m.to_sql())
        self.assertEqual([d[0] for d in rows.description], ["the url"])
        self.assertEqual(len(rows.fetchall()), 5)

    def test_two_columns_of_the_same_name_get_distinct_headings(self):
        m = QueryModel()
        v = m.add_table("visits")
        u = m.add_table("urls", [Join(None, "id", v, "url", "LEFT", "test")])
        m.toggle_column(v, "url")
        m.toggle_column(u, "url")
        m.toggle_column(u, "title")
        self.assertEqual(m.headings(), ["visits.url", "urls.url", "title"])
        cur = self.s.conn().execute(m.to_sql())
        self.assertEqual([d[0] for d in cur.description], ["visits.url", "urls.url", "title"])
        m.columns[1].label = "address"
        self.assertEqual(m.headings(), ["url", "address", "title"])

    def test_meanings_are_shown_beside_the_value(self):
        from engine.querymodel import meaning_formatter, meanings_text, parse_meanings
        m = parse_meanings("1 = incoming\n\n2 = outgoing\nnot a line\n 3 =  missed ")
        self.assertEqual(m, {"1": "incoming", "2": "outgoing", "3": "missed"})
        with self.assertRaises(ValueError):
            parse_meanings("= nothing")
        f = meaning_formatter(m)
        self.assertEqual(f(2), "2 (outgoing)")
        self.assertEqual(f(2.0), "2 (outgoing)")
        self.assertEqual(f("3"), "3 (missed)")
        self.assertIsNone(f(9))                     # no meaning: the value as usual
        self.assertIsNone(f(None))
        self.assertEqual(parse_meanings(meanings_text(m)), m)
        q = QueryModel()
        u = q.add_table("urls")
        q.toggle_column(u, "id")
        q.columns[0].meanings = m
        self.assertEqual(QueryModel.from_dict(q.to_dict()).columns[0].meanings, m)
        old = {"tables": [["urls", "urls"]], "columns": [["urls", "id", None, None]]}
        self.assertEqual(QueryModel.from_dict(old).columns[0].meanings, {})   # older saves

    def test_count_per_shown_values(self):
        m = QueryModel()
        v = m.add_table("visits")
        u = m.add_table("urls", [Join(None, "id", v, "url", "INNER", "test")])
        m.toggle_column(u, "url")
        m.count = True
        m.sort = [(u, "url", False)]
        sql = m.to_sql()
        self.assertIn('COUNT(*) AS "rows"', sql)
        self.assertIn('GROUP BY "urls"."url"', sql)
        rows = self.run_sql(m)
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(r[1] for r in rows), 40)       # every visit counted once
        self.assertEqual(rows[0], ("https://site1.example/", 2))
        self.assertTrue(QueryModel.from_dict(m.to_dict()).count)

    def test_saved_and_loaded_unchanged(self):
        m = QueryModel()
        u = m.add_table("urls")
        m.toggle_column(u, "url")
        m.filters = [Filter(u, "url", "contains", "site1")]
        m.sort = [(u, "url", True)]
        again = QueryModel.from_dict(m.to_dict())
        self.assertEqual(again.to_sql(), m.to_sql())
        m.remove_table(u)
        self.assertEqual((m.tables, m.columns, m.filters, m.sort), ([], [], [], []))
        self.assertEqual(m.to_sql(), "")


class LinkRankTest(unittest.TestCase):
    """Which of several trusted links between two tables is used: names decide (real cases:
    WhatsApp chat/jid, PhonePe Core Data stores)."""

    def test_stems(self):
        from engine.querymodel import _stem
        self.assertEqual(_stem("chat_row_id"), "chat")
        self.assertEqual(_stem("jid"), "jid")
        self.assertEqual(_stem("ZCHAT"), "chat")
        self.assertEqual(_stem("ZTRANSACTIONENTITY"), "transaction")
        self.assertEqual(_stem("account_jid_row_id"), "account_jid")

    def test_identifiers_beat_dates_and_priorities(self):
        from engine.querymodel import _name_kind
        self.assertGreater(_name_kind("ZENTITYID"), _name_kind("ZCREATEDAT"))
        self.assertGreater(_name_kind("ZCATEGORYID"), _name_kind("ZPRIORITY"))
        self.assertGreater(_name_kind("chat_row_id"), _name_kind("sort_timestamp"))
        self.assertEqual(_name_kind("ZCAMPAIGN"), 0)


if __name__ == "__main__":
    unittest.main()
