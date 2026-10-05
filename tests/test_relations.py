"""Column relationships (engine.relations): discovery, scoring, value checks and related rows."""

import unittest
from unittest import mock

from tests.helpers import norm
from tests.fixtures import make_fixtures as fx
from tests.test_session import SessionTestBase, open_native
from engine import limits
from engine.backends import Filter
from engine.relations import (MIN_SCORE, ROWID, RelationMap, coerce, equals_expr, id_like,
                              is_confident, name_base, plain_reason, relation_map)
from engine.session import IMMUTABLE, NATIVE


def find(rels, table, column):
    got = [r for r in rels if r.other == table and r.other_column == column]
    return got[0] if got else None


def result_key(res):
    return (res.table, str(res.column), res.count,
            [(r.locator, tuple(norm(v) for v in r.values)) for r in res.rows])


class NameRulesTest(unittest.TestCase):
    def test_name_base(self):
        for col, want in (("message_row_id", "message"), ("chat_id", "chat"),
                          ("docid", "doc"), ("sender_jid_row_id", "sender_jid"),
                          ("_id", None), ("id", None), ("status", None), ("x_id", None)):
            self.assertEqual(name_base(col), want, col)

    def test_id_like(self):
        for col in ("message_row_id", "key_id", "jid", "sender_jid", "blockid", "raw_hash"):
            self.assertTrue(id_like(col), col)
        for col in ("_id", "id", "timestamp", "name", "status", "subject"):
            self.assertFalse(id_like(col), col)

    def test_coerce_to_the_target_affinity(self):
        self.assertEqual(coerce("12", "INTEGER"), 12)
        self.assertEqual(coerce(" 7 ", "NUMERIC"), 7)
        self.assertEqual(coerce("abc", "INTEGER"), "abc")
        self.assertEqual(coerce(12, "TEXT"), "12")
        self.assertEqual(coerce(2.5, "TEXT"), "2.5")
        self.assertEqual(coerce(3, "REAL"), 3.0)
        self.assertEqual(coerce(b"\x01", "INTEGER"), b"\x01")
        self.assertEqual(coerce("12", "BLOB"), "12")
        self.assertIsNone(equals_expr(None))
        e = equals_expr("abc")
        self.assertEqual((e.kind, e.op, e.operand), ("cmp", "=", ("text", "abc")))
        self.assertTrue(e.match("abc") and not e.match("ABC"))


