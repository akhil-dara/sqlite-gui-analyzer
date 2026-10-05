"""Name rules of engine.relations beyond <x>_id: table prefixes (moz_), Apple Core Data, parent
self-references, a column named fk, the distinct-value thresholds of trusted links, and integer
id links between databases (engine.crossdb). Every database is built in a temporary folder."""

import os
import sqlite3

from tests.test_session import SessionTestBase
from engine import limits
from engine.crossdb import find_links
from engine.linkgraph import Link, LinkGraph
from engine.relations import ROWID, is_confident, plain_reason, relation_map, table_prefixes


def build(path, script, rows):
    """A database from a CREATE script and {table: [row tuples]}."""
    c = sqlite3.connect(path)
    c.executescript(script)
    for table, values in rows.items():
        if values:
            c.executemany("INSERT INTO %s VALUES (%s)" % (table, ",".join("?" * len(values[0]))),
                          values)
    c.commit()
    c.close()
    return path


def link(m, table, column, other=None):
    got = [r for r in m.links() if r.table == table and r.column == column and
           (other is None or r.other == other)]
    return got[0] if got else None


class Base(SessionTestBase):
    def setUp(self):
        SessionTestBase.setUp(self)
        self.addCleanup(limits.reset)

    def mapped(self, name, script, rows):
        m = relation_map(self.open(build(os.path.join(self.tmp, name), script, rows)))
        self.assertTrue(m.map_links())
        return m


FIREFOX = """
    CREATE TABLE moz_origins(id INTEGER PRIMARY KEY, host TEXT);
    CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, origin_id INTEGER);
    CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, from_visit INTEGER, place_id INTEGER,
                                   visit_date INTEGER);
    CREATE TABLE moz_annos(id INTEGER PRIMARY KEY, place_id INTEGER, content TEXT);
    CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER DEFAULT NULL,
                               parent INTEGER, title TEXT);
"""


def firefox_rows():
    bookmarks = [(1, 2, None, 0, "root"), (12, 2, None, 1, "menu"), (15, 2, None, 1, "toolbar")]
    bookmarks += [(20 + i, 1, 10 + i, (12, 15)[i % 2], "b%d" % i) for i in range(20)]
    return {"moz_origins": [(i, "h%d" % i) for i in range(1, 61)],
            "moz_places": [(i, "https://e%d/" % i, 1 + i % 60) for i in range(1, 61)],
            "moz_historyvisits": [(i, 0, 1 + i % 40, 1600000000 + i) for i in range(1, 101)],
            "moz_annos": [(i, 5 + i, "x") for i in range(1, 11)],
            "moz_bookmarks": bookmarks}


