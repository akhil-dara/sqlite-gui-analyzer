"""Fields inside a column's values: paths found in decoded plists / JSON, values read out
by path (the first path with a value), and field columns in the query model."""
import json
import os
import plistlib
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engine import querymodel as qm  # noqa: E402
from engine.decode import fields as fl  # noqa: E402

SENT = plistlib.dumps({"paidFrom": [{"accountHolderName": "A SENDER", "amount": 38000}],
                       "to": [{"name": "b receiver"}], "sentAt": 1735731671413},
                      fmt=plistlib.FMT_BINARY)
RECEIVED = json.dumps({"from": {"name": "C Sender"}, "amount": 32500,
                       "receivedIn": [{"accountHolderName": "D OWNER"}],
                       "odd key": {"x": 1}}).encode()


class PathsTest(unittest.TestCase):
    def test_join_and_split_round_trip(self):
        p = fl.join(fl.join(fl.join("", "to"), 0), "name")
        self.assertEqual(p, "to[0].name")
        self.assertEqual(fl.split(p), ["to", 0, "name"])
        q = fl.join("", "odd key")
        self.assertEqual(q, '["odd key"]')
        self.assertEqual(fl.split(q + ".x"), ["odd key", "x"])
        self.assertIsNone(fl.split("to[x"))

    def test_discover_counts_paths_kinds_and_examples(self):
        d = fl.discover([SENT, RECEIVED, None, 7, "plain text", b"\x00\x01junk"])
        self.assertEqual((d.read, d.structured), (6, 2))
        by = dict((f.path, f) for f in d.fields)
        self.assertEqual(by["paidFrom[0].accountHolderName"].count, 1)
        self.assertEqual(by["from.name"].examples, ["C Sender"])
        self.assertEqual(by["sentAt"].kinds_text(), "integer")
        self.assertIn('["odd key"].x', by)
        self.assertFalse(d.cut)

    def test_the_list_says_when_it_was_cut(self):
        d = fl.discover([SENT, RECEIVED], max_paths=3)
        self.assertEqual(len(d.fields), 3)
        self.assertTrue(d.cut)

    def test_read_takes_the_first_path_with_a_value(self):
        paths = ["from.name", "paidFrom[0].accountHolderName"]
        self.assertEqual(fl.read(SENT, paths), ("A SENDER", "paidFrom[0].accountHolderName"))
        self.assertEqual(fl.read(RECEIVED, paths), ("C Sender", "from.name"))
        self.assertEqual(fl.read(None, paths), (None, None))
        self.assertEqual(fl.read(RECEIVED, ["nothing.here"]), (None, None))
        # a structure at the path comes back as JSON text, never lost
        self.assertEqual(fl.read(RECEIVED, ['["odd key"]'])[0], '{"x": 1}')


class ModelTest(unittest.TestCase):
    def test_field_columns_select_the_stored_value_and_save(self):
        m = qm.QueryModel()
        a = m.add_table("t")
        m.toggle_column(a, "data")
        f = m.add_field(a, "data", ["root.from.name", "root.paidFrom[0].x"], "Sender")
        self.assertTrue(m.has_column(a, "data"))            # the raw column stays its own
        self.assertEqual(m.fields(), [(1, f)])
        self.assertEqual(m.headings(), ["data", "Sender"])
        self.assertIn('"t"."data" AS "Sender"', m.to_sql())
        m.toggle_column(a, "data")                           # unticking the raw column
        self.assertEqual([c.paths for c in m.columns], [f.paths])   # keeps the field
        back = qm.QueryModel.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(back.columns[0].paths, f.paths)
        self.assertEqual(back.columns[0].label, "Sender")
        g = m.add_field(a, "data", ["root.to[0].name"])
        self.assertEqual(g.field_name(), "data › to[0].name")
        self.assertIn("Sender: read out of t.data", qm.describe(m))

    def test_a_field_cannot_be_summarised(self):
        c = qm.Column("t", "data", agg="count", paths=["x"])
        self.assertIsNone(c.agg)


class ReadFieldsTest(unittest.TestCase):
    def test_rows_get_the_fields_in_place_of_the_stored_values(self):
        from query_tab import read_fields
        rows = [(1, SENT, SENT), (2, RECEIVED, RECEIVED), (3, None, None)]
        got = read_fields(rows, [(1, "t", "data", ["from.name", "paidFrom[0].accountHolderName"]),
                                 (2, "t", "data", ["amount", "paidFrom[0].amount"])])
        self.assertEqual(got, [[1, "A SENDER", 38000], [2, "C Sender", 32500],
                               [3, None, None]])


