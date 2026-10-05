"""The fixes of the verification review, checked in the running app (inside the Tk main loop)
or on the engine directly."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from tests.helpers import TempDirTest
from tests.test_review_ui import AppCase, mixed_wal


def dropped_in_wal(directory):
    """A database whose WAL still holds a table dropped later (so it is WAL-only): its rows
    have a NULL, a REAL with many digits, a BLOB, and a BLOB stored in a TEXT column."""
    path = os.path.join(directory, "dropped.db")
    work = os.path.join(directory, "_work_dropped")
    os.makedirs(work)
    wpath = os.path.join(work, "dropped.db")
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE keep(id INTEGER PRIMARY KEY, note TEXT)")
    c.execute("INSERT INTO keep(note) VALUES ('text'), (?)", (b"\x00\x01bytes",))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("CREATE TABLE gone(id INTEGER PRIMARY KEY, a TEXT, r REAL, b BLOB)")
    c.execute("INSERT INTO gone(a, r, b) VALUES (NULL, 3.14159265358979, x'00ff10')")
    c.execute("INSERT INTO gone(a, r, b) VALUES ('NULL', 2.5, NULL)")
    c.execute("DROP TABLE gone")
    for sfx in ("", "-wal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.close()
    return path


class WalOnlyBrowseTest(AppCase):
    def test_wal_only_rows_keep_their_stored_values_and_load_on_a_worker(self):
        path = dropped_in_wal(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            self.assertIn("gone", app.db.wal_tables())
            app._browse_table_var.set("WAL: gone")
            app._load_browse_table()
            # nothing read on the Tk thread: the grid is empty and the status says why
            self.assertEqual(app._browse_source.row_count(), 0)
            self.assertIn("reading the WAL records of gone",
                          app._browse_status.cget("text"))
            self.assertTrue(self.pump(lambda: app._browse_wal_loading is None))
            src = app._browse_source
            cols = src.columns()
            rows = [list(r) for r in src.iter_all()]
            a, r, b = cols.index("a"), cols.index("r"), cols.index("b")
            values = set((row[a], row[r], row[b]) for row in rows)
            self.assertIn((None, 3.14159265358979, b"\x00\xff\x10"), values)
            self.assertIn(("NULL", 2.5, None), values)          # the text 'NULL' is not NULL
            self.assertTrue(app._browse_blob_export)  # BLOBs to export
            self.assertNotIn("reading", app._browse_status.cget("text"))
            # a BLOB stored in a TEXT column brings Export BLOBs… up (checked on a worker)
            app._browse_table_var.set("keep")
            app._load_browse_table()
            self.assertTrue(self.pump(lambda: app._browse_blob_export))
        self.run_app(scenario)


class ForensicReportTest(AppCase):
    def test_report_names_the_tool_and_gets_a_manifest(self):
        from tests.fixtures.forensic_fixtures import deleted_rows
        path = deleted_rows(self.tmp)
        out = tempfile.mkdtemp(prefix="sga_verify_out_")
        self.addCleanup(shutil.rmtree, out, True)
        target = os.path.join(out, "report.json")
        import jobs
        import forensics_tab
        saved = (jobs.export_options, forensics_tab.filedialog.asksaveasfilename)
        jobs.export_options = lambda *a, **k: {"scope": "", "fmt": "json",
                                               "blob_mode": "hex", "skip_hidden": False}
        forensics_tab.filedialog.asksaveasfilename = lambda *a, **k: target

        def restore():
            jobs.export_options, forensics_tab.filedialog.asksaveasfilename = saved
        self.addCleanup(restore)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._forensics._write_report({}, "forensic_report", audit=True)
            self.assertTrue(self.pump(lambda: os.path.exists(target + ".manifest.json")
                                      and self.shown))
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["tool"]["name"], "SQLite GUI Analyzer")
            self.assertIn("sqlite", data["tool"])
            with open(target + ".manifest.json", encoding="utf-8") as f:
                man = json.load(f)
            self.assertEqual(man["files"][0]["size"], os.path.getsize(target))
            self.assertEqual(len(man["files"][0]["sha256"]), 64)
            files = man["provenance"]["databases"][0]["files"]
            self.assertEqual(len([f for f in files if f["role"] == "main"][0]["sha256"]), 64)
            self.assertIn("Manifest (provenance", self.shown[-1][1])
            kinds = [e["kind"] for e in app._activity_log.entries()]
            self.assertIn("export", kinds)
        self.run_app(scenario)


class TimelineExportTest(AppCase):
    def test_timeline_export_uses_the_one_writer(self):
        from tests.fixtures import timeline_fixtures as tlfx
        path = tlfx.build(self.tmp)
        out = tempfile.mkdtemp(prefix="sga_verify_out_")
        self.addCleanup(shutil.rmtree, out, True)
        import timeline_tab
        targets = []
        saved = timeline_tab.filedialog.asksaveasfilename
        timeline_tab.filedialog.asksaveasfilename = lambda *a, **k: targets[-1]
        self.addCleanup(setattr, timeline_tab.filedialog, "asksaveasfilename", saved)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            tab = app._timeline
            app._nb.select(tab)
            self.assertTrue(self.pump(lambda: tab.detection is not None and not tab.busy(), 120))
            tab.start_build()
            self.assertTrue(self.pump(lambda: not tab.busy() and tab.result is not None, 120))
            n = len(tab.shown_events())
            self.assertGreater(n, 0)
            for fmt in ("csv", "json", "html"):
                targets.append(os.path.join(out, "timeline." + fmt))
                before = len(self.shown)
                tab.export_dialog(fmt)
                self.assertTrue(self.pump(lambda: len(self.shown) > before and not app._jobs))
                title, text = self.shown[-1][:2]
                self.assertIn("%s events written" % format(n, ","), text)
                self.assertIn("Manifest", text)
                with open(targets[-1] + ".manifest.json", encoding="utf-8") as f:
                    man = json.load(f)
                prov = man["provenance"]
                self.assertTrue(prov["complete"])
                self.assertEqual(prov["rows"], n)
                self.assertIn("sqlite", prov)
                main = [f for f in prov["databases"][0]["files"] if f["role"] == "main"][0]
                self.assertEqual(len(main["sha256"]), 64)
                self.assertIn("range_utc", prov["extra"])
            with open(targets[1], encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["rows"]), n)
            self.assertTrue(data["end"]["complete"])
            with open(targets[2], encoding="utf-8") as f:
                page = f.read()
            self.assertIn("SHA-256 " + main["sha256"], page)
            self.assertIn("%s rows" % format(n, ","), page)
            kinds = [e["kind"] for e in app._activity_log.entries()]
            self.assertGreaterEqual(kinds.count("export"), 3)
        self.run_app(scenario)


class NestedCellTest(unittest.TestCase):
    def test_lists_and_dicts_are_written_with_the_value_encoding(self):
        from engine.export import csv_cell, json_cell
        vals = [None, b"\x00\xff", 1.5, "NULL"]
        self.assertEqual(json_cell(vals), [None, {"blob_hex": "00ff", "size": 2}, 1.5, "NULL"])
        self.assertEqual(json.loads(csv_cell(vals)), json_cell(vals))
        self.assertEqual(json.loads(csv_cell({"a": b"\x01"}, "base64")),
                         {"a": {"blob_base64": "AQ==", "size": 1}})
        self.assertNotIn("b'", csv_cell([b"x"]))

    def test_stopped_counts_have_separators(self):
        from engine import export as ex
        out = tempfile.mkdtemp(prefix="sga_verify_out_")
        self.addCleanup(shutil.rmtree, out, True)
        seen = []               # progress is reported at 1,200 rows: stop there
        res = ex.write_rows(os.path.join(out, "x.html"), "html", ["a"],
                            ([i] for i in range(5000)), ex.provenance("t", [], "src"),
                            cancel=lambda: seen and True, progress=lambda n: seen.append(n),
                            every=1200)
        self.assertFalse(res.complete)
        self.assertEqual(res.stopped, "by the user after 1,200 rows")
        with open(res.path, encoding="utf-8") as f:
            self.assertIn("INCOMPLETE: stopped by the user after 1,200 rows", f.read())


class NamedCapsTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        from engine import limits
        self.addCleanup(limits.reset)

    def test_former_constants_are_limits(self):
        from engine import limits
        for name in ("live_hash_rows", "live_index_entries", "journal_records",
                     "audit_ptrmap_pages", "relations_sample_scan_rows",
                     "relations_target_scan_rows", "relations_native_index_rows",
                     "relations_cheap_scan_rows", "relations_key_sets",
                     "relations_key_set_values"):
            self.assertIn(name, limits.DEFAULTS)
            self.assertIn(name, limits.DESCRIPTIONS)
            self.assertTrue(limits.valid(name, limits.DEFAULTS[name]))
        from engine.issues import IssueLog
        limits.load({"limits": {"issues_kept": 100}})
        log = IssueLog()
        for i in range(150):
            log.add("k", str(i))
        self.assertEqual((len(log), log.dropped), (100, 50))

    def test_a_table_too_large_to_hash_is_named_in_the_carve_result(self):
        from engine import limits
        from engine.session import Session
        from tests.fixtures import forensic_fixtures as ff
        from forensics_tab import live_check_text
        limits.load({"limits": {"live_hash_rows": 10}})
        s = Session.open(ff.without_rowid_deleted(self.tmp), hash_evidence=False)
        self.addCleanup(s.close)
        res = s.forensics.carve(index_entries=False)
        self.assertEqual(res.stats.get("live_check_skipped"), ["items"])
        text = live_check_text(res.stats)
        self.assertIn("Rows of items were not compared", text)
        self.assertIn("limit live_hash_rows", text)
        self.assertEqual(live_check_text({}), "")


class RemoveMemberTest(AppCase):
    def test_removing_one_database_leaves_the_rest_of_the_case_alone(self):
        from tests.fixtures import case_fixtures as cf
        folders = [os.path.join(self.tmp, n) for n in ("phone_a", "phone_b")]
        for f in folders:
            os.makedirs(f)
        paths = [cf.messages(folders[0]), cf.contacts(folders[1]), cf.settings(folders[1])]

        def scenario():
            app = self.app
            app._open_db(paths[0], wait=True)
            app._add_paths(paths[1:], wait=True)
            # one header line: the case summary names the active database
            self.assertIn("3 databases", app._db_info.full_text())
            self.assertIn("active: messages.db", app._db_info.full_text())
            # a finished search
            app._search_var.set("example")
            app._limit_var.set("All")
            app._do_search()
            self.assertTrue(self.pump(lambda: not (app._search_thread and
                                                   app._search_thread.is_alive())))
            self.pump(lambda: False, 0.3)
            self.assertTrue(app._search_status.cget("text").startswith("Complete"))
            # a built timeline
            tab = app._timeline
            app._nb.select(tab)
            self.assertTrue(self.pump(lambda: tab.detection is not None and not tab.busy(), 120))
            tab.start_build()
            self.assertTrue(self.pump(lambda: not tab.busy() and tab.result is not None, 120))
            names = set(e.database for e in tab.result.events)
            self.assertIn("contacts.db", names)
            self.assertIn("messages.db", names)
            n_messages = sum(1 for e in tab.result.events if e.database == "messages.db")
            gone = [m for m in app.case if m.name == "contacts.db"][0]
            app.remove_member(gone)
            self.pump(lambda: False, 0.3)
            text = app._search_status.cget("text")
            self.assertTrue(text.startswith("Complete"), text)
            self.assertIn("results of contacts.db were removed with it", text)
            self.assertFalse(any(r.get("dbid") == gone.uid for r in app._search_results))
            self.assertIn("2 databases", app._db_info.full_text())
            # the timeline kept the other databases' events
            self.assertIsNotNone(tab.result)
            self.assertEqual(set(e.database for e in tab.result.events), {"messages.db"})
            self.assertEqual(len(tab.result.events), n_messages)
            self.assertIn("contacts.db left the case", tab.status.cget("text"))
            self.assertEqual(gone.path, paths[1])       # a closed member still has its path
        self.run_app(scenario)


class RecentAsksTest(AppCase):
    def test_recent_asks_before_replacing_a_case(self):
        from tests.fixtures import case_fixtures as cf
        paths = cf.build(self.tmp)
        import tkinter.messagebox as mb
        asked = []

        def scenario():
            app = self.app
            app._open_db(paths[0], wait=True)
            app._add_paths(list(paths[1:]), wait=True)
            mb.askyesno = lambda *a, **k: asked.append(a) or False     # the user says No
            menu = app.tags.recent_menu()
            labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                      if menu.type(i) == "command"]
            self.assertTrue(any("messages.db" in l for l in labels))
            app.open_recent(paths[0])
            app.wait_opened()
            self.assertEqual(len(asked), 1)
            self.assertIn("Replace the case of 3 databases", asked[0][1])
            self.assertEqual(len(app.case), 3)                  # No keeps the case
            mb.askyesno = lambda *a, **k: True
            app.open_recent(paths[0])
            app.wait_opened()
            self.assertEqual(len(app.case), 1)
            asked.clear()
            mb.askyesno = lambda *a, **k: asked.append(a) or True
            app.open_recent(paths[1])                           # one database: no question
            app.wait_opened()
            self.assertEqual(asked, [])
        self.run_app(scenario)


class CloseVerificationTest(AppCase):
    def test_the_rehash_on_close_runs_off_the_tk_thread_and_can_be_skipped(self):
        import time as _time
        path = mixed_wal(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            member = app.case.active
            ev = member.db.evidence
            self.assertTrue(self.pump(lambda: ev.hashing_done))
            real = ev.verify_on_close
            seen = {}

            def slow(limit, cancel=None, progress=None):
                deadline = _time.time() + 20
                while not (cancel is not None and cancel()) and _time.time() < deadline:
                    _time.sleep(0.02)       # a large file being re-hashed
                seen["skipped"] = cancel is not None and cancel()
                return real(limit, cancel, progress)
            ev.verify_on_close = slow
            shown = {}

            def press_skip():
                win = app._verify_close_win
                shown["window"] = win is not None and win.winfo_exists()
                if shown["window"]:
                    btn = [w for w in win.winfo_children()
                           if w.winfo_class() == "TButton"][0]
                    shown["button"] = btn.cget("text")
                    btn.invoke()
            app.after(700, press_skip)
            t0 = _time.time()
            reports = app._verify_before_close([member])
            self.assertLess(_time.time() - t0, 10)
            self.assertTrue(shown.get("window"))
            self.assertEqual(shown.get("button"), "Skip SHA-256 (size and time only)")
            self.assertTrue(seen["skipped"])
            report = reports[member.uid]
            self.assertTrue(report.unchanged)
            self.assertIn("skipped", report.checked_text())
            ev.verify_on_close = real
        self.run_app(scenario)


class ProvenanceExportsTest(AppCase):
    def test_copy_with_related_map_and_relationships_exports_have_manifests(self):
        from tests.fixtures import make_fixtures as fx
        path = fx.relations(self.tmp)
        out = tempfile.mkdtemp(prefix="sga_verify_out_")
        self.addCleanup(shutil.rmtree, out, True)
        import relations_tab
        from engine.schema import Locator
        saved = relations_tab.filedialog.asksaveasfilename
        target = {}
        relations_tab.filedialog.asksaveasfilename = lambda *a, **k: target["path"]
        self.addCleanup(setattr, relations_tab.filedialog, "asksaveasfilename", saved)

        def manifest_of(p):
            with open(p + ".manifest.json", encoding="utf-8") as f:
                man = json.load(f)
            self.assertEqual(man["files"][0]["size"], os.path.getsize(p))
            main = [f for f in man["provenance"]["databases"][0]["files"]
                    if f["role"] == "main"][0]
            self.assertEqual(len(main["sha256"]), 64)
            return man

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            rw = app.relations
            self.assertTrue(self.pump(lambda: rw.state[0] == "done", 120))
            dmu = app.datamap
            table = next(t for t in app.db.tables() if dmu.has_links(t))
            w = dmu.copy_with_related(table, [Locator("rowid", 1)])
            self.assertIsNotNone(w)
            self.assertTrue(self.pump(lambda: not w.busy(), 60))
            md = os.path.join(out, "copy.md")
            self.assertTrue(w.export_to(md))
            self.assertTrue(self.pump(lambda: not w.busy() and os.path.exists(
                md + ".manifest.json"), 60))
            self.pump(lambda: False, 0.3)
            man = manifest_of(md)
            self.assertIn("Copy with related", man["provenance"]["source"])
            with open(md, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("SQLite GUI Analyzer", text)
            self.assertIn("SHA-256", text)
            w.close()
            # the Database Map
            mw = dmu.export_map()
            html = os.path.join(out, "map.html")
            self.assertTrue(mw.export_to(html))
            self.assertTrue(self.pump(lambda: not mw.busy() and os.path.exists(
                html + ".manifest.json"), 120))
            manifest_of(html)
            mw.close()
            # Relationships: the links listed (CSV) and the diagram (SVG)
            tab = app._relations_tab
            for name, fn in (("links.csv", tab.export_csv_dialog),
                             ("map.svg", tab.export_svg_dialog)):
                target["path"] = os.path.join(out, name)
                fn()
                self.assertTrue(self.pump(lambda: not app._jobs and os.path.exists(
                    target["path"] + ".manifest.json"), 60))
                manifest_of(target["path"])
            whats = [e.get("what", "") for e in app._activity_log.entries()
                     if e["kind"] == "export"]
            for want in ("Copy with related", "Database Map", "Relationships: links listed",
                         "Relationships diagram"):
                self.assertTrue(any(want in str(x) for x in whats), (want, whats))
        self.run_app(scenario)


class WalRecordsFiltersTest(AppCase):
    def test_filters_apply_to_compared_records_and_say_when_to_compare_again(self):
        path = mixed_wal(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            wt = app._wal_frame
            wt.view_var.set("records")
            wt._switch_view()
            wt.load_records()
            self.assertTrue(self.pump(lambda: not wt._runner.busy() and wt._records))
            everything = len(wt._view)
            # a table chosen after Compare: its records at once, its own columns and a Note
            wt.table_var.set("photos")
            wt._filters_changed()
            self.assertTrue(wt._view)
            self.assertTrue(all(r["table"] == "photos" for r, _s, _d, _w in wt._view))
            self.assertLess(len(wt._view), everything)
            cols = wt.rec_grid.columns()
            self.assertIn("data", cols)
            self.assertEqual(cols[-1], "Note")
            self.assertNotIn("Compare with the database' again", wt.rec_status.cget("text"))
            # compared for one table, then asking for all: says to compare again
            wt.load_records()
            self.assertTrue(self.pump(lambda: not wt._runner.busy() and getattr(
                wt, "_loaded_filters", (None,))[0] == "photos"))
            wt.table_var.set(wt.table_combo.cget("values")[0])      # all tables
            wt._filters_changed()
            self.assertIn("press 'Compare with the database' again",
                          wt.rec_status.cget("text"))
        self.run_app(scenario)


class PolishTest(AppCase):
    def test_menus_dialogs_and_statuses(self):
        from tests.fixtures import case_fixtures as cf
        from tests.fixtures import forensic_fixtures as ff
        paths = cf.build(self.tmp)
        jdir = os.path.join(self.tmp, "j")
        os.makedirs(jdir)
        journal = ff.hot_journal(jdir)

        def labels(menu):
            return [menu.entrycget(i, "label") if menu.type(i) != "separator" else "-"
                    for i in range(menu.index("end") + 1)] if menu.index("end") is not None \
                else []

        def scenario():
            app = self.app
            app._open_db(paths[0], wait=True)
            # SQL: a refused statement leaves no earlier rows under it
            app._sql_editor.insert("1.0", "SELECT 1")
            app._sql_run()
            self.assertTrue(self.pump(lambda: app._sql_result_rows))
            app._sql_editor.delete("1.0", "end")
            app._sql_editor.insert("1.0", "SELEC 1")
            app._sql_run()
            self.assertEqual(app._sql_result_rows, [])
            self.assertIsNone(app._sql_grid.source)
            self.assertIn("Not run", app._sql_status_label.cget("text"))
            # grid menus: no greyed Hide column on the row-id column, no doubled separators
            g = app._browse_grid
            self.assertTrue(self.pump(lambda: g.row_count()))
            hm = labels(g.build_header_menu(0))
            self.assertNotIn("Hide column", hm)
            cm = labels(g.build_context_menu(0, 1))
            self.assertNotEqual(cm[0], "-")
            self.assertNotEqual(cm[-1], "-")
            self.assertFalse(any(a == b == "-" for a, b in zip(cm, cm[1:])))
            # one Export ▾ in Browse
            self.assertEqual(app._browse_export_btn.cget("text"), "Export ▾")
            self.assertIn("Rows (CSV or JSON)…", labels(app._browse_export_menu()))
            # the modes in groups
            groups = app._mode_combo.cget("groups")
            self.assertEqual([g for g, _v in groups], ["Text", "Binary", "Schema"])
            # no journal: no Rollback Journal page
            fz = app._forensics
            self.assertEqual(fz.nb.tab(fz._journal_page, "state"), "hidden")
            # the one scope dialog, for a case
            app._add_paths(list(paths[1:]), wait=True)
            from dialogs import ScopeDlg
            dlg = ScopeDlg.for_case(app, app._search_members())
            self.assertEqual(len(dlg.group_titles()), 3)
            dlg._apply()
            self.assertEqual(sorted(dlg.result), sorted(m.uid for m in app._search_members()))
            # the Limits window marks changed and wrong values
            w = app.datamap.limits_window(app)
            name = "sql_history"
            w.vars[name].set("7")
            self.assertIn("changed", w.status_text(name))
            self.assertIn(name, w.changed())
            w.vars[name].set("x")
            self.assertIn("not valid", w.status_text(name))
            self.assertFalse(w.save())
            w.close()
            # a journal: the page is back
            app._open_db(journal, wait=True)
            self.assertNotEqual(fz.nb.tab(fz._journal_page, "state"), "hidden")
        self.run_app(scenario)


class TagActivityTest(AppCase):
    def test_tag_actions_are_in_the_activity_log(self):
        from engine.schema import Locator
        from engine.tags import entry_from_db_row
        path = mixed_wal(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            e = entry_from_db_row("people", Locator("rowid", 1), ["id", "name", "age"],
                                  [1, "person 0", 0])
            self.assertEqual(app.tag_entries([e], "Important"), 1)
            app.tags.set_tag([e], "Important", False)
            tags = [x for x in app._activity_log.entries() if x["kind"] == "tag"]
            self.assertEqual([x.get("action") for x in tags], ["tag", "untag"])
            self.assertIn("people", tags[0]["rows"][0])
        self.run_app(scenario)


class HelpCoversTest(unittest.TestCase):
    def test_help_has_a_section_per_tab_and_the_new_features(self):
        from dialogs import help_text
        sections = dict(help_text())
        for title in ("Overview", "Search tab", "Search modes", "Browse tab", "Row detail",
                      "Rows and their related rows", "SQL tab", "WAL tab", "Forensics tab",
                      "Timeline tab", "Tags", "Several databases (a case)",
                      "Relationships tab", "BLOB Inspector", "Exports", "Evidence safety",
                      "Limits", "Keyboard shortcuts"):
            self.assertIn(title, sections)
        text = "\n".join(sections.values())
        for label in ("Copy with related", "Database Map…", "Show value from linked table…",
                      "Find this value everywhere", "Related rows", "Skip SHA-256",
                      "Matches (CSV or JSON)…", "Rows (CSV or JSON)…", "BLOBs as files…",
                      "Links listed (CSV)…", "Diagram (SVG)…", "Forensic report (everything "
                      "found so far)…", "Recovered records listed…", "live_hash_rows",
                      "Only with rows", "Stopped after", "Ctrl+O", "Delete", "F3",
                      "Add database(s)…", "Open folder…", "SQLite GUI Analyzer"):
            self.assertIn(label, text)
        for gone in ("Forensic Analyzer", "Forensic Report…", "Export BLOBs…", "Cancelled",
                     "Row Detail", "Export CSV…\" or", "full metadata"):
            self.assertNotIn(gone, text)


class SearchSourceFilterTest(AppCase):
    def test_freed_pages_filter_shows_the_freed_page_hits(self):
        from tests.fixtures.forensic_fixtures import deleted_rows
        from constants import source_key, source_name
        self.assertEqual(source_key(source_name("Freelist")), "Freelist")
        self.assertEqual(source_key("DB"), "DB")
        path = deleted_rows(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._search_var.set("note number")
            app._limit_var.set("All")
            app._search_free_var.set(True)
            app._do_search()
            self.assertTrue(self.pump(lambda: not (app._search_thread and
                                                   app._search_thread.is_alive())))
            self.pump(lambda: False, 0.3)
            freed = [r for r in app._search_results
                     if str(r.get("source", "")).startswith("Freelist")]
            self.assertTrue(freed)
            app._sr_source_filter.set(source_name("Freelist"))
            app._filter_search_results()
            self.assertEqual(len(app._sr_filtered), len(freed))
            app._sr_source_filter.set("DB")
            app._filter_search_results()
            self.assertTrue(app._sr_filtered)
            self.assertTrue(all(not str(r.get("source", "DB")).startswith("Freelist")
                                for r in app._sr_filtered))
        self.run_app(scenario)


if __name__ == "__main__":
    unittest.main()
