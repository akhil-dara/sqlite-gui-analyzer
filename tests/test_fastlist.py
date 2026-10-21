"""fastlist.FastTree: the Treeview interface the lists use (model, columns, tags, selection,
open / close, geometry, events), and its point: a wheel step costs a fraction of a Treeview's
with the same rows."""
import time
import tkinter as tk
from tkinter import ttk
import unittest

import tests.helpers  # noqa: F401 - the src path
from tests.helpers import tk_root
from tests.helpers import within  # noqa: E402


COLS = ("#", "Kind", "Table", "Column", "RowID", "Matched Value", "Type")


def fill(tree, n=200, children=True):
    for i in range(n):
        iid = tree.insert("", "end", iid="r%d" % i,
                          values=(i, "msgstore.db · Live row", "message_ftsv2_content",
                                  "c0content", 6000 + i, "good evening, see you at the station " * 4, "contains"),
                          tags=("odd" if i % 2 else "even",))
        if children:
            tree.insert(iid, "end", iid="r%d.c" % i, values=("", "", "", "c1", "", "detail", ""),
                        tags=("sr_child",))


class FastTreeTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        self.root.geometry("1200x700+-4000+0")     # shown off the screen: drawn, not seen
        self.root.deiconify()
        from fastlist import FastTree
        self.tree = FastTree(self.root, columns=COLS, show="tree headings")
        for c in COLS:
            self.tree.heading(c, text=c)
        self.tree.pack(fill="both", expand=True)

    def pump(self, t=0.2):
        end = time.time() + t
        while time.time() < end:
            self.root.update()
            time.sleep(0.01)

    def test_model_like_a_treeview(self):
        t = self.tree
        fill(t, 5)
        self.assertEqual(t.get_children(), ("r0", "r1", "r2", "r3", "r4"))
        self.assertEqual(t.get_children("r0"), ("r0.c",))
        self.assertEqual(t.parent("r0.c"), "r0")
        self.assertEqual(t.item("r2", "values")[4], 6002)
        self.assertEqual(t.item("r2")["tags"], ("even",))
        t.item("r2", values=("x",) * 7, tags=("sr_recovered",))
        self.assertEqual(t.item("r2", "tags"), ("sr_recovered",))
        self.assertEqual(t.set("r2", "Kind"), "x")
        t.set("r2", "Kind", "y")
        self.assertEqual(t.item("r2", "values")[1], "y")
        auto = t.insert("", 0, values=("a",))
        self.assertEqual(t.get_children()[0], auto)
        t.delete("r0")
        self.assertFalse(t.exists("r0"))
        self.assertFalse(t.exists("r0.c"))
        t.delete(*t.get_children())
        self.assertEqual(t.get_children(), ())
        with self.assertRaises(tk.TclError):
            t.item("nope", "values")

    def test_columns_tags_and_drawing(self):
        t = self.tree
        t.column("#0", width=24, stretch=False)
        t.column("RowID", width=70, stretch=False)
        self.assertEqual(t.column("RowID", "width"), 70)
        self.assertEqual(t.cget("columns"), COLS)
        t.tag_configure("odd", background="#f1f5f9")
        fill(t, 30)
        self.pump()
        text = t._text.get("1.0", "end")
        self.assertIn("message_ftsv2_content", text)
        self.assertIn("…", text)                    # the long preview is cut
        self.assertEqual(len(t._lines), 30)               # children closed
        t.item("r1", open=True)
        self.pump()
        self.assertEqual(len(t._lines), 31)
        self.assertEqual(t._lines[2], "r1.c")
        self.assertIn("t_odd", t._text.tag_names("2.0"))  # row r1 carries its tag

    def test_selection_events_and_geometry(self):
        t = self.tree
        fill(t, 50)
        self.pump()
        got = []
        t.bind("<<TreeviewSelect>>", lambda e: got.append(t.selection()))
        t.selection_set("r3")
        self.pump(0.1)
        self.assertEqual(t.selection(), ("r3",))
        self.assertTrue(got)
        box = t.bbox("r3")
        self.assertTrue(box and box[3] > 0)
        self.assertEqual(t.identify_row(box[1] + 2), "r3")
        self.assertEqual(t.identify_column(5), "#0")
        self.assertEqual(t.identify_column(t.column("#0", "width") + 5), "#1")
        t.focus("r3")
        t._move(1)
        self.assertEqual(t.selection(), ("r4",))
        t._move(1, extend=True)
        self.assertEqual(t.selection(), ("r4", "r5"))
        t.see("r49.c")                                   # opens its parent, scrolls to it
        self.pump()
        self.assertTrue(t.item("r49", "open"))
        self.assertTrue(t.bbox("r49.c"))
        t.delete("r4")
        self.assertNotIn("r4", t.selection())

    def test_wheel_step_is_fast(self):
        t = self.tree
        fill(t, 200)
        self.pump(0.3)

        def cost(w, scroll):
            ts = []
            for k in range(20):
                a = time.perf_counter()
                scroll(3 if k % 10 < 5 else -3)
                w.update_idletasks()
                ts.append(time.perf_counter() - a)
            ts.sort()
            return ts[10]
        fast = cost(t, lambda n: t.yview_scroll(n, "units"))
        tv = ttk.Treeview(self.root, columns=COLS, show="tree headings")
        t.pack_forget()
        tv.pack(fill="both", expand=True)
        fill(tv, 200)
        self.pump(0.3)
        slow = cost(tv, lambda n: tv.yview_scroll(n, "units"))
        # half of the Treeview's time: what the list is for (a warning on a CI runner, whose
        # drawing costs differ from a desktop's)
        within(self, fast, slow * 0.5, "a wheel step of the list (Treeview %.1f ms)" % (
            slow * 1000))


if __name__ == "__main__":
    unittest.main()
