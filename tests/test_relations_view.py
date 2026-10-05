"""Relationship menus and windows over a real database, without the App (introspection only;
the Relationships tab: test_relations_tab)."""

import time
import tkinter as tk
import unittest

from tests.helpers import TempDirTest, tk_root
from tests.fixtures import make_fixtures as fx
from database import DB, RID
from engine.relations import ROWID
from engine.schema import Locator
from relations_view import ColumnMapWindow, FindWindow, RelatedWindow, RelationWindows


def labels(menu):
    return [menu.entrycget(i, "label") for i in range((menu.index("end") or 0) + 1)
            if menu.type(i) != "separator"] if menu.index("end") is not None else []


def cascade(menu, label):
    for i in range((menu.index("end") or 0) + 1):
        if menu.type(i) == "cascade" and menu.entrycget(i, "label") == label:
            return menu.nametowidget(menu.entrycget(i, "menu"))
    return None


class ViewTestBase(TempDirTest):
    fixture = staticmethod(fx.relations)

    def setUp(self):
        TempDirTest.setUp(self)
        self.root = root = tk_root(self)
        self.db = db = DB()
        db.open(self.fixture(self.tmp))
        self.tagged = []
        self.browsed = []
        root.db = db
        root._release_worker_connection = lambda: db.session and \
            db.session.release_thread_connection()

        def tag_menu(menu, get_entries, label="Tag"):
            sub = tk.Menu(menu, tearoff=0)
            sub.add_command(label="Relevant", command=lambda: self.tagged.extend(get_entries()))
            menu.add_cascade(label=label, menu=sub)
            return sub
        root.tag_menu = tag_menu
        root.browse_related = lambda *a: self.browsed.append(a)
        root.browse_table = lambda t: self.browsed.append((t,))
        self.rw = root.relations = RelationWindows(root)
        self.addCleanup(self.close)

    def close(self):
        self.rw.stop()
        deadline = time.time() + 5
        while self.rw.worker_threads() and time.time() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.rw.close_all()
        self.db.close()

    def pump(self, busy, timeout=30):
        deadline = time.time() + timeout
        while busy() and time.time() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(busy())

    def mapped(self):
        self.rw.start_mapping()
        self.pump(self.rw.mapper.busy)
        self.assertEqual(self.rw.state[0], "done")

    def menu_for(self, table, column, value, locator=None):
        m = tk.Menu(self.root, tearoff=0)
        self.rw.value_menu(m, table, column, value, locator)
        return m


class MenuTest(ViewTestBase):
    def test_key_column(self):
        self.assertEqual(self.rw.key_column("message", RID, Locator("rowid", 7)), ("_id", 7))
        self.assertEqual(self.rw.key_column("note", RID, Locator("rowid", 3)), (ROWID, 3))
        self.assertEqual(self.rw.key_column("device", RID, Locator("pk", ("dev-01",))),
                         ("device_id", "dev-01"))
        self.assertFalse(self.rw.supported("sqlite_master"))

    def test_related_rows_only_where_rows_exist(self):
        # before the map is known the menu offers no Related rows (and stays instant)
        self.assertNotIn("Related rows", labels(self.menu_for("message_poll",
                                                              "message_row_id", 10)))
        self.mapped()
        m = self.menu_for("message_poll", "message_row_id", 10)
        self.assertEqual(labels(m)[:1], ["Related rows"])
        self.assertEqual(labels(cascade(m, "Related rows")), [
            "message (_id) — 1 row", "message_poll_option (message_row_id) — 3 rows",
            "message_vote (message_row_id) — 2 rows",
            "note_link (message_row_id) — 1 row", "receipt (msg) — 1 row",
            "All related rows…"])
        self.assertIn("Find this value everywhere", labels(m))

    def test_no_item_when_the_value_has_no_related_rows(self):
        self.mapped()
        m = self.menu_for("message_poll", "message_row_id", 999999)
        self.assertEqual(labels(m), ["Find this value everywhere"])
        # NULL, 0 and empty values: no Related rows; NULL and empty: nothing at all
        self.assertEqual(labels(self.menu_for("message", "_id", 0)),
                         ["Find this value everywhere"])
        self.assertEqual(labels(self.menu_for("message", "text_data", None)), [])
        self.assertEqual(labels(self.menu_for("message", "text_data", "")), [])

    def test_no_item_for_a_column_without_confident_relations(self):
        self.mapped()
        self.assertNotIn("Related rows", labels(self.menu_for("message", "status_id", 1)))
        self.assertNotIn("Related rows", labels(self.menu_for("settings", "message_id", 900001)))
        self.assertEqual(labels(self.menu_for("message", "text_data", "message 5")),
                         ["Find this value everywhere", "Find inside other values"])

    def test_weak_matches_never_in_the_menu(self):
        self.mapped()
        # settings.message_id names message but its values are nowhere: it is left out even
        # for a value it holds
        m = self.menu_for("settings", "message_id", 900001)
        self.assertIsNone(cascade(m, "Related rows"))
        rows = labels(cascade(self.menu_for("message", "_id", 10), "Related rows"))
        self.assertFalse([l for l in rows if l.startswith("settings")])

    def test_the_row_locator_stands_for_the_key(self):
        self.mapped()
        m = self.menu_for("message", RID, Locator("rowid", 25), Locator("rowid", 25))
        self.assertIn("message_poll (message_row_id) — 1 row",
                      labels(cascade(m, "Related rows")))


