"""The WAL tab is always there: a database without a -wal file gets a plain statement of
that, with the file name expected, the journal mode and how a WAL is read; the facts at the
top of the Bytes window are copyable text."""
import os
import sqlite3
import tkinter as tk

from tests.test_review_ui import AppCase


def make(tmp):
    path = os.path.join(tmp, "plain.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t VALUES (1, 'a')")
    con.commit()
    con.close()
    return path


class WalAbsentTest(AppCase):
    def test_tab_present_and_says_why(self):
        path = make(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            self.pump(lambda: False, 0.3)
            texts = [app._nb.tab(t, "text") for t in app._nb.tabs()]
            self.assertIn("WAL", texts)
            self.assertEqual(texts.index("WAL"), texts.index("Browse") + 1)
            app._nb.select(app._wal_frame)
            self.pump(lambda: False, 0.3)
            text = app._wal_frame.problem.cget("text")
            self.assertIn("plain.db has no write-ahead log", text)
            self.assertIn(path + "-wal", text)
            self.assertIn("journal mode rollback", text)
            self.assertIn("checksum chain", text)
            self.assertTrue(app._wal_frame.problem.winfo_manager())
            self.assertFalse(app._wal_frame.bar_row.winfo_manager())
        self.run_app(scenario)

    def test_info_block_is_copyable(self):
        def scenario():
            from recordui import info_block
            app = self.app
            t = info_block(app, "page 13 of x\nbyte 49,696", "#ffffff", "#000000", "TkDefaultFont")
            t.pack()
            self.pump(lambda: False, 0.2)
            self.assertIsInstance(t, tk.Text)
            self.assertEqual(t.get("1.0", "end-1c"), "page 13 of x\nbyte 49,696")
            self.assertEqual(str(t.cget("state")), "disabled")
            app.clipboard_clear()
            t.copy_selection()              # nothing selected: all of it
            self.assertEqual(app.clipboard_get(), "page 13 of x\nbyte 49,696")
            t.tag_add("sel", "2.0", "end-1c")
            t.copy_selection()
            self.assertEqual(app.clipboard_get(), "byte 49,696")
            t.copy_all()
            self.assertEqual(app.clipboard_get(), "page 13 of x\nbyte 49,696")
            t.destroy()
        self.run_app(scenario)