class PrefixTest(Base):
    def test_moz_prefix_and_fk_and_parent(self):
        m = self.mapped("places.sqlite", FIREFOX, firefox_rows())
        self.assertEqual(m.name_target("place_id"), ("moz_places", True))
        r = link(m, "moz_historyvisits", "place_id")
        self.assertEqual((r.other, r.other_column, r.kind, r.base), ("moz_places", "id", "name",
                                                                     0.8))
        self.assertIn("without the prefix moz_", r.reasons[0])
        self.assertTrue(is_confident(r))
        self.assertTrue(is_confident(link(m, "moz_annos", "place_id")))
        # fk: moz_places and moz_origins both hold its values; moz_places is referred to more
        fk = link(m, "moz_bookmarks", "fk")
        self.assertEqual((fk.other, fk.other_column), ("moz_places", "id"))
        self.assertTrue(is_confident(fk))
        self.assertIn("fk holds keys of moz_places", fk.reasons[0])
        # parent: a row of the same table
        parent = link(m, "moz_bookmarks", "parent")
        self.assertEqual((parent.other, parent.other_column), ("moz_bookmarks", "id"))
        self.assertTrue(is_confident(parent))
        self.assertIn("self-reference", parent.reasons[0])
        self.assertIn("(same table)", plain_reason(parent))
        back = [r for r in m.for_column("moz_bookmarks", "id")
                if r.other == "moz_bookmarks" and r.other_column == "parent"]
        self.assertEqual([r.direction for r in back], ["in"])
        # a self-reference is drawn as a loop beside its table's box
        graph = LinkGraph([Link(parent)])
        graph.layout()
        pts = graph.points(graph.links[0])
        self.assertEqual(len(pts), 4)
        self.assertGreater(pts[1][0], pts[0][0])
        self.assertIn("<polyline", graph.svg())

    def test_prefix_shared_by_most_tables(self):
        self.assertIn("zen_", table_prefixes(["zen_pins", "zen_workspaces", "zen_themes"]))
        self.assertIn("zz", table_prefixes(["ZZNOTE", "ZZFOLDER", "ZZACCOUNT"]))
        self.assertNotIn("message_", table_prefixes(["message", "message_media",
                                                     "message_vote"]))
        self.assertNotIn("zen_", table_prefixes(["zen_pins", "notes", "folders"]))
        m = self.mapped("zen.db", """
            CREATE TABLE zen_workspaces(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE zen_pins(id INTEGER PRIMARY KEY, workspace_id INTEGER);
            CREATE TABLE ZZFOLDER(id INTEGER PRIMARY KEY);
            CREATE TABLE ZZNOTE(id INTEGER PRIMARY KEY, folder_id INTEGER);
            CREATE TABLE zen_themes(id INTEGER PRIMARY KEY);
        """, {"zen_workspaces": [(i, "w") for i in range(1, 20)],
              "zen_pins": [(i, 1 + i % 19) for i in range(1, 40)]})
        r = link(m, "zen_pins", "workspace_id")
        self.assertEqual((r.other, r.other_column), ("zen_workspaces", "id"))
        self.assertTrue(is_confident(r))
        # 'zz' is not shared by most of these tables: folder_id names no table
        self.assertIsNone(link(m, "ZZNOTE", "folder_id"))

    def test_own_identifier_is_no_self_reference(self):
        m = self.mapped("nodes.db", """
            CREATE TABLE node(id INTEGER PRIMARY KEY, node_id TEXT, parent_node_id INTEGER);
        """, {"node": [(i, "n%d" % i, i - 1 if i > 1 else None) for i in range(1, 30)]})
        self.assertIsNone(link(m, "node", "node_id"))
        r = link(m, "node", "parent_node_id")
        self.assertEqual((r.other, r.other_column, r.base), ("node", "id", 0.65))
        self.assertTrue(is_confident(r))

    def test_parent_names_a_table_when_there_is_one(self):
        m = self.mapped("parents.db", """
            CREATE TABLE parents(id INTEGER PRIMARY KEY);
            CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER);
        """, {"parents": [(i,) for i in range(1, 20)],
              "child": [(i, 1 + i % 19) for i in range(1, 40)]})
        self.assertEqual(link(m, "child", "parent_id").other, "parents")


CORE_DATA = """
    CREATE TABLE Z_PRIMARYKEY(Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER,
                              Z_MAX INTEGER);
    CREATE TABLE ZFOLDER(Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZNAME VARCHAR);
    CREATE TABLE ZNOTE(Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZFOLDER INTEGER,
                       ZROOTFOLDER INTEGER, ZNUMBEROFFOLDER INTEGER, ZTITLE VARCHAR);
    CREATE INDEX ZNOTE_ZROOTFOLDER_INDEX ON ZNOTE(ZROOTFOLDER);
    CREATE TABLE ZTAG(Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZNAME VARCHAR);
    CREATE TABLE Z_2TAGS(Z_2NOTES INTEGER, Z_4TAGS INTEGER, PRIMARY KEY (Z_2NOTES, Z_4TAGS));
"""


