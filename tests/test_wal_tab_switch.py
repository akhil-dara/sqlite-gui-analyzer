"""In a case, the WAL tab stays selected when the active database changes - to one without a
WAL (it then says there is none) and back; the selection never falls to the SQL tab."""
import os

from tests.fixtures import case_fixtures as cf
from tests.test_review_ui import AppCase


class WalTabSwitchTest(AppCase):
    def test_wal_tab_keeps_the_selection(self):
        d = os.path.join(self.tmp, "case")
        os.makedirs(d)
        with_wal = cf.messages(d, wal=True)
        without = cf.contacts(d)

        def scenario():
            app = self.app
            app._open_paths([with_wal, without], wait=True)
            self.pump(lambda: False, 0.5)
            members = dict((os.path.basename(m.db.evidence.main), m) for m in app.case)
            app.activate_member(members["messages.db"])
            self.pump(lambda: False, 0.3)
            app._nb.select(app._wal_frame)
            self.pump(lambda: False, 0.3)
            self.assertEqual(app._nb.tab(app._nb.select(), "text"), "WAL")
            self.assertIn("frames", app._wal_frame.summary.cget("text"))
            app.activate_member(members["contacts.db"])
            self.pump(lambda: False, 0.5)
            self.assertEqual(app._nb.tab(app._nb.select(), "text"), "WAL")
            self.assertIn("contacts.db has no write-ahead log",
                          app._wal_frame.problem.cget("text"))
            app.activate_member(members["messages.db"])
            self.pump(lambda: False, 0.5)
            self.assertEqual(app._nb.tab(app._nb.select(), "text"), "WAL")
            self.assertIn("frames", app._wal_frame.summary.cget("text"))
            self.assertFalse(app._wal_frame.problem.winfo_manager())
        self.run_app(scenario)
