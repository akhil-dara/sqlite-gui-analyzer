"""The Relationships diagram is drawn when its tab is shown, not while another tab is (the
links of a case being checked redrew a large diagram nobody saw)."""

import time

from tests.test_review_ui import AppCase


class LazyDiagramTest(AppCase):
    def test_drawn_when_shown(self):
        from tests.fixtures import make_fixtures as fx
        path = fx.relations(self.tmp)

        def scenario():
            app = self.app
            tab = app._relations_tab
            app._nb.select(app._search_frame)
            app._open_db(path, wait=True)
            self.assertTrue(self.pump(lambda: app.relations.state[0] == "done", 60))
            self.pump(lambda: False, 0.3)
            self.assertTrue(tab._draw_pending)
            self.assertIsNone(tab.model)
            app._nb.select(tab)
            self.assertTrue(self.pump(lambda: tab.model is not None, 10))
            self.assertFalse(tab._draw_pending)
            t0 = time.time()
            app._nb.select(app._search_frame)
            tab.reload()                        # hidden again: nothing drawn now
            self.assertTrue(tab._draw_pending)
            self.assertLess(time.time() - t0, 5)
        self.run_app(scenario)