def core_data_rows():
    return {"Z_PRIMARYKEY": [(1, "Folder", 0, 40), (2, "Note", 0, 80), (3, "SmartFolder", 1, 5),
                             (4, "Tag", 0, 30), (5, "Other", 0, 0)],
            "ZFOLDER": [(i, 1 if i % 3 else 3, 1, "f%d" % i) for i in range(10, 41)],
            "ZNOTE": [(i, 2, 1, 10 + i % 31, 10 + i % 5, 10 + i % 7, "n%d" % i)
                      for i in range(1, 81)],
            "ZTAG": [(i, 4, 1, "t%d" % i) for i in range(1, 31)],
            "Z_2TAGS": [(1 + i % 80, 1 + i % 30) for i in range(100)]}


class CoreDataTest(Base):
    def test_core_data_names(self):
        m = self.mapped("NoteStore.sqlite", CORE_DATA, core_data_rows())
        r = link(m, "ZNOTE", "ZFOLDER")
        self.assertEqual((r.other, r.other_column, r.kind, r.base), ("ZFOLDER", "Z_PK", "name",
                                                                     0.8))
        self.assertIn("Core Data", r.reasons[0])
        self.assertTrue(is_confident(r))
        root = link(m, "ZNOTE", "ZROOTFOLDER")      # the last word, an indexed column
        self.assertEqual((root.other, root.base), ("ZFOLDER", 0.65))
        # a count whose name happens to end in a table's name is not indexed: no link
        self.assertIsNone(link(m, "ZNOTE", "ZNUMBEROFFOLDER"))
        ent = link(m, "ZFOLDER", "Z_ENT")
        self.assertEqual((ent.other, ent.other_column), ("Z_PRIMARYKEY", "Z_ENT"))
        self.assertIsNotNone(link(m, "ZNOTE", "Z_ENT"))
        self.assertIsNone(link(m, "Z_PRIMARYKEY", "Z_ENT"))
        # a many-to-many join table
        notes, tags = link(m, "Z_2TAGS", "Z_2NOTES"), link(m, "Z_2TAGS", "Z_4TAGS")
        self.assertEqual((notes.other, notes.other_column), ("ZNOTE", "Z_PK"))
        self.assertEqual((tags.other, tags.other_column), ("ZTAG", "Z_PK"))
        self.assertTrue(is_confident(notes) and is_confident(tags))
        # the Core Data columns Z_PK / Z_ENT / Z_OPT are not paired as same-named columns
        self.assertFalse([r for r in m.links() if r.kind == "same_name"])

    def test_z_names_outside_core_data_are_plain_names(self):
        m = self.mapped("plain.db", """
            CREATE TABLE ZFOLDER(Z_PK INTEGER PRIMARY KEY);
            CREATE TABLE ZNOTE(Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER);
        """, {})
        self.assertIsNone(link(m, "ZNOTE", "Z_ENT"))


