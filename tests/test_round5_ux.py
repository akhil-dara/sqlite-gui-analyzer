"""This round's UX fixes, by introspection: zone names, 'not set' dates, hide empty tables,
full column types, Row History's per-table counts, the diagram's view control, the logo."""

import os
import shutil
import tempfile
import time
import unittest

from tests.helpers import TempDirTest, free_tk


class ZoneAndSentinelTest(unittest.TestCase):
    def test_zone_labels_name_the_places_and_read_back(self):
        from engine import timeline as tl
        self.assertEqual(tl.zone_label(330, local=0), "UTC+05:30 · India, Sri Lanka")
        self.assertEqual(tl.zone_label(330, local=330),
                         "UTC+05:30 · India, Sri Lanka (local time)")
        for m in (330, -480, 345, 0):
            self.assertEqual(tl.parse_offset(tl.zone_label(m)), m)
        self.assertEqual(tl.parse_offset("UTC-08:00"), -480)

    def test_unset_dates_never_read_as_epoch(self):
        from engine import timeline as tl
        for kind in ("unix_s", "unix_ms", "webkit_us", "cocoa_s", "filetime"):
            f = tl.formatter(kind)
            self.assertEqual(f(0), "0 (not set)")
            self.assertEqual(f(-1), "-1 (not set)")
            self.assertNotIn("1970", f(0))
            self.assertNotIn("1601", f(0))
        self.assertTrue(tl.formatter("unix_s")(1700000000).startswith("2023-11-14"))

    def test_datamap_keeps_no_date_for_sentinels(self):
        from engine.datamap import convert_date
        self.assertIsNone(convert_date(0, "webkit_us"))
        self.assertIsNotNone(convert_date(13300000000000000, "webkit_us"))

    def test_custom_format_with_a_lone_surrogate_falls_back(self):
        from datetime import datetime
        from engine import timeline as tl
        self.assertEqual(tl.format_dt(datetime(2026, 1, 2, 3, 4, 5), "custom", chr(0xD800)),
                         "2026-01-02 03:04:05")


class LogoTest(unittest.TestCase):
    def test_every_logo_size_is_bundled(self):
        import appicons
        for s in appicons.SIZES:
            self.assertTrue(os.path.isfile(appicons.logo_path(s)), s)
        self.assertTrue(appicons.logo_path(25).endswith("logo_32.png"))
        self.assertTrue(appicons.logo_path(999).endswith("logo_256.png"))


