"""The Relationships tab over real databases, without the App (introspection only): the
tables and a table's card, live search with its count and empty state, the diagram (an
entity-relationship diagram: no overlapping cards or text, a relationship's panel with its
cardinality and a sample JOIN, groups, junction tables, linked columns only, moved cards kept,
overview, zoom), empty tables, All links and the exports."""

import os
import sqlite3
import xml.dom.minidom

from tests.helpers import TempDirTest, tk_root  # noqa: F401
from tests.test_relations_view import ViewTestBase
from engine import limits
from engine.linkgraph import EMPTY, overlaps
from relations_tab import ALL, DB as MAIN, RelationsTab


def text_boxes(canvas):
    """(item, bbox) of every text drawn on the canvas (not hidden, not a tooltip)."""
    out = []
    for item in canvas.find_all():
        if canvas.type(item) != "text" or canvas.itemcget(item, "state") == "hidden":
            continue
        if "tip" in canvas.gettags(item):
            continue
        x0, y0, x1, y1 = canvas.bbox(item)
        out.append((item, x0, y0, x1 - x0, y1 - y0))
    return out


def hub_fixture(directory, referring=150):
    """message referred to by `referring` tables alike (att_NNN.message_row_id), by one empty
    table, and referring to chat."""
    path = os.path.join(directory, "hub.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE chat(_id INTEGER PRIMARY KEY, subject TEXT)")
    c.execute("CREATE TABLE message(_id INTEGER PRIMARY KEY, chat_row_id INTEGER, body TEXT)")
    c.executemany("INSERT INTO chat VALUES (?,?)", [(i, "chat %d" % i) for i in range(1, 11)])
    c.executemany("INSERT INTO message VALUES (?,?,?)",
                  [(i, 1 + i % 10, "m %d" % i) for i in range(1, 301)])
    for i in range(referring):
        t = "att_%03d" % i
        c.execute("CREATE TABLE %s(_id INTEGER PRIMARY KEY, message_row_id INTEGER, v TEXT)" % t)
        c.executemany("INSERT INTO %s(message_row_id, v) VALUES (?,?)" % t,
                      [(20 + (3 * i + k) % 270, "x") for k in range(12)])
    c.execute("CREATE TABLE message_draft(_id INTEGER PRIMARY KEY, message_row_id INTEGER)")
    c.commit()
    c.close()
    return path


class TabCase(ViewTestBase):
    def tab(self):
        tab = RelationsTab(self.root, self.root)
        tab.pack(fill="both", expand=True)
        self.root.geometry("1200x800")
        self.mapped()
        self.root.update()
        return tab

    def assert_no_overlaps(self, tab):
        cv = tab.canvas
        self.assertEqual(overlaps(text_boxes(cv)), [])
        boxes = []
        for item in cv.find_withtag("body"):
            x0, y0, x1, y1 = cv.coords(item)
            boxes.append((item, x0, y0, x1 - x0, y1 - y0))
        self.assertTrue(boxes)
        self.assertEqual(overlaps(boxes), [])
        # everything inside the scroll region
        region = [float(v) for v in str(cv.cget("scrollregion")).split()]
        x0, y0, x1, y1 = cv.bbox("all")
        self.assertTrue(region[0] <= x0 and region[1] <= y0 and x1 <= region[2] and
                        y1 <= region[3])


