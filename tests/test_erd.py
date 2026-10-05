"""The entity-relationship diagram's model and layout (engine.erd), without Tk: cardinality
from the schema and the values, junction tables as many-to-many, several relationships between
two tables kept apart, self-references, groups, card rows and their limit, a layered layout
without overlaps that is deterministic and reduces crossings, tidy(), the SVG, and speed."""

import os
import random
import sqlite3
import time
import unittest
import xml.dom.minidom

from tests.helpers import TempDirTest, within
from tests.fixtures import make_fixtures as fx
from tests.test_linkgraph import hub_links, mk, random_links
from database import DB as Database
from engine import erd, limits
from engine.erd import ErdModel, TableSpec, arrange
from engine.linkgraph import DB, Link
from engine.relations import relation_map


def spec(*cols, **kw):
    """TableSpec from 'name type [pk]' strings; unique columns: kw unique=('a', ...)."""
    out = []
    uniq = {}
    for c in cols:
        parts = c.split()
        pk = len(parts) > 2 and parts[2] == "pk"
        out.append((parts[0], parts[1], 1 if pk else 0))
        if pk:
            uniq[parts[0].lower()] = "PRIMARY KEY"
    for u in kw.get("unique", ()):
        uniq[u.lower()] = "UNIQUE index"
    return TableSpec(out, uniq, rows=kw.get("rows", 10))


def node(t):
    return (DB, t)


def people_model(**kw):
    """person referred to twice by message (sender and receiver), message referring to itself
    (a reply), and a junction table message_tag between message and tag."""
    links = [mk("message", "sender_id", "person", "_id", kind="declared"),
             mk("message", "receiver_id", "person", "_id", kind="declared"),
             mk("message", "reply_to", "message", "_id"),
             mk("message_tag", "message_id", "message", "_id"),
             mk("message_tag", "tag_id", "tag", "_id")]
    specs = {node("person"): spec("_id INTEGER pk", "name TEXT"),
             node("message"): spec("_id INTEGER pk", "sender_id INTEGER",
                                   "receiver_id INTEGER", "reply_to INTEGER", "body TEXT"),
             node("message_tag"): spec("message_id INTEGER", "tag_id INTEGER"),
             node("tag"): spec("_id INTEGER pk", "label TEXT")}
    return ErdModel(links, specs, **kw)