class RelationMapTest(SessionTestBase):
    def setUp(self):
        SessionTestBase.setUp(self)
        self.path = fx.relations(self.tmp)
        self.s = self.open(self.path)
        self.m = relation_map(self.s)

    def test_one_map_per_session_built_once(self):
        self.assertIs(relation_map(self.s), self.m)
        self.m.build()
        took = self.m.build_seconds
        self.m.build()
        self.assertEqual(self.m.build_seconds, took)
        tables, columns, links = self.m.summary()
        self.assertEqual(tables, 14)
        self.assertGreater(columns, 30)
        self.assertGreater(links, 10)
        self.assertEqual(self.m.problems, [])

    def test_declared_foreign_key_both_directions(self):
        r = find(self.m.for_column("receipt", "msg"), "message", "_id")
        self.assertEqual((r.kind, r.direction, r.score), ("fk", "out", 1.0))
        self.assertIn("FOREIGN KEY", r.why()[0])
        back = find(self.m.for_column("message", "_id"), "receipt", "msg")
        self.assertEqual((back.kind, back.direction, back.score), ("fk", "in", 1.0))

    def test_name_links(self):
        rels = self.m.for_column("message_poll_option", "message_row_id")
        r = find(rels, "message", "_id")
        self.assertEqual((r.kind, r.direction, r.base), ("name", "out", 0.8))
        # the other columns naming message._id hold the same kind of value
        peer = find(rels, "message_poll", "message_row_id")
        self.assertEqual((peer.kind, peer.direction, peer.via), ("shared", "peer",
                                                                 ("message", "_id")))
        self.assertGreaterEqual(peer.score, MIN_SCORE)
        self.assertIsNotNone(find(rels, "receipt", "msg"))
        # the last words of a name: sender_jid_row_id -> jid
        tail = find(self.m.for_column("message_vote", "sender_jid_row_id"), "jid", "_id")
        self.assertEqual((tail.kind, tail.base), ("name", 0.65))
        # a table without INTEGER PRIMARY KEY is referred to by its rowid
        note = find(self.m.for_column("note_link", "note_id"), "note", ROWID)
        self.assertEqual(note.kind, "name")
        self.assertIsNotNone(find(self.m.for_column("note", ROWID), "note_link", "note_id"))
        # a key column lists every column that refers to its table
        incoming = self.m.for_column("message", "_id")
        self.assertEqual(set((r.other, r.other_column) for r in incoming if r.direction == "in"),
                         set([("message_poll", "message_row_id"),
                              ("message_poll_option", "message_row_id"),
                              ("message_vote", "message_row_id"), ("receipt", "msg"),
                              ("settings", "message_id"), ("note_link", "message_row_id")]))
        # a table's own key is not a reference to itself
        self.assertIsNone(find(self.m.for_column("user_device", "device_id"),
                               "user_device", "device_id"))

    def test_misleading_name_scores_low_once_checked(self):
        r = find(self.m.for_column("settings", "message_id"), "message", "_id")
        self.assertEqual(r.base, 0.8)
        self.assertTrue(self.m.verify(r))
        self.assertEqual(r.links[0].overlap.found, 0)
        self.assertLess(r.score, 0.3)
        self.assertIn("0% of 40 sampled values found", r.why()[-1])
        # the result is cached per column pair: a later query sees it
        again = find(self.m.for_column("message", "_id"), "settings", "message_id")
        self.assertLess(again.score, MIN_SCORE)
        # and a column that shares the key through it is no better than its weakest link
        peer = find(self.m.for_column("message_poll", "message_row_id"), "settings", "message_id")
        self.assertLess(peer.score, MIN_SCORE)

    def test_verified_name_link_scores_high(self):
        r = find(self.m.for_column("message_poll", "message_row_id"), "message", "_id")
        self.m.verify(r)
        self.assertGreater(r.score, 0.9)
        self.assertIn("100% of 60 sampled values found", r.why()[-1])

    def test_text_against_integer(self):
        rels = self.m.for_column("labels", "chat_row_id")
        r = find(rels, "chat", "_id")
        same_type = find(self.m.for_column("message", "chat_row_id"), "chat", "_id")
        self.assertLess(r.base, same_type.base)
        self.assertTrue(any("types differ" in w for w in r.why()))
        self.m.verify(r)
        self.assertEqual(r.links[0].overlap.found, r.links[0].overlap.sampled)
        # values are converted to the other column's type: '3' finds chat 3 and back
        res = self.m.rows_for(r, "3")
        self.assertEqual((res.count, res.rows[0].values[0]), (1, 3))
        back = find(self.m.for_column("chat", "_id"), "labels", "chat_row_id")
        res = self.m.rows_for(back, 3)
        self.assertEqual(res.count, 3)
        self.assertTrue(all(row.values[1] == "3" for row in res.rows))

    def test_without_rowid_target(self):
        r = find(self.m.for_column("user_device", "device_id"), "device", "device_id")
        self.assertEqual(r.kind, "name")
        res = self.m.rows_for(r, "dev-03")
        self.assertEqual(res.count, 1)
        self.assertEqual(res.rows[0].locator.kind, "pk")
        self.assertEqual(res.rows[0].values, ["dev-03", "model 3"])

    def test_flag_column_is_down_weighted(self):
        r = find(self.m.for_column("message", "status_id"), "status", "_id")
        self.assertGreaterEqual(r.base, MIN_SCORE)
        self.m.verify(r)
        self.assertTrue(r.links[0].overlap.flag_like)
        self.assertLess(r.score, MIN_SCORE)
        self.assertIn("flag or status code", r.why()[-1])

    def test_unindexed_and_rowid_targets_are_checked(self):
        for table, column, other, ocol in (("message", "_id", "message_vote", "message_row_id"),
                                           ("note_link", "note_id", "note", ROWID)):
            r = find(self.m.for_column(table, column), other, ocol)
            self.m.verify(r)
            self.assertGreater(r.score, 0.9, (table, column, other))

    def test_related_rows(self):
        got = dict(((res.table, res.column), res)
                   for res in self.m.related_rows("message_poll", "message_row_id", 10))
        self.assertEqual(got[("message", "_id")].count, 1)
        self.assertEqual(got[("message_poll_option", "message_row_id")].count, 3)
        self.assertEqual(got[("message_vote", "message_row_id")].count, 2)
        self.assertEqual(got[("receipt", "msg")].count, 1)
        self.assertEqual(got[("note_link", "message_row_id")].count, 1)
        # by its name alone settings.message_id looks like a message reference ...
        self.assertEqual(got[("settings", "message_id")].count, 0)
        # ... its values say otherwise: with verify=True weaker links are left out
        checked = dict(((res.table, res.column), res) for res in self.m.related_rows(
            "message_poll", "message_row_id", 10, verify=True))
        self.assertNotIn(("settings", "message_id"), checked)
        self.assertEqual(set(checked), set(got) - set([("settings", "message_id")]))
        rels = self.m.for_column("message_poll", "message_row_id")
        everything = list(self.m.related_rows("message_poll", "message_row_id", 10,
                                              relations=rels))
        self.assertEqual(len(everything), len(rels))
        # the row limit keeps the first rows and the full count
        res = self.m.rows_for(find(rels, "message_poll_option", "message_row_id"), 10, limit=2)
        self.assertEqual((res.count, len(res.rows)), (3, 2))
        # a value that is no row id finds nothing, without scanning
        res = self.m.rows_for(find(rels, "message", "_id"), "abc")
        self.assertEqual((res.count, res.note), (0, "not a row id"))

    def test_cancel(self):
        seen = []
        for res in self.m.related_rows("message", "_id", 10, cancel=lambda: bool(seen)):
            seen.append(res)
        self.assertEqual(len(seen), 1)
        r = find(self.m.for_column("message", "_id"), "message_vote", "message_row_id")
        self.assertFalse(self.m.verify(r, cancel=lambda: True))
        self.assertIsNone(r.links[0].overlap)

    def test_whole_map(self):
        everything = self.m.all_relations()
        self.assertIn(("message", "chat_row_id"), everything)
        self.assertTrue(all(isinstance(v, list) for v in everything.values()))


