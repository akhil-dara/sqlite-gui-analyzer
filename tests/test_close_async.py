"""Closing a case does not hold the Tk thread while the evidence is verified: the databases
close at once, their files are verified on a worker thread, and the header, the activity log
and (for a changed file) a warning then say what was verified."""

import os
import time

from tests.test_review_ui import AppCase
from tests.helpers import within


class CloseAsyncTest(AppCase):
    def build(self, n):
        from tests.fixtures import workspace_fixtures as wf
        return wf.build(self.tmp, n)

    def test_close_verifies_afterwards(self):
        paths = self.build(6)

        def scenario():
            from engine.activity import ActivityLog
            app = self.app
            app._open_paths(paths, wait=True)
            t0 = time.perf_counter()
            app._close_db(confirm=False)
            within(self, time.perf_counter() - t0, 1.0, "close")
            self.assertEqual(len(app.case), 0)
            self.assertTrue(self.pump(lambda: not app.closing(), 30))
            self.assertEqual(len(app.last_closed), 6)
            self.assertTrue(all("verified unchanged" in c for c in app.last_closed))
            self.assertIn("last closed", app._db_info.full_text())
            kinds = [e["kind"] for e in ActivityLog(paths).entries()]
            self.assertEqual(kinds.count("close"), 6)
            self.assertEqual(self.shown, [])
        self.run_app(scenario)

    def test_a_file_changed_meanwhile_is_reported(self):
        paths = self.build(2)

        def scenario():
            app = self.app
            app._open_paths(paths, wait=True)
            st = os.stat(paths[1])
            os.utime(paths[1], ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
            app._close_db(confirm=False)
            self.assertTrue(self.pump(lambda: not app.closing(), 30))
            self.assertTrue(any("CHANGED" in c for c in app.last_closed))
            said = "\n".join(str(a) for a in self.shown)
            self.assertIn("Evidence changed", said)
        self.run_app(scenario)

    def test_a_new_open_keeps_its_header_and_wait(self):
        paths = self.build(3)

        def scenario():
            app = self.app
            app._open_paths(paths[:2], wait=True)
            app._close_db(confirm=False)
            app._open_db(paths[2], wait=True)
            app.wait_closed()
            self.assertFalse(app.closing())
            self.assertNotIn("No database loaded", app._db_info.full_text())
            self.assertEqual(len(app.last_closed), 2)
            # wait=True: verified before the close returns
            app._close_db(confirm=False, wait=True)
            self.assertFalse(app.closing())
            self.assertEqual(len(app.last_closed), 1)
            self.assertIn("last closed", app._db_info.full_text())
        self.run_app(scenario)