class ModelTest(unittest.TestCase):
    def setUp(self):
        limits.reset()
        self.addCleanup(limits.reset)

    def test_columns_markers_and_parallel_relationships(self):
        m = people_model()
        msg = m.tables[node("message")]
        self.assertEqual([c.name for c in msg.columns],
                         ["_id", "sender_id", "receiver_id", "reply_to", "body"])
        self.assertEqual([c.name for c in msg.columns if c.fk],
                         ["sender_id", "receiver_id", "reply_to"])
        self.assertTrue(msg.column("_id").pk and msg.column("_id").target)
        # two links message -> person: two relationships, two labels, never merged
        pair = [r for r in m.rels if r.src == node("message") and r.dst == node("person")]
        self.assertEqual(sorted(r.label for r in pair),
                         ["receiver_id → _id", "sender_id → _id"])
        # the self-reference
        loop = [r for r in m.rels if r.self_loop]
        self.assertEqual([(r.src_col, r.dst_col) for r in loop], [("reply_to", "_id")])
        # rids are the index
        self.assertEqual([r.rid for r in m.rels], list(range(len(m.rels))))

    def test_cardinality_from_schema_when_values_were_not_read(self):
        m = people_model()
        r = next(r for r in m.rels if r.src_col == "sender_id")
        self.assertEqual(r.cardinality, ("1", "*"))
        self.assertEqual(r.card_text(), "one-to-many")
        self.assertEqual(r.basis, "schema")
        self.assertIn("not all counted", r.evidence_src)
        self.assertIn("PRIMARY KEY", r.evidence_dst)
        # a unique referring column: one-to-one, from the schema
        links = [mk("profile", "person_id", "person", "_id")]
        specs = {node("profile"): spec("_id INTEGER pk", "person_id INTEGER",
                                       unique=("person_id",)),
                 node("person"): spec("_id INTEGER pk")}
        r = ErdModel(links, specs).rels[0]
        self.assertEqual((r.cardinality, r.basis), (("1", "1"), "schema"))
        # a target that is not unique: many on both sides
        links = [mk("a", "code", "b", "code")]
        specs = {node("a"): spec("code TEXT"), node("b"): spec("code TEXT")}
        r = ErdModel(links, specs).rels[0]
        self.assertEqual(r.cardinality, ("*", "*"))
        self.assertIn("not unique by the schema", r.evidence_dst)

    def test_junction_collapses_to_many_to_many(self):
        m = people_model()
        self.assertEqual(list(m.junctions), [node("message_tag")])
        self.assertIn(node("message_tag"), m.tables)
        nm = people_model(junctions=True)
        self.assertNotIn(node("message_tag"), nm.tables)
        self.assertEqual(nm.hidden_junctions, [node("message_tag")])
        r = next(r for r in nm.rels if r.junction is not None)
        self.assertEqual((r.src, r.src_col, r.dst, r.dst_col),
                         (node("message"), "_id", node("tag"), "_id"))
        self.assertEqual((r.cardinality, r.card_text(), r.style), (("*", "*"), "many-to-many",
                                                                   "nm"))
        self.assertEqual(r.label, "via message_tag")
        sql = r.join_sql(nm)
        self.assertIn('JOIN "message_tag" AS j ON j."message_id" = s."_id"', sql)
        self.assertIn('JOIN "tag" AS d ON d."_id" = j."tag_id"', sql)
        # a table something refers to, or the focused one, is never a junction
        self.assertEqual(list(people_model(keep=(node("message_tag"),)).junctions), [])

    def test_groups_of_alike_tables(self):
        m = ErdModel(hub_links(150))
        self.assertEqual(len(m.groups), 1)
        g = next(iter(m.groups.values()))
        self.assertEqual((len(g.members), g.anchor), (150, node("message")))
        self.assertEqual(g.lines(), ["message_row_id → message._id"])
        self.assertEqual(m.table_count(), 150 + len(m.tables))
        grs = [r for r in m.rels if r.group == g.key]
        self.assertEqual(len(grs), 1)
        self.assertEqual((len(grs[0].links), grs[0].src, grs[0].src_col), (150, g.key, "0"))
        self.assertIs(m.group_of(node("t007")), g)
        rows = m.card_rows(g.key, expanded=True)
        self.assertEqual(sum(1 for r in rows if r.kind == "member"), 150)
        self.assertEqual([r.kind for r in m.card_rows(g.key)], ["pattern", "toggle"])
        # the minimum is a limit; the kept (focused) table is never grouped
        limits.load({"limits": {"diagram_group_min": 1000}})
        self.assertEqual(ErdModel(hub_links(150)).groups, {})
        limits.reset()
        self.assertEqual(ErdModel(hub_links(5)).groups, {})

    def test_card_rows_limit_and_linked_only(self):
        cols = ["_id INTEGER pk"] + ["c%02d TEXT" % i for i in range(80)] + ["ref_id INTEGER"]
        links = [mk("wide", "ref_id", "other", "_id")]
        specs = {node("wide"): spec(*cols), node("other"): spec("_id INTEGER pk", "x TEXT")}
        m = ErdModel(links, specs)
        rows = m.card_rows(node("wide"))
        names = [r.text for r in rows if r.kind == "col"]
        self.assertEqual(len(names), 60)
        self.assertIn("ref_id", names)          # linked: always listed
        self.assertEqual(rows[-1].text, "22 more columns (limit diagram_card_columns)")
        self.assertEqual(m.cut_cards(), [node("wide")])
        self.assertEqual(len([r for r in m.card_rows(node("wide"), full=True)
                              if r.kind == "col"]), 82)
        linked = m.card_rows(node("wide"), mode="linked")
        self.assertEqual([r.text for r in linked], ["_id", "ref_id", "80 other columns"])
        limits.load({"limits": {"diagram_card_columns": 100}})
        self.assertEqual(len(m.card_rows(node("wide"))), 82)

    def test_rowid_target_and_unknown_columns(self):
        links = [mk("note_link", "note_id", "note", None)]
        m = ErdModel(links)                     # no specs: columns from the links
        note = m.tables[node("note")]
        self.assertEqual([c.name for c in note.columns], ["rowid"])
        r = m.rels[0]
        self.assertEqual((r.dst_col, r.dst_card), ("rowid", "1"))
        self.assertIn("d.rowid = s.\"note_id\"", r.join_sql())
        self.assertEqual(m.card_rows(node("note"))[-1].kind, "note")

    def test_cross_database_join_sql(self):
        l = mk("message", "jid", "wa_contacts", "jid", kind="value", cross=True)
        l.src, l.dst = ("messages.db", "message"), ("contacts.db", "wa_contacts")
        r = ErdModel([l]).rels[0]
        self.assertEqual(r.style, "cross")
        sql = r.join_sql()
        self.assertIn("ATTACH DATABASE", sql)
        self.assertIn('"contacts_db"."wa_contacts"', sql)


