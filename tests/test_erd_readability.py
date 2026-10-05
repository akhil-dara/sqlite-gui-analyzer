"""The Relationships diagram of a large database stays readable, measured by its connector
crossings on a graph shaped like a messaging app's (a few hub tables referred to by hundreds
of others, most links weaker): the diagram draws the trusted links only (weaker ones are
listed, not drawn), and neighbouring cards are swapped where that removes crossings."""

import time
import unittest

from tests.test_review_ui import AppCase          # (puts src/ on the path first)
from engine import erd, limits
from tests.fixtures.erd_shapes import messaging_shape

# measured when written (all links 8,808 crossings, trusted 534), with a little room
ALL_BUDGET = 9300
TRUSTED_BUDGET = 600


class ReadabilityTest(unittest.TestCase):
    def setUp(self):
        limits.reset()
        self.links, self.specs = messaging_shape()

    def arrange(self, links):
        t0 = time.perf_counter()
        model = erd.ErdModel(links, self.specs)
        lay = erd.arrange(model)
        return model, lay, time.perf_counter() - t0

    def test_crossing_budgets(self):
        model, lay, secs = self.arrange(self.links)
        self.assertEqual(lay.overlaps(), [])
        self.assertLess(secs, 2.0)
        self.assertLessEqual(lay.stats["crossings"], ALL_BUDGET)
        self.assertLess(lay.stats["crossings"], lay.stats["initial_crossings"] / 2)
        trusted = [l for l in self.links if l.confident]
        tmodel, tlay, _s = self.arrange(trusted)
        self.assertEqual(tlay.overlaps(), [])
        self.assertLessEqual(tlay.stats["crossings"], TRUSTED_BUDGET)
        # crossings per connector drawn: several times fewer in the trusted Overview
        per_all = lay.stats["crossings"] / float(len(model.rels))
        per_trusted = tlay.stats["crossings"] / float(len(tmodel.rels))
        self.assertLess(per_trusted * 3, per_all)
        self.assertLess(len(tmodel.tables), len(model.tables))

    def test_transposition_never_adds_crossings(self):
        saved = erd.TRANSPOSE_ROUNDS
        try:
            erd.TRANSPOSE_ROUNDS = 0
            _m, plain, _s = self.arrange(self.links)
        finally:
            erd.TRANSPOSE_ROUNDS = saved
        _m, swapped, _s = self.arrange(self.links)
        self.assertLessEqual(swapped.stats["crossings"], plain.stats["crossings"])
        self.assertLess(swapped.stats["crossings"], plain.stats["crossings"])  # it helps here

    def test_deterministic(self):
        a = self.arrange(self.links)[1].positions()
        b = self.arrange(self.links)[1].positions()
        self.assertEqual(a, b)


class OverviewTrustedOnlyTest(AppCase):
    def test_the_overview_draws_the_trusted_links_within_budget(self):
        links, _specs = messaging_shape()
        trusted = [l for l in links if l.confident]

        def scenario():
            app = self.app
            tab = app._relations_tab
            app._nb.select(tab)
            self.pump(lambda: False, 0.2)
            tab.all_links = links
            tab.refresh()
            tab.overview = True
            tab.draw()
            self.assertIsNotNone(tab.model)
            # weaker links are listed (All links), never drawn
            self.assertEqual(len(tab.graph.links), len(trusted))
            stats = tab.erd.layout.stats
            self.assertLessEqual(stats["crossings"], TRUSTED_BUDGET)
            self.assertEqual(tab.erd.layout.overlaps(), [])
            self.assertIn("Overview", tab.diagram_note.cget("text"))
        self.run_app(scenario)
