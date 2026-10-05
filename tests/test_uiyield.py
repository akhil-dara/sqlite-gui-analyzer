"""Worker threads give way while the Tk thread is busy (engine.uiyield), and the app shows
every tab once at start while its window is still transparent."""

import threading
import time
import unittest

from tests.test_review_ui import AppCase
from tests.helpers import within


class UiYieldTest(unittest.TestCase):
    def setUp(self):
        from engine import uiyield
        self.u = uiyield
        self.addCleanup(uiyield.stop)

    def in_worker(self, fn):
        out = {}

        def run():
            t0 = time.perf_counter()
            fn()
            out["s"] = time.perf_counter() - t0
        th = threading.Thread(target=run)
        th.start()
        th.join(5)
        return out["s"]

    def test_no_user_interface_no_pause(self):
        u = self.u
        u.stop()
        n = u.naps()
        self.assertLess(self.in_worker(lambda: [u.pause() for _ in range(1000)]), 0.2)
        self.assertEqual(u.naps(), n)

    def test_a_busy_tk_thread_is_given_way(self):
        u = self.u
        u.beat()
        time.sleep(u.BUSY_AFTER + 0.01)     # no beat since: the Tk thread is busy
        n = u.naps()
        spent = self.in_worker(u.pause)
        self.assertGreater(u.naps(), n)
        within(self, spent, u.MOST + 0.1)    # never longer than MOST after the last beat
        # beating (serving events): workers go on at once
        u.beat()
        n = u.naps()
        self.assertLess(self.in_worker(u.pause), 0.02)
        self.assertEqual(u.naps(), n)

    def test_a_long_silence_is_waiting_not_drawing(self):
        u = self.u
        u.beat()
        time.sleep(u.MOST + 0.05)
        n = u.naps()
        self.assertLess(self.in_worker(u.pause), 0.02)
        self.assertEqual(u.naps(), n)

    def test_the_tk_thread_itself_never_pauses(self):
        u = self.u
        u.beat()
        time.sleep(u.BUSY_AFTER + 0.01)
        n = u.naps()
        u.pause()
        self.assertEqual(u.naps(), n)


class AppBeatTest(AppCase):
    def test_beats_and_tabs_shown_at_start(self):
        from engine import uiyield

        def scenario():
            app = self.app
            self.pump(lambda: False, 0.2)
            self.assertTrue(uiyield._state["on"])
            self.assertLess(time.monotonic() - uiyield._state["last"], 0.2)
            if app._windowingsystem in ("win32", "aqua"):
                titles = [t.strip() for t in app.tabs_warmed]
                for name in ("Search", "Browse", "SQL", "Forensics", "Timeline", "Tagged",
                             "Relationships", "Overview", "WAL"):
                    self.assertIn(name, titles)
                self.assertEqual(float(app.attributes("-alpha")), 1.0)
            # Overview and WAL are shown only when they apply
            shown = [app._nb.tab(t, "text").strip() for t in app._nb.tabs()]
            self.assertNotIn("Overview", shown)
            self.assertNotIn("WAL", shown)
            self.assertEqual(app._nb.select(), str(app._search_frame))
        self.run_app(scenario)
        from engine import uiyield as u
        self.assertFalse(u._state["on"])            # the app ended: workers never wait