class FixtureCardinalityTest(TempDirTest):
    """Cardinality from the values the relation map read (the relations fixture)."""

    def setUp(self):
        TempDirTest.setUp(self)
        self.db = Database()
        self.db.open(fx.relations(self.tmp))
        self.addCleanup(self.db.close)
        mp = relation_map(self.db.session)
        mp.map_links()
        self.links = [l for l in (Link(r, DB, rows=mp.known_rows) for r in mp.links())
                      if l.confident]
        self.specs = dict((node(t), s) for t, s in erd.table_specs(
            self.db.session, mp.tables, mp.known_rows).items())

    def rel(self, model, src, col):
        return next(r for r in model.rels if r.src == node(src) and r.src_col == col)

    def test_one_to_many_one_to_one_and_junctions(self):
        m = ErdModel(self.links, self.specs)
        r = self.rel(m, "message", "chat_row_id")
        self.assertEqual((r.cardinality, r.basis), (("1", "*"), "schema and values"))
        self.assertIn("10 distinct values in 300 rows", r.evidence_src)
        r = self.rel(m, "receipt", "msg")
        self.assertEqual((r.card_text(), r.style), ("one-to-one", "declared"))
        self.assertIn("every one of its 100 rows", r.evidence_src)
        r = self.rel(m, "message_poll", "message_row_id")       # its INTEGER PRIMARY KEY
        self.assertEqual((r.card_text(), r.basis), ("one-to-one", "schema"))
        r = self.rel(m, "user_device", "device_id")             # a TEXT PRIMARY KEY target
        self.assertEqual((r.dst_card, r.evidence_dst), ("1", "unique: PRIMARY KEY"))
        r = self.rel(m, "note_link", "note_id")                 # the rowid of note
        self.assertEqual((r.dst_col, r.dst_card), ("rowid", "1"))
        self.assertIn(node("note_link"), m.junctions)
        nm = ErdModel(self.links, self.specs, junctions=True)
        self.assertNotIn(node("note_link"), nm.tables)
        self.assertTrue(any(r.junction == node("note_link") for r in nm.rels))
        lay = arrange(m)
        self.assertEqual(lay.overlaps(), [])
        xml.dom.minidom.parseString(lay.svg())

    def test_unique_indexes_from_the_schema(self):
        path = os.path.join(self.tmp, "u.db")
        c = sqlite3.connect(path)
        c.executescript("CREATE TABLE u(id INTEGER PRIMARY KEY, a TEXT UNIQUE, b INT, c INT,"
                        " d INT, e TEXT);"
                        "CREATE UNIQUE INDEX ub ON u(b); CREATE INDEX uc ON u(c);"
                        "CREATE UNIQUE INDEX ucd ON u(c, d);"
                        "CREATE UNIQUE INDEX ue ON u(e) WHERE e IS NOT NULL;"
                        "CREATE TABLE k(code TEXT PRIMARY KEY, v INT) WITHOUT ROWID;")
        c.close()
        db = Database()
        db.open(path)
        try:
            specs = erd.table_specs(db.session, ["u", "k"])
        finally:
            db.close()
        u = specs["u"].unique
        self.assertEqual(sorted(u), ["a", "b", "id"])
        self.assertEqual(u["a"], "UNIQUE constraint")
        self.assertEqual(u["b"], "UNIQUE index")
        self.assertIn("INTEGER PRIMARY KEY", u["id"])
        self.assertEqual(specs["k"].unique, {"code": "PRIMARY KEY"})
        self.assertFalse(specs["k"].rowid)


