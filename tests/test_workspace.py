"""The case workspace with many databases (a phone extraction of 16, 40 and 60 databases):
the one-line header, the Case navigator (grouped, sortable, filter chips, search over every
database, table and column), the Overview, the one scope control, the Timeline's grouped date
columns and density chart, the command palette. Checked by widget introspection inside the
running app (off the screen), and the parts without Tk directly."""

import os
import time
import unittest
from datetime import datetime, timedelta

from tests.helpers import TempDirTest, within
from tests.test_review_ui import AppCase


class NameIndexTest(TempDirTest):
    def test_forty_databases_any_table_found_fast(self):
        from case import Case
        from navigator import NameIndex, case_name, group_key
        from tests.fixtures import workspace_fixtures as wf
        paths = wf.build(self.tmp, 40)
        case = Case()
        self.addCleanup(lambda: [case.remove(m) for m in list(case)])
        for p in paths:
            case.add(p)
        index = NameIndex(list(case))
        tables = [(m, t) for m in case for t in m.db.tables()]
        self.assertGreater(len(tables), 300)
        worst = 0.0
        for m, t in tables[::7]:
            t0 = time.perf_counter()
            found = index.find(t)
            worst = max(worst, time.perf_counter() - t0)
            self.assertTrue(any(e.kind == "table" and e.table == t and e.member is m
                                for e in found), t)
        self.assertLess(worst, 0.1)                     # every table found in under 100 ms
        cols = index.find("counterparty")
        self.assertEqual([(e.kind, e.member.name, e.table) for e in cols],
                         [("column", "transactions_db", "txn")])
        self.assertEqual(index.find("nothing-like-this"), [])
        # groups: the app package of each database
        self.assertEqual(group_key(paths[0]), "com.whatsapp")
        self.assertEqual(group_key(paths[12]), "com.android.chrome")     # .../Default/History
        self.assertEqual(case_name(paths[3:9]), "com.phonepe.app")
        self.assertTrue(case_name(paths))


class DensityTest(unittest.TestCase):
    def test_bins(self):
        from density import bin_times, nice_bins
        t0 = datetime(2023, 1, 1)
        times = [t0 + timedelta(hours=i) for i in range(48)]
        edges, totals, per, step, name = bin_times(times, ["a" if i % 2 else "b"
                                                           for i in range(48)], most=10)
        self.assertEqual(sum(totals), 48)
        self.assertEqual(sum(per["a"]) + sum(per["b"]), 48)
        self.assertLessEqual(len(totals), 10)
        self.assertEqual(name, "6 hours")
        self.assertEqual(nice_bins(t0, t0 + timedelta(days=400), 20)[1], "30 days")
        self.assertEqual(bin_times([]), ([], [], {}, None, ""))


class PaletteRankTest(unittest.TestCase):
    def test_rank(self):
        from palette import Item, rank, score
        self.assertEqual(score("msg", "msgstore.db"), 1)
        self.assertEqual(score("msgdb", "msgstore.db"), 5)
        self.assertIsNone(score("zzz", "msgstore.db"))
        items = [Item("table", "message", "msgstore.db", ("t", 1), lambda: None),
                 Item("database", "msgstore.db", "/x", ("d", 1), lambda: None),
                 Item("action", "Build timeline", "", ("a", 1), lambda: None)]
        self.assertEqual([i.label for i, _s in rank("mess", items)], ["message"])
        self.assertEqual([i.label for i, _s in rank("msgstore", items)][0], "msgstore.db")
        # nothing typed: the recent ones first, then the actions and tabs
        self.assertEqual([i.label for i, _s in rank("", items, [("t", 1)])],
                         ["message", "Build timeline"])


