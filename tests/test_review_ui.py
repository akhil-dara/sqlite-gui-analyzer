"""The fixes of the analyst review, checked in the running app (inside the Tk main loop):

- WAL tab, Records of several tables: never one table's headers over another's records;
  one table chosen: its own columns; a record the database cannot be read for is 'could not
  compare', never 'not in DB';
- Search: 'Max rows/table' names the tables it cut; All has no limit; the errors button only
  when there are errors; the scope dialog never hides tables still being counted;
- SQL tab: comments before a SELECT are fine, a writing statement is refused inline;
- exports: a Browse export writes the rows and a manifest with the evidence SHA-256;
- the activity log notes the open, the search, the export and the close;
- a WAL file that cannot be read still gets its tab, saying why; other files next to the
  database are listed in Evidence.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import traceback
import unittest

from tests.helpers import TempDirTest, free_tk, off_screen_windows


def mixed_wal(directory):
    """A database whose WAL holds committed rows of two tables with different columns."""
    path = os.path.join(directory, "mixed.db")
    work = os.path.join(directory, "_work")
    os.makedirs(work)
    wpath = os.path.join(work, "mixed.db")
    c = sqlite3.connect(wpath, isolation_level=None)
    c.execute("PRAGMA page_size=1024")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    c.execute("CREATE TABLE photos(id INTEGER PRIMARY KEY, data BLOB)")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.executemany("INSERT INTO people(name, age) VALUES (?, ?)",
                  [("person %d" % i, i) for i in range(40)])
    c.executemany("INSERT INTO photos(data) VALUES (?)", [(bytes([i]) * 30,) for i in range(40)])
    c.execute("UPDATE people SET age = 99 WHERE id = 3")
    for sfx in ("", "-wal"):
        shutil.copyfile(wpath + sfx, path + sfx)
    c.close()
    return path


class AppCase(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_review_data_")
        os.environ["SGA_DATA_DIR"] = self.data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(os.environ.pop, "SGA_DATA_DIR", None)
        off_screen_windows(self)        # every window of the app stays off the screen
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.geometry("1200x750+-4000+0")
        self.app.tags.warn_with_dialogs = False
        self.errors = []
        self.app.report_callback_exception = lambda e, v, tb: self.errors.append(
            "".join(traceback.format_exception(e, v, tb)))
        import tkinter.messagebox as mb
        saved = (mb.showinfo, mb.showwarning, mb.showerror, mb.askyesno)
        self.shown = []
        mb.showinfo = mb.showwarning = mb.showerror = \
            lambda *a, **k: self.shown.append(a)
        mb.askyesno = lambda *a, **k: True

        def restore():
            mb.showinfo, mb.showwarning, mb.showerror, mb.askyesno = saved
        self.addCleanup(restore)

    def pump(self, cond, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.update()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def run_app(self, scenario):
        app = self.app

        def go():
            try:
                scenario()
            except Exception:           # noqa: BLE001 - reported below
                self.errors.append(traceback.format_exc())
            finally:
                try:
                    app._close_db(confirm=False, wait=True)
                except Exception:       # noqa: BLE001 - reported below
                    self.errors.append(traceback.format_exc())
                app.destroy()
        app.after(50, go)
        app.mainloop()
        self.assertEqual(self.errors, [])


class WalRecordsTest(AppCase):
    def test_records_of_several_tables_never_share_headers(self):
        path = mixed_wal(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            wt = app._wal_frame
            self.assertTrue(app._wal_tab_added)
            wt.view_var.set("records")
            wt._switch_view()
            wt.load_records()
            self.assertTrue(self.pump(lambda: not wt._runner.busy() and wt._records))
            tables = set(r["table"] for r, _s, _d, _w in wt._records)
            self.assertEqual(tables, set(["people", "photos"]))
            cols = wt.rec_grid.columns()
            self.assertIn("Values", cols)            # all tables: one Values column
            self.assertFalse(set(["name", "age", "data"]) & set(cols))
            ti = cols.index("Table")
            rows = wt.rec_grid.fetch_rows(0, wt.rec_grid.row_count() - 1)
            self.assertEqual(len(rows), len(wt._view))
            for (values, _flags), (rec, _s, _d, _w) in zip(rows, wt._view):
                self.assertEqual(values[ti], rec["table"])
                self.assertTrue(values[cols.index("Values")].startswith("id="))
            # the changed row is 'different', naming its column
            diff = [x for x in wt._records if x[1] == "different"]
            self.assertTrue(any("age" in d for _r, _s, d, _w in diff))
            # one table: its own columns
            wt.table_var.set("photos")
            wt.load_records()
            self.assertTrue(self.pump(lambda: not wt._runner.busy() and
                                      getattr(wt, "_loaded_filters", (None,))[0] == "photos"))
            cols = wt.rec_grid.columns()
            self.assertIn("data", cols)
            self.assertNotIn("name", cols)
        self.run_app(scenario)


class CompareTest(unittest.TestCase):
    def test_a_read_error_is_not_taken_for_a_missing_row(self):
        from types import SimpleNamespace
        from wal_tab import compare_with_db, same_value

        def boom(table, loc):
            raise IOError("disk went away")
        db = SimpleNamespace(session=SimpleNamespace(row=boom))
        status, _d, _n, reason = compare_with_db(db, "t", object(), ["a"], [1], set(["t"]),
                                                 set())
        self.assertEqual(status, "error")
        self.assertIn("disk went away", reason)
        db = SimpleNamespace(session=SimpleNamespace(row=lambda t, l: None))
        self.assertEqual(compare_with_db(db, "t", object(), ["a"], [1], set(["t"]), set())[0],
                         "not_in_db")
        self.assertEqual(compare_with_db(db, "x", object(), ["a"], [1], set(["t"]),
                                         set(["x"]))[0], "wal_table")
        self.assertTrue(same_value(1, 1.0))
        self.assertFalse(same_value(b"1", "1"))
        self.assertFalse(same_value("1", 1))
        self.assertTrue(same_value(b"ab", b"ab"))
        self.assertFalse(same_value(None, ""))


class SearchAndSqlTest(AppCase):
    def test_capped_tables_are_named_and_all_has_no_limit(self):
        path = mixed_wal(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)

            def search(term, limit):
                app._search_var.set(term)
                app._limit_var.set(limit)
                app._do_search()
                self.assertTrue(self.pump(lambda: not (app._search_thread and
                                                       app._search_thread.is_alive())))
                self.pump(lambda: False, 0.3)
            search("person", "100")
            self.assertEqual(app._search_capped, [])
            self.assertNotIn("stopped at", app._search_status.cget("text"))
            self.assertFalse(app._search_err_btn.winfo_manager())   # no errors: no button
            # a limit smaller than the matches names the table
            search("person", "5")
            self.assertEqual([w for _u, w, _n in app._search_capped], ["people"])
            text = app._search_status.cget("text")
            self.assertIn("stopped at 5 matching rows", text)
            self.assertIn("people", text)
            self.assertTrue(any(v.startswith("people (5+)")
                                for v in app._sr_table_filter.cget("values")))
            search("person", "All")
            self.assertIsNone(app._search_limit)
            self.assertEqual(app._search_capped, [])
            rows = set(repr(h.get("locator")) for h in app._search_results)
            self.assertEqual(len(rows), 40)
        self.run_app(scenario)

    def test_sql_allows_comments_and_refuses_writes_inline(self):
        from utils import sql_first_keyword, sql_reads_only
        self.assertEqual(sql_first_keyword("-- note\n/* x */ ( SELECT 1)"), "SELECT")
        self.assertTrue(sql_reads_only("  -- a\nWITH c AS (SELECT 1) SELECT * FROM c"))
        self.assertTrue(sql_reads_only("PRAGMA table_info(t)"))
        self.assertFalse(sql_reads_only("/* x */ DELETE FROM t"))
        self.assertFalse(sql_reads_only("-- only a comment"))
        path = mixed_wal(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._sql_editor.insert("1.0", "-- the people\nSELECT * FROM people")
            app._sql_run()
            self.assertTrue(self.pump(lambda: "returned" in app._sql_status_label.cget("text")
                                      or "No rows" in app._sql_status_label.cget("text")))
            if app.db.session.sql_sees_current_state:
                self.assertEqual(len(app._sql_result_rows), 40)
                self.assertFalse(app._sql_notice.winfo_manager())
            else:
                # Python < 3.11: SQL sees the main file only, and the tab says so
                self.assertTrue(app._sql_notice.winfo_manager())
                self.assertIn("committed WAL frame", app._sql_notice_lbl.cget("text"))
            app._sql_editor.delete("1.0", "end")
            app._sql_editor.insert("1.0", "DROP TABLE people")
            app._sql_run()
            self.assertIn("Not run", app._sql_status_label.cget("text"))
            self.assertEqual(self.shown, [])             # said inline, no dialog
        self.run_app(scenario)

    def test_scope_dialog_keeps_tables_still_counted(self):
        from dialogs import ScopeDlg
        from tests.helpers import tk_root
        root = tk_root(self)
        counts = {"a": "?", "b": 0, "c": 5, "d": "~12"}
        dlg = ScopeDlg(root, ["a", "b", "c", "d"], counts, ["a", "b", "c", "d"])
        self.assertEqual(dlg._visible_tables, ["a", "c", "d"])   # only b is known empty
        self.assertIn("1 not counted yet", dlg._stats_label.cget("text"))
        counts["a"] = 0
        dlg._watch_counts()
        self.assertEqual(dlg._visible_tables, ["c", "d"])
        dlg.destroy()


class ExportAndLogTest(AppCase):
    def test_browse_export_has_provenance_and_the_log_says_so(self):
        path = mixed_wal(self.tmp)
        out = tempfile.mkdtemp(prefix="sga_review_out_")
        self.addCleanup(shutil.rmtree, out, True)
        target = os.path.join(out, "people.json")
        import app as app_module
        app_module.export_options = lambda *a, **k: {"scope": "rows", "fmt": "json",
                                                     "blob_mode": "hex", "skip_hidden": False}
        app_module.ask_path = lambda *a, **k: target
        self.addCleanup(setattr, app_module, "export_options",
                        __import__("jobs").export_options)
        self.addCleanup(setattr, app_module, "ask_path", __import__("jobs").ask_path)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._browse_table_var.set("people")
            app._load_browse_table()
            self.assertTrue(self.pump(lambda: app._browse_source.row_count() == 40))
            app._browse_export()
            self.assertTrue(self.pump(lambda: not app._jobs and os.path.exists(target)))
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["rows"]), 40)
            self.assertTrue(data["end"]["complete"])
            prov = data["provenance"]
            files = prov["databases"][0]["files"]
            main = [f for f in files if f["role"] == "main"][0]
            self.assertEqual(len(main["sha256"]), 64)
            self.assertIn("UTC", prov["exported_utc"])
            manifest = target + ".manifest.json"
            with open(manifest, encoding="utf-8") as f:
                man = json.load(f)
            self.assertEqual(man["files"][0]["size"], os.path.getsize(target))
            log = app._activity_log
            kinds = [e["kind"] for e in log.entries()]
            self.assertIn("open", kinds)
            self.assertIn("export", kinds)
            self.log_path = log.path
        self.run_app(scenario)
        from engine.activity import ActivityLog
        kinds = [e["kind"] for e in ActivityLog([path]).entries()]
        self.assertEqual(kinds[-1], "close")


class EvidenceTest(AppCase):
    def test_unreadable_wal_gets_its_tab_and_other_files_are_listed(self):
        path = mixed_wal(self.tmp)
        with open(path + "-wal", "r+b") as f:
            f.write(b"\x00" * 32)                 # the header no longer reads
        shutil.copyfile(path, path + "-wal.bak")

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            self.assertTrue(app._wal_tab_added)
            wt = app._wal_frame
            self.assertTrue(wt.problem.winfo_manager())
            self.assertIn("could not be read", wt.problem.cget("text"))
            text = app._evidence_text(app.case.active)
            self.assertIn("mixed.db-wal.bak", text)
            self.assertIn("NOT used", text)
            self.assertIn("UTC", text)
        self.run_app(scenario)


class HelpTest(unittest.TestCase):
    def test_help_names_what_the_ui_shows(self):
        from constants import SEARCH_MODES, WAL_STATES
        from dialogs import help_text
        text = "\n".join(t + "\n" + b for t, b in help_text())
        for mode in SEARCH_MODES:
            self.assertIn(mode, text)
        for label in ("Include BLOB bytes", "Include decoded BLOBs", "Include views",
                      "Include WAL row versions", "Include freed pages", "Row panel",
                      "Inspect BLOB…", "Ctrl+E", "Ctrl+Enter", "Alt+Up/Down", "Escape",
                      "Freed Pages", "Recovered Records", "verify_rehash_bytes",
                      "Activity log", "manifest.json"):
            self.assertIn(label, text)
        for label, _fg, _bg, desc in WAL_STATES.values():
            self.assertIn("%s: %s" % (label, desc), text)
        for gone in ("Deleted Pages", "BLOB/Hex", "Hex Bytes", "title bar", "Hidden Data"):
            self.assertNotIn(gone, text)


class FlowFrameTest(unittest.TestCase):
    def test_a_narrow_row_wraps_instead_of_clipping(self):
        import tkinter.ttk as ttk
        from tests.helpers import tk_root
        from widgets import FlowFrame
        root = tk_root(self)
        root.deiconify()
        root.geometry("300x200+-4000+0")
        f = FlowFrame(root)
        f.pack(fill="x")
        buttons = [f.add(ttk.Button(f, text="Button number %d" % i)) for i in range(6)]
        for _ in range(20):
            root.update()
        self.assertGreater(f.line_count(), 1)
        for b in buttons:
            self.assertGreaterEqual(b.winfo_width() + 2, min(b.winfo_reqwidth(), 300))
            self.assertLessEqual(b.winfo_x() + b.winfo_width(), f.winfo_width() + 2)
        f.show(buttons[0], False)
        root.update()
        self.assertEqual(buttons[0].winfo_manager(), "")

    def test_a_longer_text_after_layout_is_not_cut(self):
        # a text change fires no <Configure> on a placed item: the row must
        # still re-lay-out so the longer text is not clipped
        import time
        import tkinter.ttk as ttk
        from tests.helpers import tk_root
        from widgets import FlowFrame
        root = tk_root(self)
        root.deiconify()
        root.geometry("600x200+-4000+0")
        f = FlowFrame(root)
        f.pack(fill="x")
        cb = f.add(ttk.Checkbutton(f, text="Only the selected table"), gap=8)
        for _ in range(20):
            root.update()
        cb.configure(text="Only a_much_longer_table_name_here")
        deadline = time.time() + 5
        while time.time() < deadline:
            root.update()
            time.sleep(0.05)
            if cb.winfo_width() + 4 >= cb.winfo_reqwidth():
                break
        self.assertGreaterEqual(cb.winfo_width() + 4, cb.winfo_reqwidth(),
                                "checkbutton text is cut")


class MenuButtonTest(unittest.TestCase):
    def test_posts_menu_with_a_single_arrow(self):
        # a menu button is a plain push-button (one arrow in its text), not a
        # ttk.Menubutton (which draws a second, native indicator arrow)
        from tests.helpers import tk_root
        from widgets import menu_button
        root = tk_root(self)
        btn, menu = menu_button(root, "Export \u25be")
        btn.pack()
        root.update()
        self.assertEqual(btn.winfo_class(), "TButton")
        self.assertEqual(btn.cget("text"), "Export \u25be")
        menu.add_command(label="Hi", command=lambda: None)
        # a real post runs the platform's modal menu loop (on Windows it waits for a click):
        # record the call instead
        posted = []
        menu.tk_popup = lambda x, y, *a: posted.append((x, y))
        btn.invoke()
        root.update()
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][1], btn.winfo_rooty() + btn.winfo_height())


if __name__ == "__main__":
    unittest.main()