class LayoutTest(unittest.TestCase):
    def setUp(self):
        limits.reset()
        self.addCleanup(limits.reset)

    def check(self, lay):
        self.assertEqual(lay.overlaps(), [])
        x0, y0, x1, y1 = lay.bounds
        for c in lay.cards.values():
            self.assertTrue(x0 <= c.x and c.x + c.w <= x1 and y0 <= c.y and c.y + c.h <= y1)
        for r in lay.model.rels:
            pts = lay.routes[r.rid]
            # attached to the exact column rows, on a card edge
            a, b = lay.cards[r.src], lay.cards[r.dst]
            self.assertAlmostEqual(pts[0][1], a.y + a.port_off(r.src_col), places=3)
            self.assertAlmostEqual(pts[-1][1], b.y + b.port_off(r.dst_col), places=3)
            self.assertIn(round(pts[0][0], 3), (round(a.x, 3), round(a.x + a.w, 3)))
            self.assertIn(round(pts[-1][0], 3), (round(b.x, 3), round(b.x + b.w, 3)))
            for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
                self.assertTrue(abs(xa - xb) < 0.01 or abs(ya - yb) < 0.01)   # right angles

    def test_people_layout(self):
        m = people_model()
        lay = arrange(m)
        self.check(lay)
        # referring tables left of the tables they refer to
        c = lay.cards
        self.assertLess(c[node("message")].x, c[node("person")].x)
        self.assertLess(c[node("message_tag")].x, c[node("message")].x)
        # the two message -> person connectors: separate lanes, both labelled
        pair = [r for r in m.rels if r.dst == node("person")]
        self.assertNotEqual(lay.routes[pair[0].rid], lay.routes[pair[1].rid])
        self.assertEqual(sum(1 for r in pair if r.rid in lay.labels), 2)
        # the self-reference: a loop on the card's right side
        loop = next(r for r in m.rels if r.self_loop)
        pts = lay.routes[loop.rid]
        right = c[node("message")].x + c[node("message")].w
        self.assertEqual((pts[0][0], pts[-1][0]), (right, right))
        self.assertTrue(all(x >= right for x, _y in pts))
        # labels cover no card
        for rid, rect in lay.labels.items():
            for card in lay.cards.values():
                self.assertFalse(erd._overlap(rect, card.rect()), (rid, card.key))

    def test_deterministic(self):
        a = arrange(ErdModel(random_links(120, 200, seed=3)))
        b = arrange(ErdModel(random_links(120, 200, seed=3)))
        self.assertEqual(a.positions(), b.positions())
        self.assertEqual(a.routes, b.routes)
        self.assertEqual(a.svg(), b.svg())
        self.check(a)

    def test_crossings_reduced(self):
        # s_i refers to t_(5-i): the names' order crosses every pair of lines
        links = [mk("s%d" % i, "ref", "t%d" % (5 - i), "_id") for i in range(6)]
        links += [mk("t%d" % i, "up", "hub", "_id") for i in range(6)]
        lay = arrange(ErdModel(links, group_min=100))
        self.assertEqual(lay.stats["initial_crossings"], 15)
        self.assertEqual(lay.stats["crossings"], 0)
        self.assertEqual(erd.count_crossings(
            [["s%d" % i for i in range(6)], ["t%d" % i for i in range(6)]],
            [("s%d" % i, "t%d" % (5 - i)) for i in range(6)]), 15)
        self.check(lay)
        # the sources are in the order of the tables they refer to
        ys = dict((k[1], c.y) for k, c in lay.cards.items())
        srcs = sorted(("s%d" % i for i in range(6)), key=lambda n: ys[n])
        targets = [ys["t%d" % (5 - int(n[1]))] for n in srcs]
        self.assertEqual(targets, sorted(targets))

    def test_components_side_by_side_and_groups(self):
        links = hub_links(150) + [mk("x1", "y_id", "y", "_id"), mk("z1", "w_id", "w", "_id")]
        lay = arrange(ErdModel(links))
        self.assertEqual(lay.stats["components"], 3)
        self.check(lay)
        g = next(iter(lay.model.groups))
        listed = arrange(lay.model, expanded=[g])
        self.assertEqual(listed.cards[g].h, erd.HEAD_H + erd.ROW_H * 151 + erd.BOTTOM)
        self.check(listed)

    def test_tidy_keeps_positions_but_removes_overlaps(self):
        lay = arrange(people_model())
        before = lay.positions()
        msg, person = node("message"), node("person")
        # drop person on message: they overlap
        lay.move(person, lay.cards[msg].x + 10, lay.cards[msg].y + 10)
        self.assertTrue(lay.manual)
        self.assertEqual(lay.overlaps(), [(msg, person)] if lay.cards[msg].x <
                         lay.cards[person].x else [(person, msg)])
        lay.tidy()
        self.check(lay)
        after = lay.positions()
        self.assertEqual(after[person][0], before[msg][0])        # aligned with message
        for k in before:
            if k != person:
                self.assertEqual(after[k][0], before[k][0])       # x kept
        # arrange() with positions keeps them
        again = arrange(people_model(), positions=after)
        self.assertEqual(again.positions(), after)
        self.assertTrue(again.manual)
        self.check(again)

    def test_svg_escapes_and_lists_everything(self):
        links = [mk('a<b&"c', "x<y", "t'2", "_id"), mk("t'2", "z&", 'a<b&"c', "x<y")]
        specs = {node('a<b&"c'): spec("x<y INTEGER", "plain TEXT"),
                 node("t'2"): spec("_id INTEGER pk", "z& INTEGER", "w TEXT")}
        m = ErdModel(links, specs)
        text = arrange(m).svg(title="R & D")
        doc = xml.dom.minidom.parseString(text)
        texts = [n.firstChild.data for n in doc.getElementsByTagName("text") if n.firstChild]
        for name in ('a<b&"c', "t'2", "x<y", "plain", "_id", "z&", "w"):
            self.assertIn(name, texts)
        self.assertIn("&lt;", text)
        self.assertEqual(text.count('<polyline class="link"'), 2)
        self.assertEqual(text.count('<polyline class="card"'), 4)     # both ends of each
        # the same model: the same document
        self.assertEqual(text, arrange(ErdModel(links, specs)).svg(title="R & D"))

    def test_300_tables_are_laid_out_fast(self):
        rnd = random.Random(11)
        links = random_links(300, 540, seed=5)
        specs = {}
        for l in links:
            for n in (l.src, l.dst):
                if n not in specs:
                    specs[n] = spec("_id INTEGER pk", *["c%d TEXT" % i for i in
                                                        range(rnd.randrange(2, 30))] +
                                    ["ref_%d INTEGER" % i for i in range(4)])
        t0 = time.perf_counter()
        m = ErdModel(links, specs)
        lay = arrange(m)
        seconds = time.perf_counter() - t0
        self.assertGreaterEqual(len(m.tables), 250)
        self.assertEqual(lay.overlaps(), [])
        within(self, seconds, 2.5, "diagram layout")
        self.assertLessEqual(lay.stats["crossings"], lay.stats["initial_crossings"])


if __name__ == "__main__":
    unittest.main()
