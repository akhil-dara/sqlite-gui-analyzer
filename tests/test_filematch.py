"""Conditions on the files of a folder (engine.filematch), the scan that leaves the files not
matching unopened, and the Open Folder dialog's conditions, Tick shown only and sorting."""

import os
import sqlite3
import unittest

from tests.helpers import TempDirTest

from engine import filematch
from engine.case import scan_folder
from engine.filematch import Condition, matches, parse_size


def make_db(path, rows=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(x)")
    c.executemany("INSERT INTO t VALUES (?)", [("v%d" % i,) for i in range(rows)])
    c.commit()
    c.close()


class MatchTest(unittest.TestCase):
    def test_text_operators_ignore_case(self):
        name, rel = "places.sqlite", "Alex/Firefox/Profiles/abc.default/places.sqlite"
        ok = lambda f, op, v: matches([Condition(f, op, v)], "all", name, rel)
        self.assertTrue(ok("name", "equals", "PLACES.sqlite"))
        self.assertFalse(ok("name", "not_equals", "places.sqlite"))
        self.assertTrue(ok("path", "contains", "profiles/abc"))
        self.assertTrue(ok("path", "contains", "Profiles\\abc".replace("\\", "/")))
        self.assertTrue(ok("name", "starts", "plac"))
        self.assertTrue(ok("name", "ends", ".SQLITE"))
        self.assertTrue(ok("name", "like", "p*.sqlite"))
        self.assertFalse(ok("name", "like", "p?.sqlite"))
        self.assertTrue(ok("folder", "equals", "abc.default"))
        self.assertTrue(ok("ext", "equals", "sqlite"))
        self.assertTrue(ok("path", "not_contains", "chrome"))

    def test_windows_separators_in_the_path(self):
        self.assertTrue(matches([Condition("path", "contains", "user data/default")], "all",
                                "History", "Bob\\Chrome\\User Data\\Default\\History"))
        self.assertTrue(matches([Condition("folder", "equals", "default")], "all",
                                "History", "Bob\\Chrome\\User Data\\Default\\History"))

    def test_all_and_any(self):
        conds = [Condition("name", "equals", "History"), Condition("name", "equals",
                                                                    "places.sqlite")]
        self.assertTrue(matches(conds, "any", "places.sqlite", "x/places.sqlite"))
        self.assertTrue(matches(conds, "any", "History", "x/History"))
        self.assertFalse(matches(conds, "any", "Cookies", "x/Cookies"))
        self.assertFalse(matches(conds, "all", "History", "x/History"))

    def test_empty_conditions_let_everything_through(self):
        self.assertTrue(matches([], "all", "a.db", "a.db"))
        self.assertTrue(matches([Condition("name", "equals", "  ")], "all", "a.db", "a.db"))
        self.assertTrue(matches([Condition("size", "at_least", "")], "all", "a.db", "a.db"))

    def test_sizes(self):
        self.assertEqual(parse_size("2 MB"), 2 * 1024 ** 2)
        self.assertEqual(parse_size("512kb"), 512 * 1024)
        self.assertEqual(parse_size("1,024"), 1024)
        self.assertIsNone(parse_size("big"))
        big = [Condition("size", "at_least", "1 MB")]
        self.assertTrue(matches(big, "all", "a", "a", 2 * 1024 ** 2))
        self.assertFalse(matches(big, "all", "a", "a", 10))
        asked = []
        self.assertFalse(matches(big, "all", "a", "a", lambda: asked.append(1) or 10))
        self.assertEqual(asked, [1])
        # a size is read only when a size condition is there
        asked = []
        matches([Condition("name", "equals", "a")], "all", "a", "a",
                lambda: asked.append(1) or 10)
        self.assertEqual(asked, [])

    def test_describe(self):
        conds = [Condition("name", "equals", "History"), Condition("name", "equals",
                                                                    "places.sqlite")]
        self.assertEqual(filematch.describe(conds, "any"),
                         "file name is History or file name is places.sqlite")
        self.assertEqual(filematch.describe([], "all"), "")


class ScanTest(TempDirTest):
    def users_folder(self):
        root = os.path.join(self.tmp, "Users")
        for user in ("alex", "sam", "kim"):
            make_db(os.path.join(root, user, "Chrome", "Default", "History"))
            make_db(os.path.join(root, user, "Firefox", "p1.default", "places.sqlite"))
            make_db(os.path.join(root, user, "Chrome", "Default", "Cookies"))
            with open(os.path.join(root, user, "notes.txt"), "w") as f:
                f.write("not a database")
        return root

    def test_the_scan_opens_only_the_files_that_match(self):
        root = self.users_folder()
        conds = [Condition("name", "equals", "History"), Condition("name", "equals",
                                                                    "places.sqlite")]
        opened = []
        import engine.case as case_mod
        real = case_mod.candidate

        def spy(path, folder=None):
            opened.append(os.path.basename(path))
            return real(path, folder)
        case_mod.candidate = spy
        self.addCleanup(setattr, case_mod, "candidate", real)
        stats = {}
        found, seen, problems = scan_folder(
            root, True, accept=lambda n, r, s: matches(conds, "any", n, r, s), stats=stats)
        self.assertEqual(sorted(c.name for c in found), ["History"] * 3 + ["places.sqlite"] * 3)
        self.assertEqual(seen, 6)                       # only those were opened
        self.assertEqual(stats["walked"], 12)           # 9 databases + 3 notes
        self.assertEqual(stats["skipped"], 6)
        self.assertNotIn("Cookies", opened)
        self.assertNotIn("notes.txt", opened)
        self.assertEqual(problems, [])

    def test_without_conditions_every_file_is_looked_at(self):
        root = self.users_folder()
        stats = {}
        found, seen, _p = scan_folder(root, True, stats=stats)
        self.assertEqual(len(found), 9)
        self.assertEqual(seen, 12)
        self.assertEqual(stats["skipped"], 0)


class DialogTest(TempDirTest):
    def setUp(self):
        super().setUp()
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest("no display: %s" % e)
        self.root.geometry("+-4000+0")
        self.addCleanup(self.root.destroy)

    def test_conditions_filter_the_list_and_tick_shown_only(self):
        from case_ui import OpenFolderDialog
        root = ScanTest.users_folder(self)
        d = OpenFolderDialog(self.root, folder=root, auto_choose=False)
        d.geometry("+-4000+0")
        d.wait()
        self.assertEqual(len(d.candidates), 9)
        d.add_condition("name", "equals", "places.sqlite")
        d.add_condition("name", "equals", "History")
        d.mode_var.set("any")
        d._apply_conditions()
        self.assertEqual(sorted(c.name for c in d.shown()), ["History"] * 3 +
                         ["places.sqlite"] * 3)
        self.assertEqual(len(d.tree.get_children()), 6)
        self.assertIn("6 shown where", d.status.cget("text"))
        d.tick_shown_only()
        self.assertEqual(len(d.ticked), 6)
        d.ok()
        self.assertEqual(sorted(os.path.basename(p) for p in d.result),
                         ["History"] * 3 + ["places.sqlite"] * 3)

    def test_scan_only_and_sorting(self):
        from case_ui import OpenFolderDialog
        root = ScanTest.users_folder(self)
        d = OpenFolderDialog(self.root, folder=root, auto_choose=False)
        d.geometry("+-4000+0")
        d.wait()
        d.add_condition("path", "contains", "firefox")
        d._apply_conditions()
        self.assertIn("The list is from a scan with other settings", d.plan.cget("text"))
        d.rescan()                          # conditions given before a scan limit it
        d.wait()
        self.assertEqual([c.name for c in d.candidates], ["places.sqlite"] * 3)
        self.assertIn("other files not opened", d.status.cget("text"))
        self.assertIn("Scanned with these settings", d.plan.cget("text"))
        self.assertEqual(d.scan_btn.cget("text"), "Scan again")
        d.sort_by("rel")
        d.sort_by("rel")                    # descending
        rels = [d.tree.set(i, "rel") for i in d.tree.get_children()]
        self.assertEqual(rels, sorted(rels, key=str.lower, reverse=True))
        self.assertTrue(d.tree.heading("rel", "text").endswith("▼"))
        d.close()


class StepsTest(DialogTest):
    def test_a_chosen_folder_waits_for_scan(self):
        import case_ui
        root = ScanTest.users_folder(self)
        d = case_ui.OpenFolderDialog(self.root, auto_choose=False)
        d.geometry("+-4000+0")
        self.addCleanup(lambda: d.winfo_exists() and d.close())
        self.assertEqual(str(d.scan_btn.cget("state")), "disabled")    # no folder yet
        self.assertIn("Choose a folder first", d.plan.cget("text"))
        real = case_ui.filedialog.askdirectory
        case_ui.filedialog.askdirectory = lambda **k: root
        self.addCleanup(setattr, case_ui.filedialog, "askdirectory", real)
        d.choose_folder()
        d.update()
        self.assertFalse(d.scanning)                    # nothing read until Scan
        self.assertEqual(d.candidates, [])
        self.assertIn("Will look in this folder and its subfolders at every file",
                      d.plan.cget("text"))
        self.assertEqual(d.empty_title.cget("text"), "Ready to scan")
        d.add_condition("name", "equals", "History")
        d._apply_conditions()
        self.assertIn("open only the files where file name is History", d.plan.cget("text"))
        d.recursive_var.set(False)
        d._settings_changed()
        self.assertIn("this folder only (not its subfolders)", d.plan.cget("text"))
        self.assertFalse(d.scanning)
        d.recursive_var.set(True)
        d.rescan()
        d.wait()
        self.assertEqual([c.name for c in d.candidates], ["History"] * 3)

    def test_a_stopped_scan_can_be_scanned_again(self):
        from case_ui import OpenFolderDialog
        import engine.case as case_mod
        root = ScanTest.users_folder(self)
        real = case_mod.candidate
        seen, box = [], {}

        def slow(path, folder=None):
            seen.append(path)
            if len(seen) == 2:
                box["dialog"].stop()        # Stop pressed after the second database
            return real(path, folder)
        d = box["dialog"] = OpenFolderDialog(self.root, auto_choose=False)
        case_mod.candidate = slow
        self.addCleanup(setattr, case_mod, "candidate", real)
        d.folder_var.set(root)
        d.rescan()
        d.geometry("+-4000+0")
        self.addCleanup(lambda: d.winfo_exists() and d.close())
        d.wait()
        self.assertLess(len(d.candidates), 9)
        self.assertIn("Scan stopped after", d.plan.cget("text"))
        self.assertIn("Press Scan again for a complete scan", d.plan.cget("text"))
        self.assertEqual(d.scan_btn.cget("text"), "Scan again")
        self.assertEqual(str(d.scan_btn.cget("state")), "normal")
        case_mod.candidate = real
        d.rescan()
        d.wait()
        self.assertEqual(len(d.candidates), 9)
        self.assertNotIn("stopped", d.plan.cget("text"))


if __name__ == "__main__":
    unittest.main()
