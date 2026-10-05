"""The one search field (widgets.SearchBox): placeholder, live filtering after a pause, ×,
Find / Enter / Shift+Enter, Escape, the 'N of M' count and the empty state, Ctrl+F."""

import time
import tkinter as tk
import unittest
from tkinter import ttk

from tests.helpers import tk_root
from widgets import SearchBox, focus_search_in, search_boxes_in


class SearchBoxTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        self.changes, self.nexts = [], []
        self.box = SearchBox(self.root, placeholder="Find a table…", delay=30,
                             on_change=self.changes.append, on_next=self.nexts.append)
        self.box.pack()
        self.root.update()

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.005)

    def test_placeholder_clear_button_and_live_filter(self):
        b = self.box
        self.assertTrue(b.placeholder_visible())
        self.assertEqual(b.placeholder.full_text(), "Find a table…")
        self.assertEqual(b.get(), "")
        self.assertFalse(b.clear_btn.winfo_manager())
        b.entry.insert(0, "mes")
        b.entry.insert("end", "sage")
        self.assertFalse(b.placeholder_visible())
        self.assertTrue(b.clear_btn.winfo_manager())
        self.assertEqual(self.changes, [])          # not yet: waits for a pause
        self.pump(0.15)
        self.assertEqual(self.changes, ["message"])  # once, after the pause
        b.clear_btn.invoke()
        self.assertEqual(self.changes, ["message", ""])
        self.assertTrue(b.placeholder_visible())
        self.assertFalse(b.clear_btn.winfo_manager())

    def test_find_enter_and_escape(self):
        b = self.box
        b.find_btn.invoke()
        self.assertEqual(self.nexts, [])            # nothing to find
        b.set("jid")
        self.assertEqual(self.changes, ["jid"])
        b.find_btn.invoke()
        for seq in ("<Key-Return>", "<Shift-Key-Return>", "<Key-Escape>", "<Key-F3>"):
            self.assertIn(seq, b.entry.bind())
        b.next(True)
        b.next(False)
        self.assertEqual(self.nexts, [True, True, False])
        b.clear()
        self.assertEqual(b.get(), "")
        self.assertEqual(self.changes, ["jid", ""])
        self.assertTrue(b.find_btn.cget("text"))

    def test_count_and_empty_state(self):
        b = self.box
        self.assertEqual(b.set_count(3, 88, "table", "tables"), "")
        b.set("mess")
        self.assertEqual(b.set_count(3, 88, "table", "tables"), "3 of 88 tables match")
        self.assertEqual(b.count_text(), "3 of 88 tables match")
        self.assertEqual(b.set_count(0, 88, "table", "tables", "table or column"),
                         "No table or column matches “mess”")
        from constants import C
        self.assertEqual(str(b.count_label.cget("foreground")), C["red"])

    def test_ctrl_f_focuses_the_search_field_of_the_window(self):
        other = ttk.Frame(self.root)
        other.pack()
        second = SearchBox(other, primary=True)
        second.pack()
        self.root.update()
        self.assertEqual(len(search_boxes_in(self.root)), 2)
        self.assertIs(focus_search_in(self.root), second)       # the primary one
        self.assertIs(focus_search_in(self.box), self.box)
        other.pack_forget()                                     # out of view
        self.assertIs(focus_search_in(self.root), self.box)
        nb = ttk.Notebook(self.root)
        nb.pack()
        tab1, tab2 = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(tab1, text="one")
        nb.add(tab2, text="two")
        in_tab2 = SearchBox(tab2)
        in_tab2.pack()
        self.assertIsNone(focus_search_in(nb))                  # its tab is not selected
        nb.select(tab2)
        self.assertIs(focus_search_in(nb), in_tab2)
        empty = tk.Toplevel(self.root)
        self.assertIsNone(focus_search_in(empty))
        empty.destroy()

    def test_tree_filter_sets_lines_aside_and_brings_them_back(self):
        from widgets import TreeFilter
        tree = ttk.Treeview(self.root, columns=("a", "b"), show="headings")
        for i in range(10):
            tree.insert("", "end", values=("row %d" % i, "even" if i % 2 == 0 else "odd"))
        box = SearchBox(self.root, delay=0)
        f = TreeFilter(tree, box, "issue", "issues")
        box.set("even row")
        self.assertEqual(len(tree.get_children()), 5)
        self.assertEqual(box.count_text(), "5 of 10 issues match")
        box.set("nothing")
        self.assertEqual(tree.get_children(), ())
        self.assertEqual(box.count_text(), "No issue matches “nothing”")
        box.set("")
        self.assertEqual([tree.item(i, "values")[0] for i in tree.get_children()],
                         ["row %d" % i for i in range(10)])
        # refilled: the new lines are filtered by themselves, the old ones are gone
        box.set("odd")
        tree.delete(*tree.get_children())
        tree.insert("", "end", values=("new", "odd"))
        tree.insert("", "end", values=("other", "even"))
        self.root.update()
        self.assertEqual([tree.item(i, "values")[0] for i in tree.get_children()], ["new"])
        self.assertEqual(box.count_text(), "1 of 2 issues match")
        box.set("")
        self.assertEqual(len(tree.get_children()), 2)
        box.set("e")
        box.next(True)
        self.assertEqual(len(f.tree.selection()), 1)

    def test_text_find_marks_and_walks_the_matches(self):
        from widgets import TextFind
        text = tk.Text(self.root)
        text.insert("1.0", "alpha beta\nbeta gamma\nBETA")
        box = SearchBox(self.root, delay=0)
        f = TextFind(text, box)
        box.set("beta")
        self.assertEqual(len(f.hits), 3)
        self.assertEqual(box.count_text(), "1 of 3")
        box.next(True)
        self.assertEqual(box.count_text(), "2 of 3")
        box.next(False)
        box.next(False)
        self.assertEqual(box.count_text(), "3 of 3")
        box.set("zeta")
        self.assertEqual(box.count_text(), "Not found: “zeta”")

    def test_narrow_room_shrinks_the_entry_not_the_buttons(self):
        b = self.box
        b.set("x")
        b.pack_forget()
        self.root.update()
        room = b.find_btn.winfo_reqwidth() + b.clear_btn.winfo_reqwidth() + 20
        b.place(x=0, y=0, width=room)
        self.root.update()
        self.assertLess(b.entry.winfo_width(), b.entry.winfo_reqwidth())
        for btn in (b.find_btn, b.clear_btn):
            self.assertGreaterEqual(btn.winfo_width(), btn.winfo_reqwidth())


if __name__ == "__main__":
    unittest.main()
