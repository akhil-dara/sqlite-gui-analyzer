"""Every window the app opens, at 900x600: each button has a label and at least the size it
asks for (none squashed into a thin bar), no list shows an empty first column (Treeview #0)
unless it is a tree, and each list the user scrolls has a search field. Checked by widget
introspection off the screen, never by looking at it."""

import os
import shutil
import tempfile
import time
import tkinter as tk
import traceback
import unittest

from tests.helpers import TempDirTest, free_tk, off_screen_windows
from tests.fixtures import make_fixtures as fx

BUTTONS = ("TButton", "Button")


def walk(w):
    yield w
    for c in w.winfo_children():
        for x in walk(c):
            yield x


def problems_of(win, label):
    """What is wrong in one window: unlabelled or squashed buttons, blank #0 columns."""
    out = []
    for w in walk(win):
        try:
            cls = w.winfo_class()
            if not w.winfo_ismapped():
                continue
            if cls in BUTTONS:
                text = str(w.cget("text")).strip()
                if not text and not str(w.cget("image")):
                    out.append("%s: a button without a label" % label)
                    continue
                if w.winfo_height() + 1 < w.winfo_reqheight():
                    out.append("%s: button %r squashed to %d of %d px high" % (
                        label, text, w.winfo_height(), w.winfo_reqheight()))
                if w.winfo_width() + 2 < w.winfo_reqwidth():
                    out.append("%s: button %r cut to %d of %d px wide" % (
                        label, text, w.winfo_width(), w.winfo_reqwidth()))
            elif cls == "TCombobox":
                out.append("%s: a plain dropdown (not the searchable one)" % label)
            elif cls == "Treeview":
                show = [str(s) for s in w.tk.splitlist(w.cget("show"))]
                if "tree" in show:
                    items = list(w.get_children())
                    used = any(w.item(i, "text") or w.get_children(i) for i in items)
                    if items and not used:
                        out.append("%s: a list with an empty first column" % label)
        except tk.TclError:
            continue
    return out


def has_search(win):
    from widgets import search_boxes_in
    return bool(search_boxes_in(win, visible=False))


class WindowsTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_windows_data_")
        os.environ["SGA_DATA_DIR"] = self.data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(os.environ.pop, "SGA_DATA_DIR", None)
        off_screen_windows(self)
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.tags.warn_with_dialogs = False
        self.errors = []
        self.app.report_callback_exception = lambda e, v, tb: self.errors.append(
            "".join(traceback.format_exception(e, v, tb)))

    def settle(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.01)

    def test_every_window_at_900_by_600(self):
        app = self.app
        path = fx.relations(self.tmp)
        found = []
        searchless = []

        def toplevels():
            """The windows open now (tooltips, borderless, left out)."""
            return [w for w in walk(app) if isinstance(w, tk.Toplevel) and w.winfo_exists()
                    and not w.wm_overrideredirect()]

        def opened(before):
            return [w for w in toplevels() if w not in before]

        def check(label, open_fn, busy=None, search=True):
            before = toplevels()
            open_fn()
            self.settle(0.3)
            if busy is not None:
                deadline = time.time() + 30
                while busy() and time.time() < deadline:
                    self.settle(0.05)
            new = opened(before)
            if not new:
                found.append("%s: no window opened" % label)
                return
            from widgets import scaling_factor
            f = scaling_factor(app)         # 900x600 at the display's scaling
            for win in new:
                win.geometry("%dx%d" % (900 * f, 600 * f))
                self.settle(0.4)
                found.extend(problems_of(win, label))
                if search and not has_search(win):
                    searchless.append(label)
                try:
                    close = getattr(win, "close", None)
                    (close or win.destroy)()
                except tk.TclError:
                    pass
            self.settle(0.1)

        def scenario():
            from dialogs import HelpDialog, RowWin, ScopeDlg
            from engine.schema import Locator
            app._open_db(path, wait=True)
            self.settle(1.0)
            rw = app.relations
            deadline = time.time() + 60
            while rw.mapper.busy() and time.time() < deadline:
                self.settle(0.05)
            holder = {}

            def keep(name, fn):
                def run():
                    holder[name] = fn()
                return run
            check("Column relationships", keep("cm", lambda: rw.column_map(
                "message_poll", "message_row_id")), lambda: holder["cm"].busy())
            check("Related rows", keep("rr", lambda: rw.related("message", "_id", 5)),
                  lambda: holder["rr"].busy())
            check("Find everywhere", keep("fw", lambda: rw.find(5)),
                  lambda: holder["fw"].busy())
            check("Help", lambda: HelpDialog(app))
            check("Row detail", lambda: RowWin.show(app, app.db, "message",
                                                    Locator("rowid", 1)))
            check("Search scope", lambda: ScopeDlg(app, app.db.tables(), {}, [], []))
            check("Database Map", lambda: app.datamap.export_map(), search=False)
            check("Copy with related", lambda: app.datamap.copy_with_related(
                "message", [Locator("rowid", 1)]), search=False)
            check("Limits", lambda: app.datamap.limits_window())
            check("Issues", app._show_issues)
            check("Activity log", app._show_activity)
            check("Evidence", app._show_evidence)
            app._nb.select(app._browse_frame)
            app._browse_table_var.set("message")
            app._load_browse_table()
            self.settle(0.5)
            check("Column chooser", lambda: app._browse_grid.column_chooser())
            check("Manage tags", lambda: app._tags_tab.manage_tags())
            from case_ui import OpenFolderDialog
            check("Open folder", lambda: OpenFolderDialog(app, None, auto_choose=False))
            # every tab: its lists have the one search field; Browse's filters say how
            # they work
            for tab in app._nb.tabs():
                frame = app.nametowidget(tab)
                title = app._nb.tab(tab, "text").strip()
                if title in ("Search", "Browse"):
                    continue            # the search itself; Browse: its filters (below)
                if not has_search(frame):
                    searchless.append("tab " + title)
            self.assertEqual(app._browse_filter_entry.placeholder.full_text(),
                             "words, all must match")
            self.assertEqual(app._browse_filter_help.cget("text"), "?")
            before = toplevels()
            app._browse_filter_help.invoke()
            self.settle(0.2)
            pop = opened(before)
            self.assertEqual(len(pop), 1)
            text = " ".join(str(w.cget("text")) for w in walk(pop[0])
                            if w.winfo_class() == "Label")
            for part in (">5", "5~10", "/regex/", "NULL", "!text", "a%b_c"):
                self.assertIn(part, text)
            pop[0].destroy()

        def go():
            try:
                scenario()
            except Exception:           # noqa: BLE001 - reported below
                self.errors.append(traceback.format_exc())
            finally:
                app._close_db(confirm=False)
                app.destroy()
        app.after(100, go)
        app.mainloop()
        self.assertEqual(self.errors, [])
        self.assertEqual(sorted(set(found)), [])
        self.assertEqual(sorted(set(searchless)), [])


if __name__ == "__main__":
    unittest.main()