class ThresholdTest(Base):
    SCRIPT = """
        CREATE TABLE account(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE login(id INTEGER PRIMARY KEY, account_id INTEGER);
        CREATE TABLE session(id INTEGER PRIMARY KEY, account_id INTEGER);
        CREATE TABLE a(id INTEGER PRIMARY KEY, device_uuid TEXT);
        CREATE TABLE b(id INTEGER PRIMARY KEY, device_uuid TEXT);
    """

    def rows(self, login_values, session_values, uuids):
        return {"account": [(i, "a%d" % i) for i in range(11, 60)],
                "login": [(i, v) for i, v in enumerate(login_values, 1)],
                "session": [(i, v) for i, v in enumerate(session_values, 1)],
                "a": [(i, u) for i, u in enumerate(uuids, 1)],
                "b": [(i, u) for i, u in enumerate(uuids, 1)]}

    def test_name_link_needs_three_values(self):
        m = self.mapped("few.db", self.SCRIPT, self.rows([11, 12] * 10, [11, 12, 13] * 5,
                                                         ["dev-%04d" % i for i in range(5)]))
        two = link(m, "login", "account_id")
        self.assertEqual(two.links[0].overlap.found, 2)
        self.assertFalse(is_confident(two))
        self.assertIn("weaker: too few values (2 distinct values match; 3 needed)",
                      two.why()[-1])
        self.assertIn("weaker: too few values", plain_reason(two))
        three = link(m, "session", "account_id")
        self.assertTrue(is_confident(three))
        self.assertNotIn("weaker", plain_reason(three))
        # same-named columns need 10 values: 5 are not enough
        same = link(m, "a", "device_uuid")
        self.assertEqual((same.kind, same.links[0].overlap.found), ("same_name", 5))
        self.assertFalse(is_confident(same))
        self.assertIn("(5 distinct values match; 10 needed)", same.why()[-1])

    def test_enough_values_and_the_limits(self):
        m = self.mapped("many.db", self.SCRIPT, self.rows([11, 12, 13], [11, 12, 13, 14],
                                                          ["dev-%04d" % i for i in range(12)]))
        self.assertTrue(is_confident(link(m, "login", "account_id")))
        self.assertTrue(is_confident(link(m, "a", "device_uuid")))
        limits.load({"limits": {"relations_min_name_values": 4,
                                "relations_min_same_name_values": 13}})
        self.assertFalse(is_confident(link(m, "login", "account_id")))
        self.assertTrue(is_confident(link(m, "session", "account_id")))
        self.assertFalse(is_confident(link(m, "a", "device_uuid")))
        # no setting can let a link rest on two values
        problems = limits.load({"limits": {"relations_min_name_values": 2,
                                           "relations_min_same_name_values": 1}})
        self.assertEqual(len(problems), 2)
        self.assertEqual(limits.get("relations_min_name_values"), 3)


class CrossIntegerTest(Base):
    def dbs(self):
        contacts = build(os.path.join(self.tmp, "contacts.db"), """
            CREATE TABLE contact(id INTEGER PRIMARY KEY, name TEXT, is_favorite INTEGER,
                                 status_id INTEGER);
            CREATE TABLE note(id INTEGER PRIMARY KEY, body TEXT);
        """, {"contact": [(i, "c%d" % i, i % 2, i % 4) for i in range(100, 141)],
              "note": [(i, "n") for i in range(1, 41)]})
        calls = build(os.path.join(self.tmp, "calls.db"), """
            CREATE TABLE call(id INTEGER PRIMARY KEY, contact_id INTEGER, is_video INTEGER,
                              status_id INTEGER, duration INTEGER);
            CREATE TABLE event(id INTEGER PRIMARY KEY, what TEXT);
        """, {"call": [(i, 100 + i % 41, i % 2, i % 4, i * 7) for i in range(1, 81)],
              "event": [(i, "e") for i in range(1, 41)]})
        return [("contacts", self.open(contacts)), ("calls", self.open(calls))]

    def test_integer_id_link_and_no_flag_links(self):
        res = find_links(self.dbs())
        got = [(l.src_db, l.src_table, l.src_col, l.dst_db, l.dst_table, l.dst_col, l.confident)
               for l in res.links]
        self.assertEqual(got, [("calls", "call", "contact_id", "contacts", "contact", "id",
                                True)])
        self.assertIn("integer ids: contact_id names table contact", res.links[0].reason())
        self.assertEqual(res.int_profiles["contacts"], 2)      # contact.id, note.id
        # flags (0/1), status codes (0..3) and row ids of unrelated tables are never linked
        for l in res.links:
            self.assertNotIn(l.src_col, ("is_favorite", "is_video", "status_id", "duration"))
            self.assertNotEqual((l.src_table, l.dst_table), ("event", "note"))

    def test_reference_inside_its_own_database_is_not_linked_across(self):
        other = build(os.path.join(self.tmp, "other.db"), """
            CREATE TABLE contact(id INTEGER PRIMARY KEY);
            CREATE TABLE message(id INTEGER PRIMARY KEY, contact_id INTEGER);
        """, {"contact": [(i,) for i in range(100, 141)],
              "message": [(i, 100 + i % 41) for i in range(1, 81)]})
        dbs = self.dbs()[:1] + [("other", self.open(other))]
        res = find_links(dbs)
        self.assertEqual(res.links, [])