class BareNameTest(SessionTestBase):
    def test_number_column_named_after_a_table(self):
        import os
        import sqlite3
        path = os.path.join(self.tmp, "history.db")
        c = sqlite3.connect(path)
        c.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT);"
                        "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, note TEXT);"
                        "CREATE TABLE notes(id INTEGER PRIMARY KEY);")
        c.executemany("INSERT INTO urls VALUES (?, ?)", [(i, "u%d" % i) for i in range(1, 30)])
        c.executemany("INSERT INTO visits VALUES (?, ?, ?)",
                      [(i, 1 + i % 29, "x") for i in range(1, 60)])
        c.commit()
        c.close()
        m = relation_map(self.open(path))
        r = find(m.for_column("visits", "url"), "urls", "id")
        self.assertEqual((r.kind, r.base), ("name", 0.65))
        m.verify(r)
        self.assertTrue(is_confident(r))
        # a TEXT column named after a table is not a reference by its name
        self.assertIsNone(find(m.for_column("visits", "note"), "notes", "id"))
        self.assertIsNone(find(m.for_column("urls", "url"), "urls", "id"))


class MapTest(SessionTestBase):
    """The whole-database map: links, confident relations and the counts menus show."""

    def setUp(self):
        SessionTestBase.setUp(self)
        self.m = relation_map(self.open(fx.relations(self.tmp)))

    def test_links_and_mapping(self):
        pairs = set((r.table, str(r.column), r.other, str(r.other_column), r.kind)
                    for r in self.m.links())
        for want in (("receipt", "msg", "message", "_id", "fk"),
                     ("message_poll", "message_row_id", "message", "_id", "name"),
                     ("message_vote", "sender_jid_row_id", "jid", "_id", "name"),
                     ("note_link", "note_id", "note", "None", "name"),
                     ("user_device", "device_id", "device", "device_id", "name")):
            self.assertIn(want, pairs)
        seen = []
        self.assertIsNone(self.m.known_confident("message_poll", "message_row_id"))
        self.assertTrue(self.m.map_links(progress=lambda d, t: seen.append((d, t))))
        self.assertTrue(self.m.mapped)
        self.assertEqual(seen[-1], (14, 14))
        self.assertEqual(self.m.known_rows("message"), 300)
        confident = dict(((r.table, str(r.column)), r) for r in self.m.links() if is_confident(r))
        self.assertIn(("receipt", "msg"), confident)
        self.assertIn(("labels", "chat_row_id"), confident)       # TEXT vs INTEGER, verified
        self.assertNotIn(("settings", "message_id"), confident)   # misleading name
        self.assertNotIn(("message", "status_id"), confident)     # a 0/1 flag
        self.assertEqual(plain_reason(confident[("receipt", "msg")]),
                         "declared foreign key; same values found in 100% of samples")

    def test_confident_and_quick_counts(self):
        rels = self.m.confident("message_poll", "message_row_id")
        others = set((r.other, r.other_column) for r in rels)
        self.assertIn(("message", "_id"), others)
        self.assertNotIn(("settings", "message_id"), others)       # weak: never in menus
        counts = dict(((r.other, r.other_column), n)
                      for r, n in self.m.quick_counts("message_poll", "message_row_id", 10))
        self.assertEqual(counts, {("message", "_id"): 1,
                                  ("message_poll_option", "message_row_id"): 3,
                                  ("message_vote", "message_row_id"): 2,
                                  ("note_link", "message_row_id"): 1,
                                  ("receipt", "msg"): 1})
        # a value no related table holds, and trivial values: nothing
        self.assertEqual(self.m.quick_counts("message_poll", "message_row_id", 999999), [])
        for v in (None, 0, "", b""):
            self.assertEqual(self.m.quick_counts("message_poll", "message_row_id", v), [])
        # a column without confident relations: nothing to offer
        self.assertEqual(self.m.confident("message", "status_id"), [])
        self.assertEqual(self.m.quick_counts("message", "status_id", 1), [])


