"""Views: searched (on request) with locators that reopen the matched row; browsed, filtered,
exported and shown in Row Detail like tables; listed apart in the search scope dialog."""
import unittest

from tests.helpers import TempDirTest, tk_root
from tests.fixtures import make_fixtures as fx
from engine.backends import Filter

from browse_sources import TableSource
from database import DB, RID


class ViewFacadeTest(TempDirTest):
    def open(self):
        db = DB()
        db.open(fx.views(self.tmp))
        self.addCleanup(db.close)
        return db

    def test_search_covers_views_and_hits_reopen_their_row(self):
        db = self.open()
        self.assertEqual(db.views(), ["vw"])
        got = dict((t, hits) for t, hits, err in db.search_tables(
            ["t", "vw"], "n05", "Case-Insensitive", 500, False, lambda: False))
        self.assertEqual(sorted(got), ["t", "vw"])
        view_hits = got["vw"]
        self.assertEqual(sorted(h["value"] for h in view_hits),
                         sorted(h["value"] for h in got["t"]))
        for h in view_hits:
            loc = h["locator"]
            self.assertEqual(loc.kind, "ordinal")
            data, cols = db.full_row("vw", loc)      # Row Detail of the matched view row
            self.assertEqual(cols, [RID, "id", "name"])
            self.assertEqual(data["name"], h["value"])
            self.assertEqual(h["rowid"], loc)

    def test_views_browse_filter_sort_and_export(self):
        db = self.open()
        src = TableSource(db, "vw")
        self.assertEqual(src.columns(), [RID, "id", "name"])
        src.sort("name", False)
        src.set_filters({"name": "n00"}, "")
        _flt, n = src.count_rows()
        self.assertEqual(n, 9)
        src.set_count(src.flt, n)
        rows = src.rows(0, 3)
        self.assertEqual([r[0][2] for r in rows], ["n001", "n002", "n003"])
        exported = list(src.iter_rows())             # what Browse > Export writes
        self.assertEqual([r[2] for r in exported], ["n00%d" % i for i in range(1, 10)])
        data, _cols = db.full_row("vw", rows[1][0][0])
        self.assertEqual(data["name"], "n002")
        self.assertEqual(db.count("vw"), 100)
        self.assertEqual(db.count_filtered("vw", Filter(col_exprs={"id": "<=10"})), 10)
        self.assertEqual(len(list(db.iter_rows("vw"))), 100)


class ScopeDialogTest(TempDirTest):
    def test_views_are_listed_apart_and_can_be_chosen(self):
        root = tk_root(self)
        from dialogs import ScopeDlg
        dlg = ScopeDlg(root, ["a", "b"], {"a": 3, "b": 0}, ["a"], views=["v1", "v2"])
        try:
            root.update()
            texts = [w.cget("text") for w in dlg._inner.winfo_children()]
            self.assertTrue(any("Views" in t for t in texts), texts)
            self.assertEqual(dlg.visible_views(), ["v1", "v2"])
            # no view named in the selection: every view starts selected
            self.assertTrue(dlg._vars["v1"].get() and dlg._vars["v2"].get())
            dlg._vars["v2"].set(False)
            dlg._sel_nonempty()                      # leaves views alone (count unknown)
            self.assertTrue(dlg._vars["v1"].get())
            dlg._filter_var.set("v2")
            root.update()
            self.assertEqual(dlg.visible_views(), ["v2"])
            dlg._sel_all()
            dlg._apply()
            self.assertEqual(sorted(dlg.result), ["a", "v1", "v2"])
        finally:
            if dlg.winfo_exists():
                dlg.destroy()

    def test_a_scope_naming_some_views_keeps_the_others_off(self):
        root = tk_root(self)
        from dialogs import ScopeDlg
        dlg = ScopeDlg(root, ["a"], {"a": 1}, ["a", "v2"], views=["v1", "v2"])
        try:
            root.update()
            self.assertFalse(dlg._vars["v1"].get())
            self.assertTrue(dlg._vars["v2"].get())
        finally:
            dlg.destroy()


if __name__ == "__main__":
    unittest.main()
