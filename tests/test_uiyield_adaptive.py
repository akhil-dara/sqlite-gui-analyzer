"""engine.uiyield: the interpreter's switch interval is fine only for a while after the
user's input (active()), and back to the default once that passes (beat()) or when the UI
ends (stop())."""
import sys
import time
import unittest

import tests.helpers  # noqa: F401 - the src path
from engine import uiyield


class AdaptiveIntervalTest(unittest.TestCase):
    def setUp(self):
        self._before = sys.getswitchinterval()
        uiyield._state.update(fast=False, fast_until=0.0, default=None, on=False)

    def tearDown(self):
        uiyield.stop()
        sys.setswitchinterval(self._before)
        uiyield._state.update(fast=False, fast_until=0.0, default=None, on=False)

    def test_fast_after_input_then_default(self):
        uiyield.serve()
        self.assertTrue(uiyield.fast())
        self.assertAlmostEqual(sys.getswitchinterval(), uiyield.FAST, places=6)
        uiyield.active(0.05)            # a short window for the test
        uiyield.beat()
        self.assertTrue(uiyield.fast())  # still within the window
        time.sleep(0.7)                 # serve()'s own window (ACTIVE_SECONDS) passes too
        uiyield.beat()
        self.assertFalse(uiyield.fast())
        self.assertAlmostEqual(sys.getswitchinterval(), self._before, places=6)
        uiyield.active()
        self.assertTrue(uiyield.fast())
        uiyield.stop()
        self.assertFalse(uiyield.fast())
        self.assertAlmostEqual(sys.getswitchinterval(), self._before, places=6)

    def test_window_extends_not_shortens(self):
        uiyield.serve()
        uiyield.active(5.0)
        until = uiyield._state["fast_until"]
        uiyield.active(0.01)
        self.assertEqual(uiyield._state["fast_until"], until)


if __name__ == "__main__":
    unittest.main()
