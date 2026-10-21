"""Unset dates left out of the timeline, decoded dates that say UTC, the values two databases
share, Open Folder looking in subfolders, Row History listing deleted rows with the live ones,
and Search saying what it did not look in."""

import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from tests.helpers import TempDirTest, free_tk, tk_root


class UnsetDatesTest(TempDirTest):
    def test_zero_dates_are_left_out_and_counted(self):
        from engine import timeline as tl
        from engine.session import Session
        path = os.path.join(self.tmp, "msgs.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE message (id INTEGER PRIMARY KEY, text TEXT, date INTEGER, "
                  "date_read INTEGER)")
        # Cocoa nanoseconds (2021); date_read 0 = never read
        rows = [(i, "m%d" % i, 633700000000000000 + i * 10 ** 9,
                 0 if i % 2 else 633700000000000000 + i * 2 * 10 ** 9) for i in range(1, 21)]
        c.executemany("INSERT INTO message VALUES (?,?,?,?)", rows)
        c.commit()
        c.close()
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        det = tl.detect(s)
        cols = [col for col in det.columns if col.table == "message"]
        self.assertEqual(sorted(col.column for col in cols if col.kind), ["date", "date_read"])
        for col in cols:
            col.override = "cocoa_ns"   # as on iOS: 0 would read 2001-01-01, inside the window
        res = tl.build_events(s, cols, {"message": ["text"]})
        self.assertTrue(res.events)
        self.assertFalse([e for e in res.events if e.raw == 0])
        self.assertTrue(all(e.when.year == 2021 for e in res.events), res.events[:3])
        self.assertEqual(res.unset, {("message", "date_read"): 10})
        note = tl.unset_note(res.unset)
        self.assertTrue(note.startswith("10 unset date values (0, -1 or empty) left out"), note)

    def test_is_unset(self):
        from engine import timeline as tl
        for v in (0, -1, 0.0, "0", "", "  "):
            self.assertTrue(tl.is_unset(v), v)
        for v in (None, 1, 633700000000000000, "2021-01-01", b"", True):
            self.assertFalse(tl.is_unset(v), v)

    def test_decoded_dates_name_their_zone(self):
        from engine import timeline as tl
        self.assertEqual(tl.utc_text("2021-01-30 13:31:43"), "2021-01-30 13:31:43 UTC")
        self.assertEqual(tl.utc_text("2021-01-30 13:31:43 UTC"), "2021-01-30 13:31:43 UTC")
        self.assertEqual(tl.utc_text("2021-01-30T13:31:43Z"), "2021-01-30T13:31:43Z")
        self.assertEqual(tl.utc_text("2021-01-30 13:31:43+05:30"), "2021-01-30 13:31:43+05:30")
        self.assertEqual(tl.utc_text("0 (not set)"), "0 (not set)")
        self.assertIsNone(tl.utc_text(None))


class TimelineLinkedValuesTest(TempDirTest):
    def test_a_describing_id_shows_what_it_stands_for(self):
        from engine import timeline as tl
        from engine.session import Session
        path = os.path.join(self.tmp, "History")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
        c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
        c.executemany("INSERT INTO urls VALUES (?,?,?)",
                      [(i, "https://site%d.example/" % i, "Site %d" % i) for i in range(1, 21)])
        c.executemany("INSERT INTO visits VALUES (?,?,?)",
                      [(i, 1 + i % 20, 13344473607000000 + i * 10 ** 7) for i in range(1, 41)])
        c.commit()
        c.close()
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        cols = [col for col in tl.detect(s).columns if col.table == "visits" and col.kind]
        links, notes = tl.link_lookups(s, {"visits": ["url"]})
        self.assertEqual(notes, [])
        res = tl.build_events(s, cols, {"visits": ["url"]}, lookups=links)
        self.assertTrue(res.events)
        for e in res.events:
            n = int(e.description.split("=")[1].split(" ")[0])
            self.assertEqual(e.description, "url=%d (https://site%d.example/)" % (n, n))
        # without links the raw value alone, as before
        plain = tl.build_events(s, cols, {"visits": ["url"]})
        self.assertNotIn("(", plain.events[0].description)


