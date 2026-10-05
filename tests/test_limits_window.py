"""The Limits window: one list of every limit and one editor for the chosen limit (a field per
limit made hundreds of widgets, slow to open and to close)."""

import time

from tests.test_review_ui import AppCase
from tests.helpers import within


class LimitsWindowTest(AppCase):
    def test_list_editor_find_and_quick_open_close(self):
        from engine import limits as lim
        app = self.app

        def scenario():
            t0 = time.perf_counter()
            w = app.datamap.limits_window(app)
            app.update_idletasks()
            opened = time.perf_counter() - t0
            self.assertEqual(len(w.tree.get_children()), len(lim.LIMITS))
            self.assertLess(len(list(self.walk(w))), 40)       # not a field per limit
            self.assertLess(opened, 1.0)
            # the editor shows the selected limit; its value edits the list row
            name = "sql_history"
            w.choose(name)
            self.assertEqual(w.edit_name.cget("text"), name)
            self.assertEqual(str(w.entry.cget("textvariable")), str(w.vars[name]))
            w.entry.delete(0, "end")
            w.entry.insert(0, "7")
            self.assertEqual(w.vars[name].get(), "7")
            iid = w._rows[name][0]
            self.assertEqual(str(w.tree.set(iid, "value")), "7")
            self.assertIn("changed", w.tree.set(iid, "status"))
            self.assertIn("changed", w.edit_status.cget("text"))
            self.assertIn("changed", w.tree.item(iid, "tags"))
            w.vars[name].set("x")
            self.assertIn("not valid", w.status_text(name))
            self.assertIn("bad", w.tree.item(iid, "tags"))
            self.assertFalse(w.save())
            self.assertIn(name, w.status.cget("text"))
            # Find: only the matching limits stay listed, in name order
            w.find_var.set("grid_")
            shown = [w.tree.item(i, "text") for i in w.tree.get_children()]
            self.assertTrue(shown)
            self.assertEqual(shown, sorted(shown))
            self.assertTrue(all("grid_" in n or "grid_" in lim.LIMITS[n][3].lower()
                                for n in shown))
            self.assertIn("of %d" % len(lim.LIMITS), w.search.count_text())
            w.choose(name)                  # a limit the Find hides: listed again
            self.assertIn(w._rows[name][0], w.tree.get_children())
            w.find_var.set("")
            self.assertEqual(len(w.tree.get_children()), len(lim.LIMITS))
            w.defaults()
            self.assertTrue(w.save())
            t0 = time.perf_counter()
            w.close()
            app.update_idletasks()
            within(self, time.perf_counter() - t0, 0.5, "Limits window")
            self.assertFalse(w.winfo_exists())
            self.assertTrue(all(v._tk is None for v in w.vars.values()))   # let go of
        self.run_app(scenario)

    def walk(self, w):
        yield w
        for c in w.winfo_children():
            for x in self.walk(c):
                yield x