class RelationsTabTest(TabCase):
    def test_tables_card_search_diagram_and_exports(self):
        tab = self.tab()
        self.assertEqual(tab.view(), "tables")
        self.assertIn("12 links (1 declared, 11 verified by values), 2 weaker",
                      tab.status.cget("text"))
        # the tables with links, the most linked selected, its card
        self.assertEqual(tab.focus, (MAIN, "message"))
        self.assertEqual(len(tab.table_tree.get_children()), 12)
        self.assertEqual(tab.card_title.cget("text"), "message")
        self.assertIn("Referred by 5 tables, refers to 1", tab.card_info.cget("text"))
        heads = [tab.card_tree.item(i, "text") for i in tab.card_tree.get_children()]
        self.assertEqual(heads[:2], ["Refers to (1)", "Referred by (5)"])
        # live search: the list of tables, its count, the empty state
        tab.search.set("poll")
        names = [tab.table_tree.item(i, "text") for i in tab.table_tree.get_children()]
        self.assertEqual(names, ["message_poll", "message_poll_option"])
        self.assertEqual(tab.search.count_text(), "2 of 12 tables match")
        tab.search.set("zzz")
        self.assertEqual(tab.table_tree.get_children(), ())
        self.assertEqual(tab.search.count_text(), "No table or column matches “zzz”")
        tab.search.set("")
        self.assertEqual(tab.search.count_text(), "")
        self.assertTrue(tab.search.placeholder_visible())
        # selecting a table selects it everywhere
        for iid, n in tab._table_iids.items():
            if n == (MAIN, "jid"):
                tab.table_tree.selection_set(iid)
        self.root.update()
        self.assertEqual(tab.focus, (MAIN, "jid"))
        self.assertEqual(tab.card_title.cget("text"), "jid")
        # the diagram: always the selected table and its neighbours
        tab.show_view("diagram")
        self.assertEqual(set(tab.shown_tables()), set(["jid", "chat", "message_vote",
                                                       "user_device"]))
        self.assertEqual(len(tab.node_items()), len(tab.shown_tables()))
        self.assert_no_overlaps(tab)
        tab.set_focus((MAIN, "message"))
        self.assertEqual(set(tab.shown_tables()), set(["message", "chat", "message_poll",
                                                       "message_poll_option", "message_vote",
                                                       "note_link", "receipt"]))
        self.assert_no_overlaps(tab)
        # search in the diagram marks the tables and says how many
        tab.search.set("vote")
        self.assertEqual(tab.search.count_text(), "1 of 7 tables match")
        self.assertEqual(tab._selected, ("node", (MAIN, "message_vote")))
        self.assertFalse(tab.side.winfo_manager())          # marked, no panel popping up
        tab.search.set("")
        # the overview: every linked table, one line per link
        tab.toggle_overview()
        self.assertEqual(len(tab.node_items()), 12)
        self.assertEqual(len(tab.canvas.find_withtag("link")), 12)
        self.assertEqual(len(tab.canvas.find_withtag("link")), len(tab.graph.shown()))
        self.assertTrue(tab.hub_btn.winfo_manager())
        self.assertIn("Overview: all 12 linked tables", tab.diagram_note.cget("text"))
        self.assert_no_overlaps(tab)
        tab.toggle_overview()
        # a click selects a table: its side panel, its links
        tab.select(("node", (MAIN, "message")))
        self.assertTrue(tab.side.winfo_manager())
        self.assertEqual(len(tab.side_tree.get_children()), 6)
        tab.select(None)
        self.assertFalse(tab.side.winfo_manager())
        # a click on a connector: the relationship's columns, cardinality and a sample JOIN
        rel = next(r for r in tab.model.rels if r.src == (MAIN, "receipt"))
        tab.erd.click(("rel", rel.rid))
        self.assertEqual(tab._selected, ("rel", rel))
        self.assertTrue(tab.side.winfo_manager())
        self.assertTrue(tab.rel_box.winfo_manager())
        self.assertFalse(tab.side_list.winfo_manager())
        self.assertEqual(tab.side_title.cget("text"), "receipt.msg → message._id")
        self.assertIn("declared foreign key", tab.side_info.cget("text"))
        self.assertIn("one-to-one (from the schema and the values)", tab.rel_card.cget("text"))
        self.assertIn('JOIN "message" AS d ON d."_id" = s."msg";', tab.sql_of_selected())
        shown = sorted(k for k, b in tab.side_btns.items() if tab.side_bar.shown(b))
        self.assertEqual(shown, ["browse_dst", "browse_src", "copy_sql", "related"])
        tab._side_action("browse_dst")
        self.assertEqual(self.browsed[-1], ("message",))
        tab._side_action("browse_src")
        self.assertEqual(self.browsed[-1], ("receipt",))
        # a table's panel again: its links, not the relationship's details
        tab.select(("node", (MAIN, "chat")))
        self.assertTrue(tab.side_list.winfo_manager())
        self.assertFalse(tab.rel_box.winfo_manager())
        tab.select(None)
        # a moved card stays where it was left (this view), until Auto-arrange
        key = (MAIN, "chat")
        x, y = tab.erd.layout.cards[key].x, tab.erd.layout.cards[key].y
        tab.erd.move_card(key, x, y + 600)
        self.assertEqual(tab._positions[tab._view_key()][key], (x, y + 600))
        tab.refresh()
        self.assertEqual((tab.erd.layout.cards[key].x, tab.erd.layout.cards[key].y),
                         (x, y + 600))
        self.assert_no_overlaps(tab)
        tab.auto_arrange()
        self.assertNotIn(tab._view_key(), tab._positions)
        self.assertEqual(tab.erd.layout.cards[key].y, y)
        # linked columns only: fewer rows, every connector still at its column
        tab.erd.set_scale(1.0)
        rows = len(tab.canvas.find_withtag("colname"))
        self.assertEqual(rows, 20)          # every column of the 7 tables
        tab.linked_only_var.set(True)
        tab._linked_only_changed()
        self.assertLess(len(tab.canvas.find_withtag("colname")), rows)
        self.assertNotIn("text_data", [tab.canvas.itemcget(i, "text")
                                       for i in tab.canvas.find_withtag("colname")])
        tab.linked_only_var.set(False)
        tab._linked_only_changed()
        self.assertEqual(len(tab.canvas.find_withtag("colname")), rows)
        # N:M: the junction tables of the whole database as many-to-many relationships
        tab.toggle_overview()
        self.assertIn("3 junction tables (N:M draws them", tab.diagram_note.cget("text"))
        tab.junction_var.set(True)
        tab.draw()
        self.assertTrue(set(["message_vote", "note_link", "user_device"]).isdisjoint(
            tab.shown_tables()))
        self.assertEqual(sum(1 for r in tab.model.rels if r.junction is not None), 3)
        self.assertIn("3 junction tables drawn as many-to-many", tab.diagram_note.cget("text"))
        self.assertTrue(tab.legend_frame.shown(tab._legend_items["nm"]))
        self.assert_no_overlaps(tab)
        tab.junction_var.set(False)
        tab.toggle_overview()
        # All links: the list, weaker on request, search
        tab.show_view("list")
        self.assertEqual(len(tab.list_rows()), 12)
        self.assertEqual(tab.list_rows()[0][:2], ("receipt.msg", "message._id"))
        tab.weaker_var.set(True)
        tab.refresh()
        self.assertEqual(len(tab.list_rows()), 14)
        tab.weaker_var.set(False)
        tab.refresh()
        tab.search.set("device_id")
        self.assertEqual([r[0] for r in tab.list_rows()], ["user_device.device_id"])
        self.assertEqual(tab.search.count_text(), "1 of 12 links match")
        tab.search.set("")
        tab.only_table_var.set(True)
        tab.refresh()
        self.assertEqual(len(tab.list_rows()), 6)
        tab.only_table_var.set(False)
        tab.refresh()
        # exports
        svg, csv_path = os.path.join(self.tmp, "map.svg"), os.path.join(self.tmp, "links.csv")
        self.assertTrue(tab.export_svg(svg) and tab.export_csv(csv_path))
        with open(svg, encoding="utf-8") as f:
            text = f.read()
        xml.dom.minidom.parseString(text)
        self.assertEqual(text.count('<polyline class="link"'), 6)
        with open(csv_path, encoding="utf-8-sig") as f:
            self.assertEqual(len(f.read().strip().splitlines()), 13)
        # closing the database empties the tab
        self.rw.close_all()
        self.assertEqual(tab.list_rows(), [])
        self.assertEqual(tab.table_tree.get_children(), ())

    def test_zoom_hides_text_instead_of_overlapping(self):
        tab = self.tab()
        tab.show_view("diagram")
        tab.zoom_center(0.3)
        self.assertTrue(tab.text_hidden())
        self.assertEqual(text_boxes(tab.canvas), [])
        tab.zoom_center(1 / 0.3)
        self.assertFalse(tab.text_hidden())
        self.assert_no_overlaps(tab)
        tab.fit()
        self.assert_no_overlaps(tab)


