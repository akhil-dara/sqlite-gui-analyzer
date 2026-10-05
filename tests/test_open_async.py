"""Opening a case never blocks the Tk thread: the databases open on a worker thread, one
after the other, each joins the case (and the navigator) as soon as it is open, a progress
window with Stop shows when it takes a while, and Close or a new open abandons the rest."""

import threading
import time

from tests.test_review_ui import AppCase
from tests.helpers import on_ci, within


class OpenAsyncTest(AppCase):
    def build(self, n):
        from tests.fixtures import workspace_fixtures as wf
        return wf.build(self.tmp, n)

    def slow_opens(self, seconds):
        """DB.open takes `seconds` longer; returns the threads each open ran on."""
        import database
        real = database.DB.open
        threads = []

        def slow(db, path, ram_limit=None, **kw):
            threads.append(threading.current_thread())
            time.sleep(seconds)
            return real(db, path, ram_limit, **kw)
        database.DB.open = slow
        self.addCleanup(setattr, database.DB, "open", real)
        return threads

    def test_opens_on_a_worker_and_joins_one_by_one(self):
        paths = self.build(16)
        threads = self.slow_opens(0.15)     # time enough to see them join one by one

        def scenario():
            app = self.app
            t0 = time.perf_counter()
            self.assertEqual(app._open_paths(paths), [])
            within(self, time.perf_counter() - t0, 0.5, "open returns")
            self.assertTrue(app.opening())
            seen = []

            def partial():
                seen.append(len(app.case))
                return not app.opening()
            self.assertTrue(self.pump(partial, 60))
            self.assertEqual([m.path for m in app.case], list(paths))   # in the order given
            # some joined before the rest (a CI machine that drew nothing until the end
            # only warns: what counts is that the window kept working while they opened)
            if not any(0 < n < 16 for n in seen):
                msg = "the databases were seen only all at once: %s" % sorted(set(seen))
                if on_ci():
                    print("::warning title=Opening was not seen one by one::%s" % msg)
                else:
                    self.fail(msg)
            main = threading.main_thread()
            self.assertEqual(len(threads), 16)
            self.assertTrue(all(t is not main for t in threads))
            self.assertIs(app.case.active, list(app.case)[0])
            self.assertIn("16 databases", app._db_info.full_text())
            self.assertEqual(len(app._navigator.db_lines()), 16)
            self.assertEqual([j for j in app._jobs if "Opening" in j.title], [])
            self.assertEqual(self.shown, [])
        self.run_app(scenario)

    def test_progress_window_and_stop(self):
        paths = self.build(16)
        self.slow_opens(0.15)

        def scenario():
            import tkinter as tk
            app = self.app
            app._open_paths(paths)
            job = app._open_state["job"]

            def window():
                return [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)
                        and w.title().startswith("Opening")]
            self.assertTrue(self.pump(lambda: window(), 10))   # shown after a moment
            self.assertIn("of 16", job.text())
            job.cancel()                                        # Stop
            self.assertTrue(self.pump(lambda: not app.opening(), 30))
            self.assertGreater(len(app.case), 0)
            self.assertLess(len(app.case), 16)
            self.assertEqual(window(), [])
            said = "\n".join(str(a) for a in self.shown)
            self.assertIn("were not opened", said)
            self.assertIn("Stopped", said)
            # what was opened is a working case
            self.assertIsNotNone(app.case.active)
            self.assertTrue(app.db.ok)
        self.run_app(scenario)

    def test_close_while_opening_abandons_the_rest(self):
        paths = self.build(16)
        self.slow_opens(0.1)

        def scenario():
            app = self.app
            app._open_paths(paths)
            self.assertTrue(self.pump(lambda: len(app.case) >= 2, 30))
            state = app._open_state
            app._close_db(confirm=False)
            self.assertFalse(app.opening())
            self.assertEqual(len(app.case), 0)
            self.pump(lambda: state["job"].finished, 30)
            self.pump(lambda: False, 0.5)
            self.assertEqual(len(app.case), 0)                  # nothing joined later
            self.assertTrue(state["abandoned"])
        self.run_app(scenario)

    def test_open_while_opening_replaces_and_add_waits(self):
        paths = self.build(8)
        self.slow_opens(0.05)

        def scenario():
            app = self.app
            app._open_paths(paths[:4])
            app._add_databases(paths[4:])       # asked while the first open runs: it waits
            self.assertTrue(self.pump(lambda: not app.opening(), 60))
            self.assertEqual([m.path for m in app.case], list(paths))
            app._open_paths(paths[:3])
            app._open_paths(paths[5:])          # a new case: the first open is abandoned
            self.assertTrue(self.pump(lambda: not app.opening(), 60))
            self.assertEqual([m.path for m in app.case], list(paths[5:]))
        self.run_app(scenario)

    def test_the_last_steps_are_spread_and_a_close_stops_them(self):
        paths = self.build(4)

        def scenario():
            app = self.app
            order = []
            real_save, real_overview = app._save_case, app._update_overview
            app._save_case = lambda: (order.append(("save", app.opening())), real_save())[1]
            app._update_overview = lambda *a, **k: (order.append(("overview", app.opening())),
                                                    real_overview(*a, **k))[1]
            done = []
            app._open_paths(paths, on_done=lambda ms: done.append(len(ms)))
            self.assertTrue(self.pump(lambda: not app.opening(), 30))
            self.assertEqual(done, [4])
            # the case's last steps ran while opening() still said so, before on_done
            self.assertIn(("overview", True), order)
            self.assertIn(("save", True), order)
            # a close while those steps run: they stop and on_done is not called
            del order[:]
            del done[:]
            closed = []

            def close_midway(*a, **k):
                if app._open_finishing is not None and not closed:
                    closed.append(True)
                    app._close_db(confirm=False)
                return real_overview(*a, **k)
            app._update_overview = close_midway
            app._open_paths(paths[:2], on_done=lambda ms: done.append(len(ms)))
            self.assertTrue(self.pump(lambda: bool(closed) and not app.opening(), 30))
            self.pump(lambda: False, 0.3)
            self.assertEqual(done, [])                  # stopped: no on_done
            self.assertEqual(len(app.case), 0)
            app._save_case, app._update_overview = real_save, real_overview
        self.run_app(scenario)

    def test_wait_returns_the_members(self):
        paths = self.build(4)

        def scenario():
            app = self.app
            members = app._open_paths(paths, wait=True)
            self.assertEqual([m.path for m in members], list(paths))
            self.assertFalse(app.opening())
            more = app._add_paths([paths[0]], wait=True)          # already open: nothing
            self.assertEqual(more, [])
        self.run_app(scenario)

    def test_a_file_that_is_not_a_database(self):
        import os
        paths = self.build(3)
        bad = os.path.join(self.tmp, "notes.db")
        with open(bad, "wb") as f:
            f.write(b"not a database at all" * 50)

        def scenario():
            app = self.app
            app._open_paths([paths[0], bad, paths[1]])
            self.assertTrue(self.pump(lambda: not app.opening(), 30))
            self.assertEqual([m.path for m in app.case], [paths[0], paths[1]])
            said = "\n".join(str(a) for a in self.shown)
            self.assertIn("notes.db: cannot be opened", said)
        self.run_app(scenario)
