"""The saved-data folder on the welcome page, header tips that never outlive the mouse, and
tag saves that never write on the window's thread."""

import os
import shutil
import tempfile
import unittest

from tests.helpers import TempDirTest, free_tk


class Round6AppTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_r6_data_")
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

    def test_welcome_page_says_where_the_data_is_saved(self):
        app = self.app
        app.update_idletasks()
        self.assertEqual(app._welcome_store.cget("text"), os.path.abspath(self.data))
        # a folder picked for the next start is shown as such, the one in use stays first
        app._data_dir_in_use = os.path.join(self.data, "old")
        app._refresh_welcome_storage()
        text = app._welcome_store.cget("text")
        self.assertTrue(text.startswith(os.path.join(self.data, "old")))
        self.assertIn("from the next start: " + os.path.abspath(self.data), text)

    def test_header_tip_is_not_shown_once_the_mouse_left(self):
        grid = self.app._browse_grid
        self.app.withdraw()
        self.app.update_idletasks()
        grid._show_text_tip("_rid: linked", 10, 10)
        self.assertIsNone(grid._tip)
        # leaving a header hides a tip already shown
        hdr_leave = grid._hdr.bind("<Leave>").strip().splitlines()
        self.assertTrue(any("_tip_hide" in line for line in hdr_leave), hdr_leave)
        import tkinter as tk
        grid._tip = tk.Toplevel(grid)
        grid._tip_hide()
        self.assertIsNone(grid._tip)

    def test_a_small_tag_save_runs_on_a_thread(self):
        tags = self.app.tags
        calls = []

        class Store(object):
            dirty = True
            path = os.path.join(self.data, "t.json")
            evidence_dir = None

            def to_data(self):
                return {"rows": 1}

            def save(self):
                calls.append("ui thread")

            def __len__(self):
                return 1
        tags._save(Store(), background=True)
        self.assertEqual(calls, [])
        self.assertIsNotNone(tags._saver)
        tags._saver.join(5)
        self.assertTrue(os.path.exists(Store.path))

    def settle(self, seconds):
        import time
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.01)

    def chrome_like_db(self):
        import sqlite3
        from tests.decode_samples import pb_bytes, pb_varint
        path = os.path.join(self.tmp, "History")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE downloads (id INTEGER PRIMARY KEY, guid TEXT, start_time "
                  "INTEGER, last_access_time INTEGER, embedder_download_data TEXT, "
                  "mime_type TEXT)")
        emb = (pb_bytes(1, pb_varint(1, 3) + pb_bytes(2, b"x")) + pb_varint(3, 1)).decode()
        rows = [(i, "g%d" % i, 13403000000000000 + i * 10 ** 9,
                 0 if i % 5 else 13403000000000000 + i * 10 ** 9, emb, "image/webp")
                for i in range(1, 60)]
        c.executemany("INSERT INTO downloads VALUES (?,?,?,?,?,?)", rows)
        c.commit()
        c.close()
        return path

    def test_row_window_stays_at_the_top_and_reads_binary_text(self):
        from dialogs import RowWin
        from engine.schema import Locator
        app = self.app
        app.geometry("1200x750+-4000+0")
        app._open_db(self.chrome_like_db(), wait=True)
        self.settle(0.8)
        w = RowWin.show(app, app.db, "downloads", Locator("rowid", 5))
        w.geometry("900x1100+-3000+0")      # taller than the row's lines
        self.settle(0.8)
        cv = w._rw_canvas
        for d in (-120, -120, 120, 120, 120):
            w._scroll_fn(type("E", (), {"delta": d})())
        self.settle(0.2)
        self.assertEqual(cv.canvasy(0), 0.0)              # no gap above the first line
        row_f, name, value = w._col_widgets["embedder_download_data"]
        self.assertEqual(name.get(), "embedder_download_data")   # the name is selectable
        texts = [c.cget("text") for c in value.master.winfo_children()
                 if c.winfo_class() in ("Label", "Button")]
        self.assertTrue(any("protobuf" in t for t in texts), texts)
        self.assertIn("Inspect…", texts)
        self.assertTrue(value.get().startswith("\\x0a\\x05"))   # escaped, no control chars
        # the date reading sits beside a short number
        _r, _n, start = w._col_widgets["start_time"]
        kids = start.master.winfo_children()
        self.assertTrue(any("WebKit" in k.get() for k in kids if k.winfo_class() == "Entry"))
        w.destroy()

    def test_export_blobs_offers_only_the_forms_the_table_holds(self):
        import plistlib
        import sqlite3
        import app as app_module
        from tests.decode_samples import pb_bytes, pb_varint
        path = os.path.join(self.tmp, "blobs.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE pl (id INTEGER PRIMARY KEY, b BLOB)")
        c.execute("CREATE TABLE pb (id INTEGER PRIMARY KEY, b BLOB)")
        c.execute("CREATE TABLE raw (id INTEGER PRIMARY KEY, b BLOB)")
        for i in range(1, 11):
            c.execute("INSERT INTO pl VALUES (?, ?)", (i, plistlib.dumps(
                {"n": i}, fmt=plistlib.FMT_BINARY)))
            c.execute("INSERT INTO pb VALUES (?, ?)", (i, pb_varint(1, i) + pb_bytes(2, b"t")))
            c.execute("INSERT INTO raw VALUES (?, ?)", (i, bytes(range(200, 216))))
        c.commit()
        c.close()
        app = self.app
        app.geometry("1200x750+-4000+0")
        app._open_db(path, wait=True)
        self.settle(0.8)
        seen = {}

        def fake(parent, title, scopes, **kw):
            seen["formats"], seen["note"] = kw.get("formats"), kw.get("note", "")
            return None                         # cancelled: nothing is written
        saved = app_module.export_options
        app_module.export_options = fake
        self.addCleanup(setattr, app_module, "export_options", saved)
        offered = {}
        for table in ("pl", "pb", "raw"):
            app._browse_table_var.set(table)
            app._load_browse_table()
            self.settle(0.6)
            app._browse_export_blobs()
            offered[table] = (seen["formats"], seen["note"])
        self.assertEqual(offered["pl"][0], ("files", "decoded_json", "plist_xml"))
        self.assertIn("10 plists", offered["pl"][1])
        self.assertEqual(offered["pb"][0], ("files", "decoded_json"))
        self.assertIn("No plist", offered["pb"][1])
        self.assertEqual(offered["raw"][0], ("files",))
        self.assertIn("Nothing there decodes", offered["raw"][1])

    def test_grid_cells_show_binary_text_escaped(self):
        from grid import cell_text
        text, kind = cell_text("\n\x05\x08\x03\x12\x01x\x18\x01")
        self.assertEqual(kind, "blob")
        self.assertIn("binary text 9 chars", text)
        self.assertNotRegex(text, "[\x00-\x08]")
        self.assertEqual(cell_text("tab\tand line\nbreaks")[1], "text")

    def test_timeline_samples_skip_unset_and_browse_the_table(self):
        app = self.app
        app.geometry("1200x750+-4000+0")
        app._open_db(self.chrome_like_db(), wait=True)
        self.settle(0.8)
        tab = app._timeline
        tab.start_detect()
        import time
        end = time.time() + 20
        while tab.detection is None and time.time() < end:
            self.settle(0.1)
        col = next(c for c in tab.detection.columns if c.column == "last_access_time")
        samples = tab._read_samples(col, 3)
        # 4 of 5 values are 0: the samples are the set ones, never 0 read as 1601
        self.assertEqual(len(samples), 3)
        self.assertFalse(any("1601" in when or raw == "0" for raw, when in samples), samples)
        # asked for more samples than there are set values: the zeros are said to be unset
        many = tab._read_samples(col, 50)
        self.assertTrue(many[-1][1].startswith("not set"), many[-1])
        self.assertEqual(many[-1][0], "0")
        tab.browse_column(col)
        self.settle(0.3)
        self.assertEqual(app._nb.select(), str(app._browse_frame))
        self.assertEqual(app._browse_table_var.get(), "downloads")


if __name__ == "__main__":
    unittest.main()
