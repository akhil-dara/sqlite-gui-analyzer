"""Nothing looks busy while nothing runs: at idle no progress bar is shown and no Stop button
is shown enabled, on any tab, with and without a database open, and in the Open Folder
dialog before a folder is chosen. Checked by widget introspection, the windows off the
screen."""

import os
import shutil
import tempfile
import time
import unittest

from tests.helpers import TempDirTest, free_tk
from tests.fixtures import make_fixtures as fx


def walk(w):
    yield w
    for c in w.winfo_children():
        for x in walk(c):
            yield x


def busy_looking(root):
    """[description] of the shown progress bars and the shown, enabled Stop buttons."""
    out = []
    for w in walk(root):
        try:
            if not w.winfo_ismapped():
                continue
            cls = w.winfo_class()
            if cls == "TProgressbar":
                out.append("progress bar %s" % w)
            elif cls in ("TButton", "Button"):
                text = str(w.cget("text"))
                if "Stop" in text:
                    state = str(w.cget("state"))
                    if state != "disabled" and not (cls == "TButton"
                                                    and w.instate(["disabled"])):
                        out.append("Stop button %s (%r)" % (w, text))
        except Exception:               # noqa: BLE001 - a widget destroyed meanwhile
            continue
    return out


class IdleControlsTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_idle_data_")
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

    def settle(self, seconds=0.4):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.01)

    def test_nothing_looks_busy_at_idle(self):
        app = self.app
        path = fx.wal_states(self.tmp)
        found = []

        def tabs(label):
            for tab in app._nb.tabs():
                app._nb.select(tab)
                self.settle(0.3)
                found.extend("%s, %s tab: %s" % (label, app._nb.tab(tab, "text").strip(), b)
                             for b in busy_looking(app))

        def scenario():
            app.state("normal")
            app.geometry("1200x750+-4000+0")
            self.settle(0.6)
            tabs("no database")
            app._open_db(path, wait=True)
            self.settle(2.0)            # counts and the Overview's date scan finish
            tabs("database open")
            from case_ui import OpenFolderDialog
            dlg = OpenFolderDialog(app, auto_choose=False)
            self.settle(0.3)
            found.extend("Open Folder (no folder): %s" % b for b in busy_looking(dlg))
            dlg.destroy()

        def go():
            try:
                scenario()
            finally:
                app._close_db(confirm=False)
                app.destroy()
        app.after(100, go)
        app.mainloop()
        self.assertEqual(sorted(set(found)), [])


if __name__ == "__main__":
    unittest.main()