class RecoveredDatesTest(TempDirTest):
    def test_a_deleted_value_reads_as_its_columns_date(self):
        from database import DB
        from date_reading import date_reading
        path = os.path.join(self.tmp, "History")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
        c.executemany("INSERT INTO visits VALUES (?,?,?)",
                      [(i, i, 13344473607000000 + i * 10 ** 7) for i in range(1, 30)])
        c.commit()
        c.close()
        db = DB()
        db.open(path)
        self.addCleanup(db.close)
        text = date_reading(db, "visits", "visit_time", 13344473607000000)
        self.assertTrue(text[:4].isdigit(), text)               # the date first
        self.assertIn(" UTC · WebKit", text)                    # then how it was read
        self.assertEqual(date_reading(db, "visits", "visit_time", 0), "")     # not set
        self.assertEqual(date_reading(db, "visits", "url", 13344473607000000), "")
        self.assertEqual(date_reading(db, "visits", "visit_time", "text"), "")


class WalOnlyHistoryTest(TempDirTest):
    def test_a_table_only_in_the_wal_is_said_plainly(self):
        from engine.schema import Locator
        from engine.session import Session
        work = os.path.join(self.tmp, "w")
        os.makedirs(work)
        src, dst = os.path.join(work, "a.db"), os.path.join(self.tmp, "a.db")
        c = sqlite3.connect(src, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE a (x)")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("CREATE TABLE m (id INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO m VALUES (?,?)", [(1, "x"), (2, "y")])
        c.execute("DELETE FROM m WHERE id = 1")
        for sfx in ("", "-wal"):
            shutil.copyfile(src + sfx, dst + sfx)
        c.close()
        s = Session.open(dst, hash_evidence=False)
        self.addCleanup(s.close)
        h = s.forensics.row_history("m", Locator("rowid", 1))
        self.assertEqual(h.notes, ["this table exists only in the WAL; the main file has no "
                                   "copy of it"])
        gone = h.versions[-1]
        self.assertIn("frame %d)" % (gone.frame + 1), gone.note)    # numbered from 1
        self.assertIn("commit %d " % (gone.commit_group + 1), gone.note)


def _contrast(a, b):
    def lum(h):
        h = h.lstrip("#")
        out = []
        for i in (0, 2, 4):
            c = int(h[i:i + 2], 16) / 255.0
            out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class WalStateColoursTest(unittest.TestCase):
    def test_they_follow_the_theme_and_stay_readable(self):
        import constants
        import tokens
        before = tokens.current_theme() if hasattr(tokens, "current_theme") else "light"
        self.addCleanup(lambda: (tokens.set_theme(before), constants.refresh_colors()))
        for theme in ("light", "dark"):
            tokens.set_theme(theme)
            constants.refresh_colors()
            for state, (_label, fg, bg, _m) in constants.WAL_STATES.items():
                fg_token, bg_token = constants._WAL_STATE_TOKENS[state]
                self.assertEqual((fg, bg), (tokens.COLOR[fg_token], tokens.COLOR[bg_token]))
                # the row window's band: heading in the state colour, body in the text colour
                self.assertGreaterEqual(_contrast(fg, bg), 4.5, (theme, state, fg, bg))
                self.assertGreaterEqual(_contrast(tokens.COLOR["text"], bg), 4.5,
                                        (theme, state))


class JournalSearchTest(TempDirTest):
    def test_recovered_records_include_the_journal(self):
        from database import DB
        from tests.fixtures import forensic_fixtures as ff
        db = DB()
        db.open(ff.persist_journal(self.tmp))
        self.addCleanup(db.close)
        hits = list(db.search_freelist("original text of row 7", "Case-Insensitive"))
        self.assertTrue(hits, "the older value kept by the journal is found")
        self.assertIn("journal", set(h["found_in"] for h in hits))
        self.assertEqual(list(db.search_freelist("updated 7", "Case-Insensitive")), [])


class ReadableNamesTest(unittest.TestCase):
    def test_numbers_get_their_parent_and_long_ids_are_shortened(self):
        from engine.case import display_names
        self.assertEqual(display_names(["/x/data/data/com.a/databases/mmssms.db",
                                        "/x/user_de/0/com.a/databases/mmssms.db"],
                                       profiles=False),
                         ["mmssms.db (data)", "mmssms.db (user_de/0)"])
        names = display_names(["/x/app/100708051342911799753/cacheV0.db",
                               "/x/app/200708051342911799999/cacheV0.db"], profiles=False)
        self.assertEqual(names, ["cacheV0.db (1007080…9753)",
                                 "cacheV0.db (2007080…9999)"])
        self.assertEqual(display_names([
            "/x/Dump/data/data/com.ig/databases/direct.db",
            "/x/Dump/data/user/0/com.ig/databases/direct.db",
            "/x/Dump/data_mirror/data_ce/null/0/com.ig/databases/direct.db"], profiles=False),
            ["direct.db (data)", "direct.db (user/0)", "direct.db (data_ce/null/0)"])
        self.assertEqual(len(set(display_names(["/a/1234567890123456789/x.db",
                                                "/b/1234567890123456789/x.db"],
                                               profiles=False))), 2)


class CountErrorTextTest(unittest.TestCase):
    def test_reasons_are_plain(self):
        import sqlite3 as sq
        from database import count_error_text
        self.assertIn("app's own tokenizer (wa_tokenizer)",
                      count_error_text(sq.OperationalError("unknown tokenizer: wa_tokenizer")))
        self.assertIn("app's own module (echo_iterator)",
                      count_error_text(sq.OperationalError("no such module: echo_iterator")))
        self.assertIn("R*Tree", count_error_text(sq.DatabaseError(
            'undersize RTree blobs in "x_node"')))
        self.assertTrue(count_error_text(ValueError("odd")).startswith("SQLite cannot count"))


class CrossMatchesTest(TempDirTest):
    def test_values_found_in_both_databases(self):
        from relations_tab import cross_matches
        a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url_id INTEGER)")
        a.executemany("INSERT INTO visits (url_id) VALUES (?)",
                      [(1,), (1,), (2,), (3,), (9,), (None,)])
        b.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
        b.executemany("INSERT INTO urls VALUES (?,?)", [(1, "a"), (2, "b"), (3, "c"), (4, "d")])
        rows, read, more = cross_matches(a, "visits", "url_id", b, "urls", "id", 100)
        self.assertEqual((read, more), (4, False))          # 1, 2, 3, 9 (NULL left out)
        self.assertEqual(sorted(rows), [(1, 2, 1), (2, 1, 1), (3, 1, 1)])
        rows, read, more = cross_matches(a, "visits", "url_id", b, "urls", "id", 2)
        self.assertEqual((read, more), (2, True))           # the limit is said, never silent


class CrossNoiseTest(TempDirTest):
    def test_a_few_shared_settings_keys_are_not_a_trusted_link(self):
        from engine.crossdb import find_links
        from engine.session import Session
        sessions = []
        for i, name in enumerate(("History", "Favicons")):
            path = os.path.join(self.tmp, name)
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE meta (key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, "
                      "value LONGVARCHAR)")
            c.executemany("INSERT INTO meta VALUES (?,?)", [
                ("version", str(40 + i)), ("last_compatible_version", "16"),
                ("mmap_status", "-1")])
            c.commit()
            c.close()
            s = Session.open(path, hash_evidence=False)
            self.addCleanup(s.close)
            sessions.append((i, s))
        res = find_links(sessions)
        # the same column of the same-named table in two databases is a copy of that
        # table (two browser profiles), never a link - counted apart instead
        meta = [l for l in res.links if l.src_table == "meta" and l.dst_table == "meta"]
        self.assertEqual(meta, [])
        self.assertIn(("meta", "key"), res.same_tables)
        self.assertIn("meta.key in 2 databases", res.same_text())


class OpenFolderTest(TempDirTest):
    def _db(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t (x)")
        c.commit()
        c.close()

    def test_subfolders_are_looked_in_by_default(self):
        from case_ui import OpenFolderDialog
        from tests.helpers import off_screen_windows
        root = tk_root(self)
        off_screen_windows(self)
        for prof in ("Default", "Profile 1"):
            self._db(os.path.join(self.tmp, prof, "History"))
        dlg = OpenFolderDialog(root, self.tmp)
        self.addCleanup(lambda: dlg.winfo_exists() and dlg.destroy())
        dlg.wait()
        self.assertEqual(len(dlg.candidates), 2)
        # without subfolders nothing is found here, and the status says where to look
        dlg.recursive_var.set(False)
        dlg.rescan()
        dlg.wait()
        self.assertEqual(dlg.candidates, [])
        self.assertIn("tick Include subfolders", dlg.status.cget("text"))


class GapsAppTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_r7_data_")
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

    def test_show_all_rows_keeps_the_deleted_rows(self):
        path = os.path.join(self.tmp, "hist.db")
        work = os.path.join(self.tmp, "_w")
        os.makedirs(work)
        wpath = os.path.join(work, "hist.db")
        c = sqlite3.connect(wpath, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE m (id INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO m VALUES (?,?)", [(i, "v%d" % i) for i in range(1, 9)])
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("DELETE FROM m WHERE id IN (1, 2, 3)")
        for sfx in ("", "-wal"):
            shutil.copyfile(wpath + sfx, path + sfx)
        c.close()
        app = self.app
        app.geometry("1300x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 60000
        app._open_db(path, wait=True)
        self.settle(0.3)
        tab = app._forensics
        tab.stop()
        tab.history_table.set("m")
        tab.history_show_all.set(True)
        tab.start_history_summary()
        end = time.time() + 20
        while time.time() < end and len(tab.keys_tree.get_children()) < 8:
            self.settle(0.2)
        keys = [tab.keys_tree.item(i, "values")[1] for i in tab.keys_tree.get_children()]
        self.assertEqual(keys, [str(i) for i in range(1, 9)])
        deleted = [tab.keys_tree.item(i, "values")[3] for i in tab.keys_tree.get_children()]
        self.assertEqual(deleted[:3], ["yes", "yes", "yes"])
        # the list is saved as CSV with its manifest
        from unittest import mock
        out = os.path.join(self.data, "rows.csv")
        with mock.patch("forensics_tab.filedialog.asksaveasfilename", return_value=out):
            self.assertEqual(tab._history_save_list(), out)
        with open(out, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(len(text.strip().splitlines()), 9)         # header + 8 rows
        self.assertTrue(os.path.exists(out + ".manifest.json") or
                        os.path.exists(os.path.splitext(out)[0] + ".csv.manifest.json"))

    def test_a_wal_hit_of_a_deleted_row_says_deleted(self):
        from types import SimpleNamespace
        from engine.schema import Locator
        from search_results import RowGroup
        work = os.path.join(self.tmp, "w")
        os.makedirs(work)
        src, dst = os.path.join(work, "d.db"), os.path.join(self.tmp, "d.db")
        c = sqlite3.connect(src, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.executemany("INSERT INTO urls VALUES (?,?)", [(1, "exfil.example.org"), (2, "kept")])
        c.execute("DELETE FROM urls WHERE id = 1")
        for sfx in ("", "-wal"):
            shutil.copyfile(src + sfx, dst + sfx)
        c.close()
        app = self.app
        app._forensics.AUTO_DELAY_MS = 60000
        app._open_db(dst, wait=True)
        self.settle(0.3)

        def group(rowid):
            g = RowGroup({"table": "urls", "rowid": rowid, "locator": Locator("rowid", rowid),
                          "source": "WAL", "category": "superseded", "frame_idx": 1}, 0)
            g.frames.append((1, 2, "superseded"))
            return g
        self.assertTrue(app.result_source_label(group(1)).startswith("deleted now · WAL version"))
        self.assertFalse(app.result_source_label(group(2)).startswith("deleted"))

    def test_a_destroyed_row_window_leaves_the_pool(self):
        from dialogs import RowWin
        from engine.schema import Locator
        path = os.path.join(self.tmp, "p.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO t VALUES (1, 'x')")
        c.commit()
        c.close()
        app = self.app
        app._forensics.AUTO_DELAY_MS = 60000
        app._open_db(path, wait=True)
        w = RowWin.show(app, app.db, "t", Locator("rowid", 1))
        self.assertIn(w, RowWin._pool.values())
        w.destroy()
        self.assertNotIn(w, RowWin._pool.values())

    def test_search_says_it_covered_live_rows_only(self):
        path = os.path.join(self.tmp, "s.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO t VALUES (1, 'hello')")
        c.commit()
        c.close()
        app = self.app
        app._forensics.AUTO_DELAY_MS = 60000
        app._open_db(path, wait=True)
        self.settle(0.3)
        app._search_free_var.set(False)
        app._search_work = [(app.case.active, ["t"])] if getattr(app.case, "active", None) \
            else []
        if not app._search_work:
            app._search_work = [(m, ["t"]) for m in app._search_members()]
        app._search_places = (False, False)
        text = app._search_skipped_text()
        self.assertTrue(text and text[0].startswith("live rows only: recovered (deleted) records "
                                                    "not searched"), text)
        app._search_places = (False, True)
        self.assertEqual(app._search_skipped_text(), [])


if __name__ == "__main__":
    unittest.main()