class HubTabTest(TabCase):
    fixture = staticmethod(hub_fixture)

    def setUp(self):
        TabCase.setUp(self)
        limits.reset()
        self.addCleanup(limits.reset)

    def test_group_of_alike_tables_and_empty_tables(self):
        tab = self.tab()
        self.assertEqual(tab.focus, (MAIN, "message"))
        # the card groups the 150 alike tables
        tree = tab.card_tree
        heads = dict((tree.item(i, "text"), i) for i in tree.get_children())
        self.assertIn("Referred by (150)", heads)
        groups = [tree.item(i, "text") for i in tree.get_children(heads["Referred by (150)"])]
        self.assertEqual(groups, ["150 tables via message_row_id → _id"])
        # the empty table: hidden, counted, its link only a weaker one
        self.assertEqual(tab.empty_cb.cget("text"), "Show empty tables (1)")
        names = [tab.table_tree.item(i, "text") for i in tab.table_tree.get_children()]
        self.assertNotIn("message_draft", names)
        self.assertIn("Weaker links (1)", heads)
        weak = tree.get_children(heads["Weaker links (1)"])
        self.assertTrue(tree.item(weak[0], "values")[2].startswith(EMPTY))
        tab.empty_var.set(True)
        tab.refresh()
        names = [tab.table_tree.item(i, "text") for i in tab.table_tree.get_children()]
        self.assertNotIn("message_draft", names)      # no trusted link: still not listed
        tab.empty_var.set(False)
        tab.refresh()
        # the diagram: one group card, nothing overlapping
        tab.show_view("diagram")
        gnames = [tab.canvas.itemcget(i, "text") for i in tab.canvas.find_withtag("gname")]
        self.assertEqual(gnames, ["150 tables  ▸"])
        self.assertEqual(set(tab.shown_tables()), set(["message", "chat"]))
        self.assertEqual(len(tab.graph.shown()), 2)         # the group's line, message → chat
        self.assert_no_overlaps(tab)
        # a click lists the group's tables (name and rows) in its card
        gid = next(iter(tab.model.groups))
        tab.toggle_group(gid)
        members = tab.canvas.find_withtag("member")
        self.assertEqual(len(members), 150)
        self.assertEqual(tab.canvas.itemcget(members[0], "text"), "att_000")
        self.assertEqual(len(tab.node_items()), 2)
        self.assert_no_overlaps(tab)
        # its panel: the group's links
        tab.select(("group", gid))
        self.assertEqual(len(tab.side_tree.get_children()), 150)
        self.assertEqual(tab.side_btns["expand"].cget("text"), "Fold")
        tab.select(None)
        tab.toggle_group(gid)
        self.assertEqual(tab.canvas.find_withtag("member"), ())
        # search lists the group's tables and marks the matching ones
        tab.search.set("att_01")
        self.assertIn(gid, tab.erd.expanded_groups())
        self.assertEqual(len(tab.canvas.find_withtag("match")), 10)
        self.assertEqual(tab.search.count_text(), "10 of 152 tables match")
        self.assertEqual(tab._selected, ("group", gid))
        self.assert_no_overlaps(tab)
        tab.search.set("")
        self.assertNotIn(gid, tab.erd.expanded_groups())
        # the group minimum is a limit
        limits.load({"limits": {"diagram_group_min": 1000}})
        tab.refresh()
        self.assertEqual(len(tab.shown_tables()), 152)
        self.assert_no_overlaps(tab)
