"""The Forensics tab settles: with recovered records shown, the panes stop resizing at once
(setting the sashes re-wraps the record panel, which once re-laid the pane and set the
sashes again, ~25 times a second, redrawing the records grid without end)."""
import tests.helpers  # noqa: F401 - the src path
from tests.fixtures import make_fixtures as mf
from tests.test_review_ui import AppCase


class ForensicsPanesSettleTest(AppCase):
    def test_no_resize_loop_at_idle(self):
        path = mf.wal_states(self.tmp)

        def scenario():
            app = self.app
            app.state("normal")
            app.geometry("1200x750+-4000+0")    # the size the loop showed at
            self.pump(lambda: False, 0.5)
            app._open_db(path, wait=True)
            fx = app._forensics
            self.pump(lambda: fx._scanned, 40)  # the recovery that starts by itself
            app._nb.select(fx)
            self.pump(lambda: not fx._busy, 30)
            self.pump(lambda: False, 1.0)
            g = fx.carve_grid
            count = [0]
            g._cv.bind("<Configure>", lambda e: count.__setitem__(0, count[0] + 1), add="+")
            before = sum(g.frames.values())
            # the event loop for 2 s (a loop of resizes never lets update() return: the
            # nested mainloop with a timed quit measures it instead of hanging)
            app.after(2000, app.quit)
            app.mainloop()
            self.assertLessEqual(count[0], 2, "the records grid kept resizing at idle")
            self.assertLessEqual(sum(g.frames.values()) - before, 4,
                                 "the records grid kept redrawing at idle")
        self.run_app(scenario)
