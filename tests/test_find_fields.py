"""Every scrollable list has a Find field (Ctrl+F comes to it): the row detail of a table row
(hundreds of columns), of a query result, of a WAL record and of a recovered record, the WAL
per-table statistics and the BLOB Inspector's timestamp readings."""

from types import SimpleNamespace

from tests.test_review_ui import AppCase


def listed(tree):
    return [tree.set(i, tree["columns"][0]) for i in tree.get_children()]


class FindFieldsTest(AppCase):
    def check_tree_find(self, win, tree, find, word, want, total):
        from widgets import focus_search_in
        self.assertIs(focus_search_in(win), find)          # Ctrl+F in that window
        find.set(word)
        shown = listed(tree)
        self.assertEqual(shown, want)
        self.assertIn("%d of %d" % (len(want), total), find.count_text())
        find.set("")
        self.assertEqual(len(tree.get_children()), total)

    def test_row_detail_of_a_wide_row(self):
        from tests.fixtures import make_fixtures as fx
        path = fx.wide(self.tmp)

        def scenario():
            from dialogs import RowWin
            from engine.schema import Locator
            from widgets import focus_search_in
            app = self.app
            app._open_db(path, wait=True)
            table = app.db.tables()[0]
            w = RowWin.show(app, app.db, table, Locator("rowid", 1))
            self.assertTrue(self.pump(lambda: getattr(w, "complete", False), 30))
            every = w.shown_columns()
            self.assertGreater(len(every), 200)
            self.assertIs(focus_search_in(w), w.find)
            name = every[123]
            w.find.set(name)
            shown = w.shown_columns()
            self.assertIn(name, shown)
            self.assertLess(len(shown), len(every))
            data = w._row_data
            self.assertTrue(all(name.lower() in (c + " " + str(data.get(c))).lower()
                                for c in shown))
            self.assertIn("of %d columns" % len(every), w.find.count_text())
            self.assertEqual(w._find_next(True), shown[0])
            self.assertIn("1 of %d" % len(shown), w.find.count_text())
            # a value: the columns holding it
            val = str(w._row_data.get(every[5]))
            w.find.set(val)
            self.assertIn(every[5], w.shown_columns())
            w.find.set("no-such-thing-anywhere")
            self.assertEqual(w.shown_columns(), [])
            self.assertIn("No column matches", w.find.count_text())
            w.find.set("")
            self.assertEqual(w.shown_columns(), every)
            w._on_close()
        self.run_app(scenario)

    def test_query_result_wal_and_recovered_record_windows(self):
        def scenario():
            from dialogs import ValuesWindow
            from forensics_tab import RecordWindow
            from wal_tab import WalRecordWindow
            app = self.app
            cols = ["id", "sender", "body", "stamp", "blob"]
            vals = [7, "alice@example.net", "see you at noon", 1700000000, b"\x00\x01"]
            w = ValuesWindow(app, "Row", "SQL result", cols, vals)
            self.check_tree_find(w, w.tree, w.find, "noon", ["body"], 5)
            w.destroy()
            w = WalRecordWindow(app, "msgs", 7, "current", 3, 2, None, cols, vals)
            self.check_tree_find(w, w.tree, w.find, "alice", ["sender"], 5)
            w.destroy()
            rec = SimpleNamespace(
                table="msgs", index=None, rowid=None, confidence="high", reasons=[],
                flags=set(), candidates=[], copies=[], columns=cols, values=vals,
                prov=SimpleNamespace(where=lambda: "freeblock of page 3"),
                as_dict=lambda: {})
            w = RecordWindow(app, rec, app)
            self.check_tree_find(w, w.tree, w.find, "stamp", ["stamp"], 5)
            w.destroy()
        self.run_app(scenario)

    def test_wal_per_table_statistics(self):
        from tests.test_review_ui import mixed_wal
        path = mixed_wal(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            wt = app._wal_frame
            wt._fill_stats()
            tree, find = wt.stats_tree, wt.stats_find
            names = listed(tree)
            self.assertGreater(len(names), 1)
            find.set(names[-1])
            self.assertIn(names[-1], listed(tree))
            self.assertLess(len(listed(tree)), len(names))
            self.assertIn("of %d tables" % len(names), find.count_text())
            find.set("")
            self.assertEqual(listed(tree), names)
        self.run_app(scenario)

    def test_blob_inspector_timestamps(self):
        def scenario():
            from inspector import BlobInspector
            app = self.app
            w = BlobInspector(app, b"\x00\x01hello", "c", "test")
            w._show_times(SimpleNamespace(value=1700000000000, kind="int"))
            total = len(w.times.get_children())
            self.assertGreater(total, 1)
            first = w.times.set(w.times.get_children()[0], "kind")
            w.times_find.set(first)
            self.assertIn(first, listed(w.times))
            self.assertIn("of %d" % total, w.times_find.count_text())
            w.times_find.set("")
            self.assertEqual(len(w.times.get_children()), total)
            w.destroy()
        self.run_app(scenario)

    def test_tooltips_say_what_they_leave_out(self):
        from widgets import cut_list
        self.assertEqual(cut_list(["a", "b"], 5), "a\nb")
        text = cut_list([str(i) for i in range(45)], 30)
        self.assertEqual(text.split("\n")[-1], "… and 15 more")
        self.assertEqual(len(text.split("\n")), 31)