class JoinByFieldTest(unittest.TestCase):
    """A table joined on a field: kept without a match, repeated for several matches, and
    never part of the SQL (SQLite cannot read inside the values)."""

    def test_model_keeps_the_joined_table_out_of_the_sql(self):
        m = qm.QueryModel()
        a = m.add_table("tx")
        m.toggle_column(a, "type")
        b = m.add_by_field("contacts", a, "data", ["from.phone"], "phone")
        m.toggle_column(b, "name")
        m.filters.append(qm.Filter(b, "name", "contains", "x"))
        m.sort.append((b, "name", False))
        sql = m.to_sql()
        self.assertNotIn("contacts", sql.split("~key")[0])
        self.assertIn('"tx"."data" AS "~key contacts"', sql)
        self.assertNotIn("WHERE", sql)
        self.assertNotIn("ORDER BY", sql)
        self.assertEqual(m.unjoined(), [])
        back = qm.QueryModel.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(back.table(b).by["to"], "phone")
        self.assertIn("matched by the tool after the query", qm.describe(m))

    def test_rows_are_matched_kept_and_repeated(self):
        import sqlite3
        import tempfile
        from engine.session import Session
        from query_tab import join_by_fields
        d = tempfile.mkdtemp()
        p = os.path.join(d, "c.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE contacts (phone TEXT, name TEXT)")
        c.executemany("INSERT INTO contacts VALUES (?,?)", [("9000000001", "Ann"),
                      ("9000000002", "Bob"), ("9000000002", "Bobby")])
        c.commit()
        c.close()
        s = Session.open(p, hash_evidence=False)
        try:
            m = qm.QueryModel()
            a = m.add_table("tx")
            m.toggle_column(a, "type")
            b = m.add_by_field("contacts", a, "data", ["from.phone"], "phone")
            m.toggle_column(b, "name")
            v1 = json.dumps({"from": {"phone": 9000000001}})      # a number matches text
            v2 = json.dumps({"from": {"phone": "9000000002"}})
            v3 = json.dumps({"from": {"phone": "nobody"}})
            rows = [("A", v1), ("B", v2), ("C", v3), ("D", None)]
            cols, out = join_by_fields(["type", "~key contacts"], rows,
                                       [(b, "contacts", m.table(b).by, ["name"], s)],
                                       list(m.columns), [True, False], m.headings(), {0: 0},
                                       1000)
        finally:
            s.close()
        self.assertEqual(cols, ["type", "name"])
        self.assertEqual(out, [["A", "Ann"], ["B", "Bob"], ["B", "Bobby"], ["C", None],
                               ["D", None]])


class BrowseFieldsTest(unittest.TestCase):
    """Browse adds chosen fields as derived columns (named by the full path, 'ƒ ' first)."""

    def test_derived_columns(self):
        from fields_ui import BrowseFields, pretty_path, short_path

        class DBStub(object):
            session = object()

            def columns(self, table):
                return [("id", "INTEGER"), ("data", "BLOB")] if table == "tx" else []

        class TagsStub(object):
            settings = {}

            def save_settings(self):
                pass

        class App(object):
            db = DBStub()
            tags = TagsStub()
        bf = BrowseFields(App())
        bf._add("tx", "data", "Sender", ["from.name", "paidFrom[0].accountHolderName"])
        bf._add("tx", "data", "amount", ["amount", "paidFrom[0].amount"])
        bf._add("tx", "data", "amount", ["amount"])          # the same label again: (2)
        self.assertEqual([f[0] for f in bf.fields_of("tx", "data")],
                         ["Sender", "amount", "amount (2)"])
        d = bf.derived("tx")
        self.assertEqual([c.name for c in d], ["ƒ Sender", "ƒ amount", "ƒ amount (2)"])
        self.assertEqual(d[0].fn(SENT), "A SENDER")
        self.assertEqual(d[0].fn(RECEIVED), "C Sender")
        self.assertEqual(d[1].fn(SENT), 38000)
        self.assertIsNone(d[1].fn(bytes([0]) + b"junk"))
        self.assertEqual(bf.fields_of("tx", "other"), [])
        self.assertEqual(bf.fields_of("nosuch"), [])        # a table without the column
        self.assertEqual(App.tags.settings["derived_columns"]["tx"][0][0], "Sender")
        self.assertEqual(short_path("root.from.name"), "from.name")
        self.assertEqual(pretty_path('["processed.isValidFeed"]'), "processed.isValidFeed")
        self.assertEqual(pretty_path("root.to[0].name"), "to[0].name")


if __name__ == "__main__":
    unittest.main()
