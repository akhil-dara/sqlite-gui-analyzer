"""Position indexes in a case: each database keeps its own, built for the active one, stopped
and dropped when its database is removed or the case closes. The Limits window lists the
Browse limits with the others."""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import traceback
import unittest

from tests.helpers import TempDirTest, free_tk

WORKERS = ("grid-fetch", "browse-count")


def table_db(directory, name, rows):
    path = os.path.join(directory, name)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE log(_id INTEGER PRIMARY KEY, body TEXT, n INTEGER)")
    c.executemany("INSERT INTO log VALUES (?,?,?)",
                  ((i, "%s row %d" % (name, i), i % 97) for i in range(rows)))
    c.commit()
    c.close()
    return path


class CasePositionsTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_data_")
        os.environ["SGA_DATA_DIR"] = self.data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(os.environ.pop, "SGA_DATA_DIR", None)
        self.paths = [table_db(self.tmp, "a.db", 30000), table_db(self.tmp, "b.db", 20000)]
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.geometry("1100x700+-4000+0")
        self.app.tags.warn_with_dialogs = False
        self.errors = []
        self.app.report_callback_exception = lambda e, v, tb: self.errors.append(
            "".join(traceback.format_exception(e, v, tb)))

    def pump(self, cond, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.update()
            if cond():
                return True
            time.sleep(0.005)
        return False

    def run_app(self, scenario):
        app = self.app
        done = []

        def go():
            try:
                scenario()
            except Exception:           # noqa: BLE001 - reported below
                self.errors.append(traceback.format_exc())
            finally:
                done.append(1)
                app._close_db(confirm=False)
                app.destroy()
        app.after(100, go)
        app.mainloop()
        self.assertTrue(done)
        self.assertEqual(self.errors, [])
        deadline = time.time() + 20
        left = []
        while time.time() < deadline:
            left = [t.name for t in threading.enumerate()
                    if t.is_alive() and any(n in t.name for n in WORKERS)]
            if not left:
                break
            time.sleep(0.05)
        self.assertEqual(left, [], "Browse workers left running")

    def test_each_database_keeps_its_own_indexes(self):
        app = self.app

        def ready():
            return not app._browse_pos_busy and not app._browse_grid.loading()

        def scenario():
            app._open_paths(self.paths, wait=True)
            app._nb.select(app._browse_frame)
            members = list(app.case)
            self.assertEqual(len(members), 2)
            a, b = members
            app.activate_member(a)
            app._browse_table_var.set("log")
            app._load_browse_table()
            self.assertTrue(self.pump(ready))
            info_a = a.db.session.positions_info("log")
            self.assertTrue(info_a and info_a["complete"])
            self.assertEqual(info_a["rows"], 30000)
            app.activate_member(b)
            app._browse_table_var.set("log")
            app._load_browse_table()
            self.assertTrue(self.pump(ready))
            self.assertEqual(b.db.session.positions_info("log")["rows"], 20000)
            # the other database's index stays with it, and serves its windows again
            self.assertEqual(a.db.session.positions_info("log")["rows"], 30000)
            g = app._browse_grid
            g.yview_moveto(0.9)
            self.assertTrue(self.pump(lambda: not g.loading() and not g.waiting()))
            first, _end = g.visible_row_range()
            self.assertEqual(g.row_data(first)[0][2], "b.db row %d" % first)
            self.assertGreater(b.db.session.positions_info("log")["served"], 0)
            # sorted: built for the active database only
            g.sort_by(3, True)
            self.assertTrue(self.pump(ready))
            self.assertIsNotNone(b.db.session.positions_info("log", "n", True))
            self.assertIsNone(a.db.session.positions_info("log", "n", True))
            # a database removed while a build runs on it: stopped, its indexes dropped
            app.activate_member(a)
            g.sort_by(2, False)                         # a build of 'body' starts on a
            session_a = a.db.session
            app.remove_member(a)
            self.assertIsNone(session_a.positions_info("log"))
            self.assertEqual(len(app.case), 1)
            self.assertTrue(self.pump(ready))
        self.run_app(scenario)

    def test_limits_window_lists_the_browse_limits(self):
        app = self.app

        def scenario():
            w = app.datamap.limits_window(app)
            for name in ("grid_window_rows", "checkpoint_every", "position_map_rows",
                         "native_sort_rows", "cell_draw_chars", "value_view_chars",
                         "case_max_databases"):
                self.assertIn(name, w.vars)
            w.vars["grid_window_rows"].set("10")         # below its range
            self.assertFalse(w.save())
            self.assertIn("grid_window_rows", w.status.cget("text"))
            w.close()
        self.run_app(scenario)


if __name__ == "__main__":
    unittest.main()
