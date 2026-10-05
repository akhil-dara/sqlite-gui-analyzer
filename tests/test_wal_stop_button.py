"""The WAL tab's Stop button is only shown while a comparison runs; an idle Stop
button is misleading (round 4 UX fix)."""
import unittest
from types import SimpleNamespace

from tests.helpers import TempDirTest

try:
    import tkinter as tk
    _tk_error = None
except ImportError as e:        # pragma: no cover - Python built without Tk
    tk, _tk_error = None, e


@unittest.skipIf(tk is None, "no tkinter: %s" % _tk_error)
class WalStopButtonTest(TempDirTest):
    def setUp(self):
        super(WalStopButtonTest, self).setUp()
        from tests.helpers import tk_root
        self.root = tk_root(self)

    def make_tab(self):
        from wal_tab import WalTab
        app = SimpleNamespace(
            db=None,
            _release_worker_connection=lambda: None,
        )
        tab = WalTab(self.root, app)
        tab.pack(fill="both", expand=True)
        self.root.update()
        self.addCleanup(tab.destroy)
        return tab

    def test_stop_hidden_while_idle(self):
        tab = self.make_tab()
        self.assertFalse(tab._rec_rbar.shown(tab._rec_stop_btn),
                         "Stop must not show when no comparison is running")

    def test_stop_hides_again_after_stop(self):
        tab = self.make_tab()
        # simulate a running comparison, then Stop
        tab._rec_rbar.show(tab._rec_stop_btn, True)
        self.root.update()
        self.assertTrue(tab._rec_rbar.shown(tab._rec_stop_btn))
        tab.stop()
        self.assertFalse(tab._rec_rbar.shown(tab._rec_stop_btn),
                         "Stop must hide again once the work is stopped")


if __name__ == "__main__":
    unittest.main()
