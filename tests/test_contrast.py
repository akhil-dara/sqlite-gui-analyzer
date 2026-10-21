"""Every classic Tk text widget of the main window and its row windows is readable in both
themes: text against its background at least 4.5:1."""

import os
import shutil
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest

from tests.helpers import TempDirTest, free_tk

TEXT_CLASSES = ("Label", "Button", "Entry", "Text", "Checkbutton", "Radiobutton", "Message",
                "Listbox")


def _rgb(widget, color):
    try:
        return tuple(x / 65535.0 for x in widget.winfo_rgb(color))
    except tk.TclError:
        return None


def _lum(c):
    out = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _background(w):
    while w is not None:
        try:
            v = w.cget("background")
            if v:
                return v
        except tk.TclError:
            pass
        w = w.master
    return None


def low_contrast(root, where, minimum=4.5):
    """[(ratio, where, class, text)] of the mapped text widgets under root below minimum."""
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if w.winfo_class() not in TEXT_CLASSES:
            continue
        try:
            text = "(field)" if w.winfo_class() in ("Entry", "Text", "Listbox") \
                else w.cget("text")
            fg = w.cget("foreground")
        except tk.TclError:
            continue
        if not text or not w.winfo_ismapped():
            continue
        a, b = _rgb(w, fg), _rgb(w, _background(w))
        if a is None or b is None:
            continue
        r = contrast(a, b)
        if r < minimum:
            out.append((round(r, 2), where, w.winfo_class(), str(text)[:50]))
    return out


class ContrastTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_contrast_")
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
        self.addCleanup(self.app.destroy)

    def settle(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.02)

    def _db(self):
        work = os.path.join(self.tmp, "w")
        os.makedirs(work)
        src, dst = os.path.join(work, "chat.db"), os.path.join(self.tmp, "chat.db")
        c = sqlite3.connect(src, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE msg (id INTEGER PRIMARY KEY, body TEXT, date INTEGER, data BLOB)")
        c.executemany("INSERT INTO msg VALUES (?,?,?,?)",
                      [(i, "message %d" % i, 1700000000000 + i, b"\x0a\x03abc")
                       for i in range(1, 40)])
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("DELETE FROM msg WHERE id IN (3, 4)")
        c.execute("UPDATE msg SET body = 'changed' WHERE id = 5")
        for sfx in ("", "-wal"):
            shutil.copyfile(src + sfx, dst + sfx)
        c.close()
        return dst

    def test_text_is_readable_in_both_themes(self):
        from dialogs import RowWin
        from engine.schema import Locator
        from forensics_tab import RecordWindow
        from wal_tab import open_wal_record
        app = self.app
        app.geometry("1300x850+-4000+0")
        app._forensics.AUTO_DELAY_MS = 300
        app._open_db(self._db(), wait=True)
        end = time.time() + 20
        while time.time() < end and not app._forensics._scanned:
            self.settle(0.2)
        before = __import__("tokens").current_theme()
        self.addCleanup(lambda: app.winfo_exists() and app.set_theme(before, save=False))
        found = []
        for theme in ("light", "dark"):
            app.set_theme(theme, save=False)
            self.settle(0.3)
            for tab in app._nb.tabs():
                app._nb.select(tab)
                self.settle(0.3)
                found += low_contrast(app, "%s: %s tab" % (theme, app._nb.tab(tab, "text").strip()))
            wins = [("row window", RowWin.show(app, app.db, "msg", Locator("rowid", 5)))]
            if app._forensics._records:
                wins.append(("recovered record", RecordWindow(app, app._forensics._records[0],
                                                              app)))
            wal = [r for r in app.db.wal.recover_all_records() if r["table"] == "msg"]
            if wal:
                wins.append(("WAL row", open_wal_record(app, wal[0])))
            self.settle(0.6)
            for name, w in wins:
                if w is not None:
                    found += low_contrast(w, "%s: %s" % (theme, name))
                    w.destroy()
        self.assertEqual(sorted(set(found)), [])


if __name__ == "__main__":
    unittest.main()
