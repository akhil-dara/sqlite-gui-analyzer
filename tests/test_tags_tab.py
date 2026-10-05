"""The Tagged tab lists any number of tagged rows in the virtual grid: tag colours, column and
text filters, selection, notes, tag removal, opening rows and the export scope."""
import os
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

from tests.helpers import TempDirTest, tk_root
from engine import tags as T
from engine.schema import Locator
from engine.tags import TagStore, entry_from_db_row, tint

N_ROWS = 12000          # well past the old 5,000-line cap


class FakeTags(object):
    """The parts of tagging.Tagging the tab uses."""

    def __init__(self, store):
        self.store, self.tab, self.save_error = store, None, ""
        self.opened, self.menus = [], 0

    def open_entry(self, entry):
        self.opened.append(entry)

    def changed(self):
        self.tab.refresh()

    def edit_note(self, entries, text=None):
        for e in entries:
            self.store.set_note(e.key, text or "a note")
        self.changed()

    def tag_menu(self, menu, get_entries, label="Tag", accelerators=False):
        self.menus += 1
        menu.add_command(label=label)

    def swatch(self, color):
        return ""


class FakeApp(object):
    def __init__(self, root, store):
        self.tags = FakeTags(store)
        self._nb = ttk.Notebook(root)


class TagsTabTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        patcher = mock.patch.dict(os.environ, {T.DATA_DIR_ENV: os.path.join(self.tmp, "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = tk_root(self)
        self.root.geometry("1200x700")
        self.store = TagStore(os.path.join(self.tmp, "case.db"))
        entries = [entry_from_db_row("t" if i % 3 else "u", Locator("rowid", i), ["id", "s"],
                                     [i, "row %05d" % i]) for i in range(1, N_ROWS + 1)]
        self.store.add(entries, "Review")
        self.store.add(entries[:10], "Relevant")        # first tag in definition order
        from tags_tab import TagsTab
        self.app = FakeApp(self.root, self.store)
        self.tab = TagsTab(self.app._nb, self.app)
        self.app._nb.add(self.tab, text="Tagged")
        self.app._nb.pack(fill="both", expand=True)
        self.tab.refresh()
        self.root.deiconify()
        self.root.update()

    def test_every_tagged_row_is_listed(self):
        g = self.tab.grid
        self.assertEqual(g.row_count(), N_ROWS)
        self.assertEqual(len(self.tab.filtered_entries()), N_ROWS)
        self.assertIn("Tagged (%s)" % format(N_ROWS, ","), self.app._nb.tab(self.tab, "text"))
        g.scroll_to_row(N_ROWS - 1)
        g.redraw_now()
        first, end = g.visible_row_range()
        self.assertEqual(end, N_ROWS)
        last = g.row_data(N_ROWS - 1)[0]
        self.assertEqual((last[2], last[3]), ("u" if N_ROWS % 3 == 0 else "t", N_ROWS))
        g.sort_by(3, desc=True)                         # row ids sort as numbers
        g.scroll_to_row(0)
        g.redraw_now()
        self.assertEqual(g.row_data(0)[0][3], N_ROWS)

    def test_tag_colours(self):
        g = self.tab.grid
        g.redraw_now()
        relevant = self.store.color_of("Relevant")
        review = self.store.color_of("Review")
        self.assertEqual(g.row_marker(0), relevant)
        self.assertEqual(g.row_background(0) in (tint(relevant),), True)
        row = next(r for r in range(g.visible_row_range()[1])
                   if self.tab._entry_of(g.row_data(r)[0]).tags == ["Review"])
        self.assertEqual(g.row_marker(row), review)

    def test_filters_selection_and_actions(self):
        tab, g = self.tab, self.tab.grid
        tab.filter_var.set("row 0001")
        tab._apply_filter()
        want = [e for e in self.store.entries()
                if all(w in " ".join(str(v) for v in tab._values(e)).lower()
                       for w in ("row", "0001"))]
        self.assertTrue(10 <= len(want) < 100)
        self.assertEqual(g.row_count(), len(want))
        g.set_filter_text(2, "u")                       # the Table column
        self.root.update()
        kept = tab.filtered_entries()
        self.assertTrue(kept and all(e.table == "u" for e in kept))
        self.assertIn("listed", tab.status.cget("text"))
        g.clear_filters()
        tab.filter_var.set("")
        tab._apply_filter()
        self.assertEqual(g.row_count(), N_ROWS)
        # sort by a column, then select three rows and act on them
        g.sort_by(3, desc=True)                         # Row, descending
        tab.select_rows(0, 2)
        sel = tab.selected_entries()
        self.assertEqual(len(sel), 3)
        tab.edit_note()
        self.assertEqual([self.store.get(e.key).note for e in sel], ["a note"] * 3)
        # kept (keys name the database too: two databases of a case can share a row key)
        self.assertEqual(set(tab.tags_keys_selected()), set(e.case_key for e in sel))
        tab.open_selected()
        self.assertEqual(self.app.tags.opened[-1].key, sel[0].key)
        tab._open_row(0, g.fetch_rows(1, 1)[0][0])
        self.assertEqual(self.app.tags.opened[-1].key, g.fetch_rows(1, 1)[0][0] and
                         tab._entry_of(g.fetch_rows(1, 1)[0][0]).key)
        # the context menu adds the tag actions
        menu = g.build_context_menu(0, 1)
        labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                  if menu.type(i) != "separator"]
        self.assertIn("Edit note…", labels)
        self.assertIn("Remove tag…", labels)
        # removing every tag of the selected rows drops them from the list
        n = tab.remove_tag(None)
        self.assertEqual(n, 3)
        self.assertEqual(g.row_count(), N_ROWS - 3)
        self.assertEqual(len(tab.scope_entries("filtered")[0]), N_ROWS - 3)

    def test_choosing_a_tag(self):
        tab = self.tab
        label = next(l for l, t in tab._labels.items() if t == "Relevant")
        tab.tag_var.set(label)
        tab.refresh()
        self.assertEqual(tab.grid.row_count(), 10)
        self.assertEqual(len(tab.scope_entries("filtered")[0]), 10)


if __name__ == "__main__":
    unittest.main()
