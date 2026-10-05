"""The time a finished search reports is the time the user waited: from the click to the
results being shown (it once left out the last steps and the hand-over to the Tk thread),
and the tables are searched largest first."""

import os
import re
import sqlite3
import time

from tests.test_review_ui import AppCase


class SearchTimingTest(AppCase):
    def test_status_time_is_the_wall_time(self):
        from tests.fixtures import make_fixtures as fx
        path = fx.relations(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            self.pump(lambda: not any(isinstance(v, str) for v in app.case.active.counts.values()),
                      30)
            app._search_var.set("a")
            t0 = time.time()
            app._do_search()
            self.assertTrue(self.pump(lambda: app._search_status.summary().startswith(
                ("Complete", "Stopped")), 60))
            wall = time.time() - t0
            m = re.search(r"([0-9.]+)s\)", app._search_status.summary())
            self.assertIsNotNone(m, app._search_status.summary())
            said = float(m.group(1))
            self.assertAlmostEqual(said, app._search_elapsed, places=1)
            # the status is set once the results are ready: at most a poll later than that
            self.assertLessEqual(app._search_elapsed, wall + 0.01)
            self.assertLess(wall - app._search_elapsed, 0.25)
        self.run_app(scenario)

    def test_largest_tables_first(self):
        from engine.session import Session
        path = os.path.join(self.tmp, "sizes.db")
        c = sqlite3.connect(path)
        for name, n in (("small", 5), ("big", 3000), ("mid", 300)):
            c.execute("CREATE TABLE %s(x TEXT)" % name)
            c.executemany("INSERT INTO %s VALUES (?)" % name, [("hello %d" % i,)
                                                              for i in range(n)])
        c.commit()
        c.close()
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        for t in ("small", "big", "mid"):
            s.count(t)
        order = [name for name, _hits, _e in s.search_tables(
            ["small", "big", "mid"], "hello", "ci", limit=10, workers=1)]
        self.assertEqual(order, ["big", "mid", "small"])