class Round5AppTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_r5_data_")
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

    def make_db(self):
        import sqlite3
        path = os.path.join(self.tmp, "r5.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE content_annotations (visit_id INTEGER PRIMARY KEY, "
                  "categories VARCHAR(255), visibility_score NUMERIC)")
        c.execute("CREATE TABLE empty_one (a)")
        c.execute("CREATE TABLE empty_two (b)")
        c.executemany("INSERT INTO content_annotations VALUES (?, ?, ?)",
                      [(i, "c%d" % i, i / 3.0) for i in range(1, 30)])
        c.commit()
        c.close()
        return path

    def test_navigator_hides_empty_tables_and_shows_full_types(self):
        app = self.app
        path = self.make_db()
        seen = {}

        def scenario():
            app.state("normal")
            app.geometry("1200x750+-4000+0")
            app._open_db(path, wait=True)
            self.settle(1.5)
            nav = app._navigator
            tree = nav.tree
            db = next(i for i, n in nav._nodes.items() if n[0] == "db")
            m = nav._nodes[db][1]
            for t in ("content_annotations", "empty_one", "empty_two"):
                m.counts[t] = app.db.count(t)
            tree.item(db, open=True)
            for c in tree.get_children(db):
                nav._forget_subtree(c)
                tree.delete(c)
            nav._fill_tables(db, m)
            seen["all"] = [tree.item(c, "text") for c in tree.get_children(db)]
            nav.hide_empty_var.set(True)
            nav._hide_empty_changed()
            seen["hidden"] = [tree.item(c, "text") for c in tree.get_children(db)]
            seen["saved"] = app.tags.settings.get("nav_hide_empty")
            tid = next(i for i, n in nav._nodes.items()
                       if n[0] == "table" and n[2] == "content_annotations")
            tree.item(tid, open=True)
            nav._fill_columns(tid, m, "content_annotations")
            seen["columns"] = [tree.item(c, "text").strip() for c in tree.get_children(tid)]
            hidden_line = next(i for i, n in nav._nodes.items() if n[0] == "empty_hidden")
            nav.hide_empty_var.set(False)
            nav._hide_empty_changed()
            seen["back"] = len([c for c in tree.get_children(db)
                                if nav._nodes.get(c, ("",))[0] == "table"])
            seen["line_gone"] = not tree.exists(hidden_line)

        def go():
            try:
                scenario()
            finally:
                app._close_db(confirm=False)
                app.destroy()
        app.after(100, go)
        app.mainloop()
        self.assertIn("empty_one", seen["all"])
        self.assertNotIn("empty_one", seen["hidden"])
        self.assertIn("2 empty tables hidden — show", seen["hidden"])
        self.assertTrue(seen["saved"])
        self.assertIn("categories   VARCHAR(255)", seen["columns"])
        self.assertIn("visibility_score   NUMERIC", seen["columns"])
        self.assertEqual(seen["back"], 3)
        self.assertTrue(seen["line_gone"])

    def test_timeline_read_as_menu_and_hover_show_samples(self):
        import sqlite3
        app = self.app
        path = os.path.join(self.tmp, "dates.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE msg (id INTEGER PRIMARY KEY, timestamp INTEGER, body TEXT)")
        c.executemany("INSERT INTO msg VALUES (?,?,?)",
                      [(i, 1735689600000 + i * 3600000, "m%d" % i) for i in range(1, 60)])
        c.commit()
        c.close()
        seen = {}

        def scenario():
            app.state("normal")
            app.geometry("1200x750+-4000+0")
            app._open_db(path, wait=True)
            tl_tab = app._timeline
            app._nb.select(tl_tab)
            end = time.time() + 15
            iid = None
            while time.time() < end and iid is None:
                self.settle(0.2)
                for i in tl_tab.col_tree.get_children("") if hasattr(tl_tab, "col_tree") \
                        else ():
                    for k in [i] + list(tl_tab.col_tree.get_children(i)):
                        col = tl_tab._column(k)
                        if col is not None and col.column == "timestamp":
                            iid = k
            seen["iid"] = iid
            if iid is not None:
                menu = tl_tab.column_menu(iid)
                sub = menu.nametowidget(menu.entrycget(menu.index("Read as"), "menu"))
                seen["labels"] = [sub.entrycget(i, "label") for i in range(sub.index("end") + 1)
                                  if sub.type(i) != "separator"]
                seen["hover"] = tl_tab._col_hover(iid)

        def go():
            try:
                scenario()
            finally:
                app._close_db(confirm=False)
                app.destroy()
        app.after(100, go)
        app.mainloop()
        self.assertIsNotNone(seen.get("iid"), "the timestamp column was not listed")
        self.assertTrue(seen["labels"][0].startswith("Sample value: 17356"))
        self.assertTrue(any("Unix milliseconds" in x and "→ 2025-01-01" in x
                            for x in seen["labels"]), seen["labels"])
        self.assertIn("samples:", seen["hover"])

    def test_diagram_view_control_shows_the_chosen_view(self):
        rt = self.app._relations_tab
        self.assertEqual(str(rt.overview_btn.cget("style")), "Segment.TRadiobutton")
        self.assertEqual(rt.view_var.get(), "1")
        rt.view_var.set("all")
        rt._view_chosen()
        self.assertTrue(rt.overview)
        rt.view_var.set("2")
        rt._view_chosen()
        self.assertFalse(rt.overview)
        self.assertEqual(rt.hops_var.get(), 2)
        self.app.destroy()

    def test_logo_in_header_and_window_icon(self):
        self.assertIsNotNone(self.app._logo_img)
        self.assertGreaterEqual(len(self.app._app_icons), 3)
        self.app.destroy()


if __name__ == "__main__":
    unittest.main()