class WindowTest(ViewTestBase):
    def test_related_window(self):
        self.mapped()
        w = self.rw.related("message_poll", "message_row_id", 10,
                            select=("message_poll_option", "message_row_id"))
        self.assertIsInstance(w, RelatedWindow)
        self.pump(w.busy)
        lines = [w.tree.item(i, "values") for i in w.tree.get_children()]
        self.assertEqual(set(v[1] for v in lines), set(["message", "message_poll_option",
                                                         "message_vote", "note_link",
                                                         "receipt"]))
        self.assertEqual(lines[0][1], "message")                     # strongest first
        self.assertTrue(all(int(v[3]) > 0 for v in lines))            # no empty lines
        self.assertIn("same values found in 100% of samples", lines[0][5])
        self.assertEqual(w._current.table, "message_poll_option")    # the chosen target
        self.assertEqual(w.grid.row_count(), 3)
        cascade(w.tag_all_menu(), "Tag all rows found").invoke(0)
        self.assertEqual(len(self.tagged), sum(len(g.rows) for g in w.groups))
        w._browse_selected()
        self.assertEqual(self.browsed[-1], ("message_poll_option", "message_row_id", 10))

    def test_column_map_window(self):
        w = self.rw.column_map("message_poll", "message_row_id")
        self.assertIsInstance(w, ColumnMapWindow)
        self.assertIs(self.rw.column_map("message_poll", "message_row_id"), w)
        self.pump(w.busy)
        top = [w.tree.item(i, "values")[0] for i in w.tree.get_children()]
        self.assertEqual(top[0], "message")
        self.assertEqual([str(s) for s in w.tree.tk.splitlist(w.tree.cget("show"))],
                         ["headings"])                              # no blank first column
        row = w.tree.item(w.tree.get_children()[0], "values")
        self.assertTrue(row[4].startswith(("Strong", "Likely")), row)
        # weaker: their own section, folded, named with the count
        self.assertEqual(w.weak_btn.cget("text"), "▸ Weaker matches (1)")
        self.assertFalse(w.weaker_shown())
        self.assertIn("1 weaker match (below, folded)", w.status.cget("text"))
        w.toggle_weaker()
        self.assertTrue(w.weaker_shown())
        weak = [w.weak_tree.item(i, "values")[0] for i in w.weak_tree.get_children()]
        self.assertEqual(weak, ["settings"])
        # the selected line explained in full, not repeating the list's Why
        w.tree.selection_set(w.tree.get_children()[0])
        w._show_why()
        text = w.why.get("1.0", "end")
        for part in ("message_poll.message_row_id → message._id", "Strength:",
                     "Declared FOREIGN KEY: no", "Found by:", "distinct values sampled"):
            self.assertIn(part, text)
        # search filters both lists
        w.search.set("zzz")
        self.assertEqual(w.tree.get_children(), ())
        self.assertIn("No table or column matches", w.search.count_text())
        w.search.set("")
        # labelled buttons of full height; Stop is shown only while the map is being made
        self.root.update()
        for key, b in w.buttons.items():
            self.assertTrue(b.cget("text"), key)
            if key == "stop":
                self.assertFalse(b.winfo_ismapped(), "Stop shown with nothing running")
                continue
            self.assertGreaterEqual(b.winfo_height(), b.winfo_reqheight(), key)
        w.close()

    def test_column_map_without_weaker_matches_says_nothing_of_them(self):
        self.mapped()
        w = self.rw.column_map("chat", "jid_row_id")
        self.pump(w.busy)
        self.assertNotIn("weaker", w.status.cget("text"))
        self.assertFalse(w.weak_btn.winfo_manager())
        w.close()

    def test_stop_and_close(self):
        w = self.rw.related("message", "_id", 5)
        w.stop()
        self.pump(w.busy)
        w.close()
        self.assertNotIn(w, self.rw.windows)


class FindWindowTest(ViewTestBase):
    fixture = staticmethod(fx.values_everywhere)

    def test_find_everywhere_window(self):
        w = self.rw.find(5, origin=("a", "n", Locator("rowid", 1)))
        self.assertIsInstance(w, FindWindow)
        self.pump(w.busy)
        got = sorted((g.table, g.column, g.count) for g in w.groups)
        self.assertEqual(got, [("a", "b", 1), ("a", "t", 1)])
        self.assertIn("Common value", w.warning.cget("text"))
        self.assertTrue(all(g.link == "same value found" for g in w.groups))
        self.assertIn("coincidence possible", w.groups[0].why)

    def test_find_inside_other_values(self):
        w = self.rw.find(b"token-123", True, origin=("a", "b", Locator("rowid", 3)))
        self.pump(w.busy)
        got = sorted((g.table, g.column, g.count) for g in w.groups)
        self.assertEqual(got, [("a", "b", 1), ("c", "code", 1), ("c", "data", 2)])
        self.assertEqual(w.warning.cget("text"), "")
        data = [g for g in w.groups if g.column == "data"][0]
        self.assertTrue(data.why.startswith("inside a larger value"))

    def test_nothing_for_empty_values(self):
        self.assertIsNone(self.rw.find(""))
        self.assertIsNone(self.rw.find(None))


if __name__ == "__main__":
    unittest.main()