class RelatedRowsDifferentialTest(SessionTestBase):
    """related_rows gives the same rows whether SQLite serves the tables or they are read
    natively, and Session.lookup agrees with the native filter for every storage class."""

    def test_sql_and_native_agree(self):
        path = fx.relations(self.tmp)
        sql, native = self.open(path), open_native(self, path)
        self.assertEqual((sql.mode, native.mode), (IMMUTABLE, NATIVE))
        ms, mn = RelationMap(sql), RelationMap(native)
        cases = [("message_poll", "message_row_id", 10), ("message", "_id", 25),
                 ("labels", "chat_row_id", "4"), ("chat", "_id", 4),
                 ("user_device", "device_id", "dev-05"), ("jid", "_id", 7),
                 ("note_link", "note_id", 3), ("note", ROWID, 3), ("message", "_id", "25"),
                 ("message", "text_data", "message 5"), ("message", "_id", None)]
        total = 0
        for table, column, value in cases:
            a = [result_key(r) for r in ms.related_rows(table, column, value, min_score=0)]
            b = [result_key(r) for r in mn.related_rows(table, column, value, min_score=0)]
            self.assertEqual(a, b, (table, column, value))
            total += sum(k[2] for k in a)
            self.assertTrue(all(r.source == "native"
                                for r in mn.related_rows(table, column, value, min_score=0)))
        self.assertGreater(total, 20)
        for table, column in (("message_poll", "message_row_id"), ("labels", "chat_row_id")):
            for rs, rn in zip(ms.for_column(table, column), mn.for_column(table, column)):
                ms.verify(rs)
                mn.verify(rn)
                self.assertEqual((rs.other, rs.other_column, rs.score),
                                 (rn.other, rn.other_column, rn.score))

    def test_large_native_tables_use_the_file_index(self):
        # Tables too large to index in memory are read through their own index B-tree
        path = fx.relations(self.tmp)
        sql, native = self.open(path), open_native(self, path)
        ms, mn = RelationMap(sql), RelationMap(native)
        with mock.patch.dict(limits._values, {"relations_native_index_rows": 10}):
            for table, column, value in (("chat", "_id", 4), ("message_poll", "message_row_id", 10),
                                         ("message", "_id", 25), ("labels", "chat_row_id", "4")):
                a = [result_key(r) for r in ms.related_rows(table, column, value, min_score=0)]
                b = [result_key(r) for r in mn.related_rows(table, column, value, min_score=0)]
                self.assertEqual(a, b, (table, column, value))
            via_index = [r for r in mn.related_rows("chat", "_id", 4, min_score=0)
                         if r.note == "found through the table's index"]
            self.assertEqual([(r.table, r.column, r.count) for r in via_index],
                             [("message", "chat_row_id", 30)])
            # value checks agree too
            for rs, rn in zip(ms.for_column("chat", "_id"), mn.for_column("chat", "_id")):
                ms.verify(rs)
                mn.verify(rn)
                self.assertEqual(rs.score, rn.score, rs)

    def test_lookup_matches_the_native_filter(self):
        path = fx.mixed_values(self.tmp)
        sql, native = self.open(path), open_native(self, path)
        values = set()
        for row in native.iter_rows("vals"):
            values.update(v for v in row.values if not isinstance(v, (bytes, type(None))))
        for column in ("a", "t", "i", "r"):
            for v in sorted(values, key=repr):
                expr = equals_expr(v)
                if expr is None:
                    continue
                a = sql.lookup("vals", column, expr, limit=1000)
                b = native.lookup("vals", column, expr, limit=1000)
                self.assertEqual((a[0], [r.locator for r in a[1]]),
                                 (b[0], [r.locator for r in b[1]]), (column, v))
                self.assertEqual(a[0], sql.count("vals", self.flt(column, expr)), (column, v))

    @staticmethod
    def flt(column, expr):
        return Filter(col_exprs={column: expr})


if __name__ == "__main__":
    unittest.main()
