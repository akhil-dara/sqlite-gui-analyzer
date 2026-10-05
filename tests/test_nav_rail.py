"""The strip at the databases panel's edge: always in view, ◀ hides the panel and ▶ (then at
the window's edge) brings it back; Ctrl+B and ☰ keep it in step. Checked by widget
introspection, the window off the screen."""

import os
import shutil
import tempfile
import time
import unittest

from tests.helpers import TempDirTest, free_tk


class NavRailTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_rail_data_")
        os.environ["SGA_DATA_DIR"] = self.data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(os.environ.pop, "SGA_DATA_DIR", None)
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.tags.warn_with_dialogs = False

    def settle(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.01)

    def test_rail_folds_and_brings_back_the_panel(self):
        app = self.app
        seen = {}

        def scenario():
            app.state("normal")
            app.geometry("1200x750+-4000+0")
            self.settle(0.6)
            rail = app._nav_rail
            seen["open"] = (rail.winfo_ismapped(), rail.cget("text"),
                            app._navigator.winfo_ismapped())
            rail.invoke()                       # ◀: hide
            self.settle()
            seen["hidden"] = (rail.winfo_ismapped(), rail.cget("text"),
                              app._navigator.winfo_ismapped(), rail.winfo_rootx() -
                              app.winfo_rootx())
            rail.invoke()                       # ▶: show again
            self.settle()
            seen["back"] = (rail.cget("text"), app._navigator.winfo_ismapped())
            app._toggle_sidebar_by_hand()       # ☰ / Ctrl+B keep the rail in step
            self.settle()
            seen["by_key"] = rail.cget("text")
            app._toggle_sidebar_by_hand()
            self.settle()
            # narrow: the panel folds by itself (the smallest size follows the display
            # scaling and may be wider than 900 px on this screen: allow 900 here)
            app.minsize(800, 500)
            app.geometry("900x600+-4000+0")
            self.settle(0.6)
            seen["narrow"] = (rail.winfo_ismapped(), rail.cget("text"),
                              app._navigator.winfo_ismapped())

        def go():
            try:
                scenario()
            finally:
                app.destroy()
        app.after(100, go)
        app.mainloop()
        self.assertEqual(seen["open"], (1, "◀", 1))
        mapped, text, nav, x = seen["hidden"]
        self.assertEqual((mapped, text, nav), (1, "▶", 0))
        self.assertLess(x, 10, "the ▶ strip sits at the window's left edge")
        self.assertEqual(seen["back"], ("◀", 1))
        self.assertEqual(seen["by_key"], "▶")
        self.assertEqual(seen["narrow"], (1, "▶", 0))


if __name__ == "__main__":
    unittest.main()