class WorkspaceAppTest(AppCase):
    def open_case(self, n):
        from tests.fixtures import workspace_fixtures as wf
        paths = wf.build(self.tmp, n)
        t0 = time.perf_counter()
        self.app._open_paths(paths, wait=True)
        return paths, time.perf_counter() - t0

    def test_sixteen_databases(self):
        def scenario():
            app = self.app
            paths, seconds = self.open_case(16)
            within(self, seconds, 5.0, "opening the case")
            app._nb.select(app._overview)
            self.pump(lambda: False, 0.5)
            # one header line: the case name and summary, the evidence chips
            from widgets import scaling_factor
            self.assertLessEqual(app._header_frame.winfo_height(), 52 * scaling_factor(app))
            self.assertIn("16 databases", app._db_info.full_text())
            ev, warn = app.status_chips()
            self.assertIn("16 read-only", ev)
            import sqlite3
            if hasattr(sqlite3.Connection, "deserialize"):     # 3.11+: the WAL merges in RAM
                self.assertIn("WAL merged", ev)
                self.assertIn("1 warning", warn)        # kv_store: a hot journal
                self.assertIn("kv_store.db", app.status_detail_text().split("\n")[0])
            else:                                       # 3.10: the WALs are warned about too
                self.assertIn("warnings", warn)
                self.assertIn("kv_store.db", app.status_detail_text())
            # plain tab names, Overview first
            titles = [app._nb.tab(t, "text") for t in app._nb.tabs()]
            self.assertEqual(titles[0], "Overview")
            self.assertTrue(all("·" not in t for t in titles))
            # the navigator: grouped by app, a line per database
            nav = app._navigator
            groups = nav.group_lines()
            self.assertIn("com.whatsapp  (3)", groups)
            self.assertIn("com.phonepe.app  (6)", groups)
            self.assertEqual(len(nav.db_lines()), 16)
            wal = [n for n, st, _t, _r in nav.db_lines() if st.startswith("WAL")]
            self.assertEqual(sorted(wal), ["mmssms.db", "msgstore.db", "transactions_db"])
            # search: a column finds its table in its database, under 50 ms
            nav.search.set("counterparty")
            self.assertLess(nav.last_filter_ms, 50)
            self.assertEqual([n for n, _s, _t, _r in nav.db_lines()], ["transactions_db"])
            self.assertIn("1 column", nav.search.count_text())
            nav.search.set("")
            # the chips: WAL only
            nav.set_chip("wal", True)
            self.assertEqual(len(nav.db_lines()), 3)
            nav.set_chip("wal", False)
            # click a table: Browse shows it in its database (made active)
            m = [x for x in app.case if x.name == "calllog.db"][0]
            app.browse_member_table(m, "calls")
            self.pump(lambda: app._browse_grid.row_count() and not app._browse_grid.loading(),
                      30)
            self.assertIs(app.case.active, m)
            self.assertEqual(app._browse_crumb.text(), "calllog.db › calls")
            # the Overview: a line per database, dates filled in the background
            ov = app._overview
            self.assertEqual(len(ov.rows()), 16)
            self.assertTrue(self.pump(lambda: not ov.busy(), 120))
            dated = [r for r in ov.rows() if r[7] not in ("…", "none found")]
            self.assertGreaterEqual(len(dated), 10, ov.rows())
            self.assertIn("com.whatsapp", [r[1] for r in dated])
            ov.search.set("phonepe")
            self.assertEqual(len(ov.rows()), 6)
            ov.search.set("")
            ov.sort_by("rows")
            self.assertEqual(ov.rows()[0][0], "analytics.db")
            # a search: 'Has hits' appears, the status is one line with details
            app._search_var.set("zebracorn")
            app._do_search()
            self.assertTrue(self.pump(lambda: not app._search_thread.is_alive(), 60))
            self.pump(lambda: False, 0.3)
            st = app._search_status
            self.assertTrue(st.summary().startswith("Complete"), st.summary())
            self.assertIn("2 databases with matches", st.summary())
            self.assertTrue(any("14 databases: searched, nothing found" in d
                                for d in st.details()), st.details())
            self.assertTrue(nav.chips.shown(nav._chip["hits"]))
            nav.set_chip("hits", True)
            self.assertEqual(sorted(n for n, _s, _t, _r in nav.db_lines()),
                             ["msgstore.db", "transactions_db"])
            nav.set_chip("hits", False)
            # the command palette finds any table
            pal = app.open_palette()
            pal = app._palette
            pal.var.set("keyword_search")
            self.assertEqual(pal.shown()[0].label, "keyword_search_terms")
            pal.close()
        self.run_app(scenario)

    def test_forty_databases_timeline(self):
        def scenario():
            app = self.app
            self.open_case(40)
            tab = app._timeline
            app._nb.select(tab)
            self.assertTrue(self.pump(lambda: tab.detection is not None and not tab.busy(),
                                      180))
            # the date columns grouped by database; the heading counts them
            groups = [tab.col_tree.item(g, "values")[2] for u, g in tab._group_iids.items()
                      if u is not None]
            self.assertGreater(len(groups), 10)
            self.assertIn("date columns in", tab.cols_head.full_text())
            tab.start_build()
            self.assertTrue(self.pump(lambda: not tab.busy() and tab.result is not None, 180))
            self.pump(lambda: False, 0.3)
            # one status line, the databases without events in one detail line
            self.assertNotIn("\n", tab.status.summary())
            self.assertIn("databases", tab.status.summary())
            # the density chart: bars, and a drag selection filters the grid
            chart = tab.chart
            self.assertGreater(chart.bar_count(), 5)
            ev = tab.result.events
            mid = ev[len(ev) // 2].when
            chart.select(mid - timedelta(days=2), mid + timedelta(days=2))
            self.pump(lambda: tab.grid.row_count() is not None, 30)
            n = tab.grid.row_count()
            self.assertTrue(0 < n < len(ev), n)
            self.assertIn("in the selected range", tab.sources.full_text())
            chart.clear_selection()
            # the scope: the timeline alone covers 2 databases
            picker = tab.scope_picker
            uids = [m.uid for m in app.case][:2]
            app.scopes.set("timeline", uids, own=True)
            self.assertEqual(picker.button.cget("text"), "2 of 40 databases ▾")
            self.assertTrue(picker.follow_lbl.winfo_manager())
            self.assertIn("Build timeline reads them", tab.status.summary())
            self.assertEqual(app._search_db_picker.button.cget("text"), "All 40 databases ▾")
        self.run_app(scenario)

    def test_sixty_databases_open_and_filter(self):
        def scenario():
            app = self.app
            _paths, seconds = self.open_case(60)
            self.pump(lambda: False, 0.3)
            within(self, seconds, 6.0, "opening 60 databases")
            nav = app._navigator
            from widgets import scaling_factor
            self.assertLessEqual(app._header_frame.winfo_height(), 52 * scaling_factor(app))
            t0 = time.perf_counter()
            nav.search.set("wal_edits")
            took = time.perf_counter() - t0
            self.assertLess(took, 0.2)
            self.assertEqual(len(nav.db_lines()), 14)   # every third of the 44 synthetic ones
            nav.search.set("")
            # many groups: collapsed except the active one's; never a flat wall
            open_groups = [iid for iid, n in nav._nodes.items()
                           if n[0] == "group" and nav.tree.item(iid, "open")]
            self.assertLessEqual(len(open_groups), 2)
            # sort by size puts the biggest first within groups; pin one to the top
            m = [x for x in app.case if x.name == "analytics.db"][0]
            nav.toggle_pin(m)
            self.assertEqual(nav.group_lines()[0], "Pinned  (1)")
        self.run_app(scenario)


@unittest.skipUnless(os.environ.get("SGA_STRESS") == "1", "stress test: set SGA_STRESS=1")
class WorkspaceStressTest(AppCase):
    """60 databases: the Overview, the Timeline and a search running while databases are
    removed one after the other, scopes and filters changed rapidly, the navigator searched
    as fast as keys come; no error, no thread left behind after the close."""

    def test_storm(self):
        import threading
        from tests.fixtures import workspace_fixtures as wf
        paths = wf.build(self.tmp, 60)
        before = set(threading.enumerate())

        def scenario():
            app = self.app
            app._open_paths(paths, wait=True)
            app._nb.select(app._overview)
            app._search_var.set("label")
            app._do_search()
            app._nb.select(app._timeline)
            self.pump(lambda: False, 0.5)
            nav = app._navigator
            for i, text in enumerate(["a", "ap", "app", "app1", "items", "", "wal", "zz", ""]):
                nav.search.set(text)
                nav.set_chip("rows", i % 2 == 0)
                app.scopes.set_global([m.uid for m in list(app.case)[i:i + 5]])
                app.update()
            for m in list(app.case)[:10]:
                app.remove_member(m)
                app.update()
            app.scopes.set_global(None)
            app._timeline.build_when_ready()
            self.pump(lambda: False, 1.0)
            for _ in range(20):
                app._stop_search()
                app._do_search()
                app.update()
            self.pump(lambda: not app._search_thread.is_alive(), 120)
            self.assertEqual(len(app.case), 50)
        self.run_app(scenario)
        deadline = time.time() + 30
        while time.time() < deadline:
            left = [t for t in threading.enumerate() if t not in before and t.is_alive()
                    and not t.daemon]
            if not left:
                break
            time.sleep(0.2)
        self.assertEqual([t.name for t in threading.enumerate() if t not in before and
                          t.is_alive() and not t.daemon], [])


if __name__ == "__main__":
    unittest.main()
