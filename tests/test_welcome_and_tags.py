"""The saved-data folder on the welcome page, header tips that never outlive the mouse, and
tag saves that never write on the window's thread."""

import os
import shutil
import tempfile
import unittest

from tests.helpers import TempDirTest, free_tk


class WelcomeAndTagsAppTest(TempDirTest):
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
        # a message box is recorded, never shown: a modal dialog nobody answers would hold a
        # test (and a CI runner) forever
        import tkinter.messagebox as mb
        saved = (mb.showinfo, mb.showwarning, mb.showerror, mb.askyesno)
        self.shown = []
        mb.showinfo = mb.showwarning = mb.showerror = \
            lambda *a, **k: self.shown.append(a)
        mb.askyesno = lambda *a, **k: True

        def restore():
            mb.showinfo, mb.showwarning, mb.showerror, mb.askyesno = saved
        self.addCleanup(restore)

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

    def test_filetime_reads_as_filetime_in_the_row_window(self):
        import sqlite3
        from dialogs import RowWin
        from engine.schema import Locator
        path = os.path.join(self.tmp, "cache.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE Container (EntryId INTEGER, AccessedTime INTEGER, Url TEXT)")
        base = 134157325254979611                   # 2026-02-16 as FILETIME
        c.executemany("INSERT INTO Container VALUES (?,?,?)",
                      [(i, base + i * 36000000000, "https://e/%d" % i) for i in range(1, 40)])
        c.commit()
        c.close()
        app = self.app
        app.geometry("1200x750+-4000+0")
        app._open_db(path, wait=True)
        self.settle(0.8)
        w = RowWin.show(app, app.db, "Container", Locator("rowid", 3))
        w.geometry("1000x600+-3000+0")
        self.settle(0.8)
        readings = w._date_readings("AccessedTime", base + 3 * 36000000000)
        self.assertEqual(readings[0][0], "filetime")
        self.assertIn("found for the column", readings[0][1])
        self.assertTrue(readings[0][2].startswith("2026-02"))
        self.assertIn("cocoa_ns", [r[0] for r in readings[1:]])
        _r, _n, value = w._col_widgets["AccessedTime"]
        buttons = [k for k in value.master.winfo_children() if k.winfo_class() == "Button"]
        self.assertTrue(any(b.cget("text").startswith("Read as") for b in buttons))
        # picking another reading makes it the column's kind (shared with Browse)
        w._set_column_kind("AccessedTime", "cocoa_ns")
        self.settle(0.5)
        self.assertEqual(app._browse_dates.choice("Container", "AccessedTime"), "cocoa_ns")
        for win in list(RowWin._pool.values()):
            win.destroy()

    def test_browse_row_panel_is_shown_and_reads_the_column_kind(self):
        import sqlite3
        path = os.path.join(self.tmp, "cache2.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE Container (EntryId INTEGER, AccessedTime INTEGER, flags INTEGER)")
        base = 134157325254979611
        c.executemany("INSERT INTO Container VALUES (?,?,?)",
                      [(i, base + i * 36000000000, 805306368) for i in range(1, 40)])
        c.commit()
        c.close()
        app = self.app
        app.geometry("1200x750+-4000+0")
        app._open_db(path, wait=True)
        self.settle(0.8)
        self.assertTrue(app._browse_grid.inspector_visible())        # on by default
        app._browse_table_var.set("Container")
        app._load_browse_table()
        self.settle(1.0)
        first = app._describe_value(base, "AccessedTime")
        self.settle(1.5)                                   # the column is judged on a worker
        text = app._describe_value(base, "AccessedTime")
        self.assertIn("FILETIME", text, (first, text))
        self.assertEqual(app._describe_value(805306368, "flags"), "")    # not a date column
        # turning it off is remembered
        app._browse_inspector_var.set(False)
        app._toggle_browse_row_panel()
        self.assertFalse(app._browse_grid.inspector_visible())
        self.assertIs(app.tags.settings.get("browse_row_panel"), False)

    def test_browse_table_picker_shows_row_counts(self):
        import sqlite3
        path = os.path.join(self.tmp, "counts.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE people (a)")
        c.execute("CREATE TABLE blank (b)")
        c.executemany("INSERT INTO people VALUES (?)", [(i,) for i in range(1234)])
        c.commit()
        c.close()
        app = self.app
        app.geometry("1200x750+-4000+0")
        app._open_db(path, wait=True)
        import time
        end = time.time() + 10
        # the counts are final in the database's count cache; the worker then asks the Tk
        # thread (after(), which needs mainloop) to redraw: done here by hand
        while time.time() < end and app._count_cache != {"people": 1234, "blank": 0}:
            self.settle(0.2)
        app._update_schema_counts()
        counts = app._browse_table_combo.cget("counts")
        self.assertEqual(counts.get("people"), "1,234")
        self.assertEqual(counts.get("blank"), "empty")

    def test_recover_overview_columns_and_record_panel(self):
        import sqlite3
        import time
        path = os.path.join(self.tmp, "deleted.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
        c.execute("CREATE TABLE tags (name TEXT)")
        c.executemany("INSERT INTO notes VALUES (?,?,?)",
                      [(i, "title %d" % i, "body text number %d" % i) for i in range(1, 30)])
        c.execute("DELETE FROM notes WHERE id IN (5, 6, 7)")
        c.commit()
        c.close()
        app = self.app
        app.geometry("1300x800+-4000+0")
        app._open_db(path, wait=True)
        self.settle(0.8)
        tab = app._forensics
        app._nb.select(tab)
        tab.start_carve()
        end = time.time() + 30
        while time.time() < end and not tab._scanned:
            self.settle(0.2)
        self.assertTrue(tab._scanned)
        tree = tab.carve_overview
        row = tree.item("t:notes", "values")
        self.assertEqual(row[0], "notes")
        self.assertGreaterEqual(int(row[2]), 3)            # recovered
        self.assertEqual(tree.get_children()[1], "t:notes")  # tables with records first
        # only notes has records: it is shown at once, in its own columns
        self.settle(0.3)
        self.assertEqual(tree.selection(), ("t:notes",))
        self.assertIn("title", tab.carve_grid.columns())
        # all tables: State column and a Values column
        tree.selection_set("__all__")
        tab._on_overview()
        self.settle(0.3)
        cols = tab.carve_grid.columns()
        self.assertEqual(cols[1], "State")
        self.assertIn("Values", cols)
        # one table: its own columns, each value in its column
        tree.selection_set("t:notes")
        tab._on_overview()
        self.settle(0.3)
        cols = tab.carve_grid.columns()
        self.assertIn("title", cols)
        self.assertIn("body", cols)
        self.assertNotIn("Values", cols)
        rows = tab.carve_grid.fetch_rows(0, tab.carve_grid.row_count() - 1)
        states = set(v[cols.index("State")] for v, _f in rows)
        self.assertEqual(states, {"deleted"})
        bodies = set(v[cols.index("body")] for v, _f in rows)
        self.assertIn("body text number 6", bodies)
        # the record panel: column / value table of the selected record
        tab.carve_grid.set_current_cell(0, 1)
        tab._show_selected_record()
        self.settle(0.2)
        panel = [tab.rec_tree.item(i, "values") for i in tab.rec_tree.get_children()]
        self.assertEqual([p[0] for p in panel], ["id", "title", "body"])
        self.assertIn("deleted", tab.rec_title.cget("text"))
        tab.rec_tree.selection_set(tab.rec_tree.get_children()[2])
        tab._rec_copy_value()
        self.assertTrue(app.clipboard_get().startswith("body text number"))

    def test_recovery_runs_by_itself_after_opening(self):
        import sqlite3
        import time
        from engine import limits
        path = os.path.join(self.tmp, "auto.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO t VALUES (?,?)", [(i, "value %d" % i) for i in range(50)])
        c.execute("DELETE FROM t WHERE id < 10")
        c.commit()
        c.close()
        app = self.app
        tab = app._forensics
        tab.AUTO_DELAY_MS = 200
        app._open_db(path, wait=True)
        end = time.time() + 20
        while time.time() < end and not tab._scanned:
            self.settle(0.2)
        self.assertTrue(tab._scanned, tab.carve_status.cget("text"))
        self.assertTrue(any(r.table == "t" for r in tab._records))
        # the limit at 0: nothing runs on opening
        limits.load({"limits": {"auto_recover_bytes": 0}})
        self.addCleanup(limits.reset)
        app._open_db(path, wait=True)
        self.settle(1.0)
        self.assertFalse(tab._scanned)

    def test_row_history_of_every_table_at_once(self):
        import shutil
        import sqlite3
        import time
        path = os.path.join(self.tmp, "hist.db")
        work = os.path.join(self.tmp, "_w")
        os.makedirs(work)
        wpath = os.path.join(work, "hist.db")
        c = sqlite3.connect(wpath, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, w TEXT)")
        c.executemany("INSERT INTO a VALUES (?,?)", [(i, "a%d" % i) for i in range(1, 6)])
        c.executemany("INSERT INTO b VALUES (?,?)", [(i, "b%d" % i) for i in range(1, 6)])
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("UPDATE a SET v = 'changed' WHERE id = 2")
        c.execute("DELETE FROM b WHERE id = 4")
        for sfx in ("", "-wal"):
            shutil.copyfile(wpath + sfx, path + sfx)
        c.close()
        app = self.app
        app.geometry("1300x800+-4000+0")
        app._open_db(path, wait=True)
        self.settle(0.5)
        tab = app._forensics
        tab.stop()                              # no recovery on opening for this test
        tab.start_history_overview()
        end = time.time() + 20
        while time.time() < end and not str(tab.history_table.get()).startswith("All tables"):
            self.settle(0.2)
        self.assertTrue(tab.history_table.get().startswith("All tables"),
                        tab.history_table.cget("values"))
        end = time.time() + 10
        while time.time() < end and len(tab.keys_tree.get_children()) < 2:
            self.settle(0.2)
        rows = [tab.keys_tree.item(i, "values") for i in tab.keys_tree.get_children()]
        self.assertEqual(sorted(r[0] for r in rows), ["a", "b"])
        # a line of table b opens b's history (All tables stays chosen)
        ib = next(i for i in tab.keys_tree.get_children()
                  if tab.keys_tree.item(i, "values")[0] == "b")
        tab.keys_tree.selection_set(ib)
        tab._on_key()
        end = time.time() + 10
        while time.time() < end and not tab.versions_tree.get_children():
            self.settle(0.2)
        self.assertEqual(tab._history.table, "b")
        self.assertTrue(tab.history_table.get().startswith("All tables"))
        tab._history_copy_all()
        csv_text = app.clipboard_get()
        self.assertTrue(csv_text.startswith("version,state,where,commit,id,w"), csv_text[:80])
        self.assertIn("deleted", csv_text)
        # another database shown next: the lists of the first one are gone, and choosing
        # 'only tables with history' never hands its 'All tables  (N changed)' label on as
        # a table name (it failed as "forensics_failed: 'All tables  (26 changed)'")
        # (in a folder of its own: a file added next to the open evidence is reported, as it
        # should be, as a change of the evidence folder)
        other_dir = tempfile.mkdtemp(prefix="sga_other_")
        self.addCleanup(shutil.rmtree, other_dir, True)
        other = os.path.join(other_dir, "plain.db")
        c = sqlite3.connect(other)
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO t VALUES (1, 'x')")
        c.commit()
        c.close()
        app._open_db(other, wait=True)
        self.settle(0.5)
        tab.stop()
        self.assertEqual(tab._hist_labels, {})
        self.assertEqual(tab._hist_values[1], [])
        tab.history_only_with.set(True)
        tab._hist_list_choice()
        tab.start_history_summary()
        self.settle(1.0)
        failed = [i for i in app.db.session.issues if i.kind == "forensics_failed"]
        self.assertEqual(failed, [])
        self.assertEqual([a for a in self.shown if "Evidence changed" in str(a)], [])
        self.assertFalse(tab.history_table.get().startswith("All tables"))
        # a label with counts that is not in the list is read as what it names
        tab.history_table.set("All tables  (26 changed)")
        self.assertEqual(tab._hist_table(), "__all__")
        tab.history_table.set("t  (3 changed)")
        self.assertEqual(tab._hist_table(), "t")

    def test_browse_shows_a_tables_recovered_records(self):
        import sqlite3
        import time
        path = os.path.join(self.tmp, "browse_rec.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
        c.executemany("INSERT INTO notes VALUES (?,?,?)",
                      [(i, "title %d" % i, "body text number %d" % i) for i in range(1, 30)])
        c.execute("DELETE FROM notes WHERE id IN (5, 6, 7)")
        c.commit()
        c.close()
        app = self.app
        app.geometry("1300x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 100
        app._open_db(path, wait=True)
        app._browse_table_var.set("notes")
        app._load_browse_table()
        end = time.time() + 20
        while time.time() < end and not app._forensics._scanned:
            self.settle(0.2)
        self.settle(0.3)
        top = app._browse_top
        self.assertTrue(top.shown(app._browse_rec_rb))
        self.assertEqual(app._browse_rec_rb.cget("text"), "Recovered (3)")
        app._browse_view_var.set("recovered")
        app._browse_view_changed()
        self.settle(0.3)
        cols = app._browse_grid.columns()
        self.assertEqual(cols[1:4], ["State", "Confidence", "Where"])
        self.assertIn("body", cols)
        rows = app._browse_grid.fetch_rows(0, app._browse_grid.row_count() - 1)
        self.assertEqual(len(rows), 3)
        self.assertIn("recovered records", app._browse_status.cget("text"))
        # back to the live rows
        app._browse_view_var.set("live")
        app._browse_view_changed()
        self.settle(0.5)
        self.assertNotIn("State", app._browse_grid.columns())

    def test_search_finds_rows_deleted_into_freeblocks(self):
        import sqlite3
        path = os.path.join(self.tmp, "search_rec.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
        c.executemany("INSERT INTO notes VALUES (?,?,?)",
                      [(i, "title %d" % i, "body text number %d" % i) for i in range(1, 30)])
        c.execute("DELETE FROM notes WHERE id = 6")
        c.commit()
        c.close()
        app = self.app
        app._forensics.AUTO_DELAY_MS = 60000          # this test searches by itself
        app._open_db(path, wait=True)
        self.settle(0.3)
        self.assertEqual(app.db.freelist_count(), 0)  # no freed pages: freeblocks only
        self.assertTrue(app._search_opts.shown(app._search_free_cb))
        self.assertEqual(app._search_free_cb.cget("text"), "Recovered records")
        hits = list(app.db.search_freelist("body text number 6", "Case-Insensitive"))
        self.assertTrue(hits, "the deleted row is found")
        self.assertTrue(all(h["source"] == "Freelist" for h in hits))
        self.assertIn(hits[0].get("found_in"), ("freeblock", "unallocated"))
        # the live rows are not repeated
        live = list(app.db.search_freelist("body text number 7", "Case-Insensitive"))
        self.assertEqual(live, [])

    def test_one_value_alone_prefers_the_common_recent_reading(self):
        from datetime import datetime
        from engine.decode import timestamps as t
        now = datetime(2026, 10, 18)
        first = lambda v: t.readings(v, now=now)[0][0]
        self.assertEqual(first(134157325254979611), "filetime")     # not Cocoa ns (2005)
        self.assertEqual(first(133963641859924277), "filetime")
        self.assertEqual(first(13435439432979156), "webkit_us")
        self.assertEqual(first(1700000000), "unix_s")
        self.assertEqual(first(1700000000000), "unix_ms")

    def test_timeline_preview_stays_visible_in_a_short_window(self):
        app = self.app
        tab = app._timeline
        app.geometry("1000x560+-4000+0")
        app._nb.select(tab)
        self.settle(0.6)
        self.assertTrue(tab._preview_open)
        self.assertTrue(tab.preview.winfo_ismapped())
        self.assertGreater(tab.preview.winfo_height(), 10)


if __name__ == "__main__":
    unittest.main()
