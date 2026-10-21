"""At start the window comes to the front (Windows: a brief topmost, taken off at once, and
the focus); the window never stays on top of other programs."""
import sys
import unittest

from tests.test_review_ui import AppCase


class ForegroundTest(AppCase):
    def test_window_raised_then_not_topmost(self):
        def scenario():
            app = self.app
            self.pump(lambda: False, 0.5)
            self.assertEqual(int(app.attributes("-topmost")), 0)
            self.assertEqual(float(app.attributes("-alpha")), 1.0)
            if sys.platform == "win32":
                self.assertIsNotNone(app.focus_displayof())
            app._raise_window()             # harmless to call again
            self.pump(lambda: False, 0.4)
            self.assertEqual(int(app.attributes("-topmost")), 0)
        self.run_app(scenario)


if __name__ == "__main__":
    unittest.main()
