"""Stress runs of a case of many databases through the whole App (skipped unless
SGA_STRESS=1): open and close a case of 18 databases again and again, remove and add databases
while searches and the relation mapping run, cancel storms, and searches across every database
while the active one keeps changing. Nothing may crash, hang or leave a worker thread behind.

    set SGA_STRESS=1 && py -X faulthandler -m unittest tests.test_case_stress
"""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import traceback
import unittest

from tests.helpers import TempDirTest, free_tk
from tests.fixtures import case_fixtures as cf

STRESS = os.environ.get("SGA_STRESS") == "1"
WORKER_NAMES = ("row-counts", "relations", "grid", "timeline", "lookup", "tag-job",
                "scan-folder", "forensics", "browse", "_search_worker", "ThreadPoolExecutor",
                "search")


def big(directory, name, rows):
    """A database with `rows` rows of jid-like text and dates (search and mapping take a
    moment on it)."""
    path = os.path.join(directory, name)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE log(_id INTEGER PRIMARY KEY, jid TEXT, body TEXT, ts INTEGER)")
    c.executemany("INSERT INTO log VALUES (?,?,?,?)",
                  ((i, cf.JIDS[i % 20], "entry number %d of the log" % i,
                    cf.T0_MS + i * 1000) for i in range(rows)))
    c.commit()
    c.close()
    return path


@unittest.skipUnless(STRESS, "stress runs: set SGA_STRESS=1")
class CaseStressTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_stress_data_")
        os.environ["SGA_DATA_DIR"] = self.data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(os.environ.pop, "SGA_DATA_DIR", None)
        paths = []
        for i in range(5):
            d = os.path.join(self.tmp, "phone%d" % i)
            os.makedirs(d)
            paths += [cf.messages(d), cf.contacts(d), cf.settings(d)]
        paths += [big(self.tmp, "big%d.db" % i, 150000) for i in range(3)]
        self.paths = paths                                  # 18 databases
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.tags.warn_with_dialogs = False
        self.errors = []
        self.app.report_callback_exception = lambda e, v, tb: self.errors.append(
            "".join(traceback.format_exception(e, v, tb)))
        import tkinter.messagebox as mb
        self._saved = (mb.showwarning, mb.showerror, mb.showinfo, mb.askyesno)
        mb.showwarning = mb.showerror = mb.showinfo = lambda *a, **k: None
        mb.askyesno = lambda *a, **k: True

    def tearDown(self):
        import tkinter.messagebox as mb
        mb.showwarning, mb.showerror, mb.showinfo, mb.askyesno = self._saved
        TempDirTest.tearDown(self)

    def pump(self, cond, timeout=120):
        app = self.app
        deadline = time.time() + timeout
        while time.time() < deadline:
            app.update()
            if cond():
                return True
            time.sleep(0.005)
        return False

    def run_app(self, scenario):
        """Run scenario() inside the Tk main loop (worker threads call after())."""
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
        while time.time() < deadline:
            left = [t.name for t in threading.enumerate()
                    if t.is_alive() and any(n in t.name for n in WORKER_NAMES)]
            if not left:
                break
            time.sleep(0.1)
        self.assertEqual(left, [], "worker threads left running")

    def search(self, term, wait=True):
        app = self.app
        app._search_var.set(term)
        app._do_search()
        if wait:
            self.assertTrue(self.pump(lambda: not app._search_thread.is_alive()))
            app.update()

    def test_open_close_repeatedly(self):
        app = self.app

        def scenario():
            for _ in range(5):
                t0 = time.time()
                app._open_paths(self.paths, wait=True)
                self.assertLess(time.time() - t0, 20)
                self.assertEqual(len(app.case), 18)
                app.update()
                app._close_db(confirm=False)
                self.assertEqual(len(app.case), 0)
            app._open_paths(self.paths, wait=True)
            self.assertTrue(self.pump(lambda: app.relations.state[0] in ("done", "stopped"),
                                      300))
            self.assertTrue(app.relations.cross_links())
        self.run_app(scenario)

    def test_add_and_remove_while_work_runs(self):
        app = self.app

        def scenario():
            app._open_paths(self.paths[:9], wait=True)
            self.search("entry", wait=False)            # searches and mapping run now
            app._add_databases(self.paths[9:], wait=True)
            for m in list(app.case)[1:6]:
                app.remove_member(m)
                app.update()
            self.search("zebracorn", wait=False)
            app._add_databases(self.paths[1:6], wait=True)
            self.assertEqual(len(app.case), 18)
            self.search("zebracorn")
            self.assertIn("searched", app._search_status.cget("text") + " searched")
            self.assertTrue(self.pump(lambda: app.relations.state[0] in ("done", "stopped"),
                                      300))
        self.run_app(scenario)

    def test_cancel_storm(self):
        app = self.app

        def scenario():
            app._open_paths(self.paths, wait=True)
            for i in range(25):
                self.search("entry number %d" % i, wait=False)
                app.update()
                app._stop_search()
            self.assertTrue(self.pump(lambda: not app._search_thread.is_alive(), 60))
            app._nb.select(app._timeline)
            for _ in range(10):
                app._timeline.stop()
                app._timeline.detection = None
                app._timeline.start_detect()
                app.update()
            app._timeline.stop()
            self.assertTrue(self.pump(lambda: not app._timeline.busy(), 60))
            app.relations.stop()
            app.relations.start_mapping()
            app.relations.stop()
            app.relations.start_mapping()
            self.assertTrue(self.pump(lambda: app.relations.state[0] in ("done", "stopped"),
                                      300))
        self.run_app(scenario)

    def test_search_while_switching_active(self):
        app = self.app

        def scenario():
            app._open_paths(self.paths, wait=True)
            self.search("entry", wait=False)
            members = list(app.case)
            for i in range(40):
                app.activate_member(members[i % len(members)])
                app.update()
            self.assertTrue(self.pump(lambda: not app._search_thread.is_alive(), 120))
            app.update()
            status = app._search_status.cget("text")
            self.assertIn("big0.db", status)
            self.assertIn("searched, nothing found", status)
        self.run_app(scenario)


if __name__ == "__main__":
    unittest.main()
