"""The write guard of a case also keeps out of the folder a case was opened from, the other
folders databases were found in, the folder of a database that failed to open, hard links
(another name of an evidence file, or any file with several names), and never reuses an
answer after the protected folders change or for a path that did not exist."""
import os
import sqlite3
import time
from unittest import mock

from tests.helpers import TempDirTest
from tests.fixtures import case_fixtures as cf
from engine import evidence as ev


def _db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(a)")
    c.commit()
    c.close()
    return path


class CaseGuardTest(TempDirTest):
    def case(self):
        from case import Case
        case = Case()
        self.addCleanup(lambda: [case.remove(m) for m in list(case)])
        return case

    def test_root_and_sibling_folders(self):
        root = os.path.join(self.tmp, "image")
        a = _db(os.path.join(root, "app1", "a.db"))
        _db(os.path.join(root, "app2", "b.db"))                 # found, not chosen
        case = self.case()
        case.add(a)
        out_root = os.path.join(root, "report.csv")
        out_sibling = os.path.join(root, "app2", "out.csv")
        self.assertFalse(case.is_protected(out_root))
        case.protect_folder(root)
        self.assertTrue(case.is_protected(out_root))
        self.assertTrue(case.is_protected(out_sibling))
        self.assertIn(root, case.evidence_dirs())
        case.clear_protected_folders()
        self.assertFalse(case.is_protected(out_root))
        self.assertTrue(case.is_protected(os.path.join(root, "app1", "x.csv")))

    def test_member_not_open_still_protects_its_folder(self):
        a = _db(os.path.join(self.tmp, "ev", "a.db"))
        case = self.case()
        m = case.add(a)
        m.db.close()
        self.assertFalse(m.db.ok)
        self.assertTrue(case.is_protected(os.path.join(self.tmp, "ev", "x.csv")))

    def test_hard_links(self):
        os.makedirs(os.path.join(self.tmp, "ev"))
        paths = cf.build(os.path.join(self.tmp, "ev"))
        case = self.case()
        case.add(paths[1])
        elsewhere = os.path.join(self.tmp, "out")
        os.makedirs(elsewhere)
        link = os.path.join(elsewhere, "contacts-copy.db")
        try:
            os.link(paths[1], link)
        except (OSError, AttributeError, NotImplementedError):
            self.skipTest("hard links are not available here")
        self.assertTrue(case.is_protected(link))                # another name of evidence
        self.assertTrue(case.active.db.evidence.is_protected(link))
        other = os.path.join(elsewhere, "notes.txt")
        with open(other, "w") as f:
            f.write("x")
        self.assertFalse(case.is_protected(other))
        os.link(other, os.path.join(elsewhere, "notes2.txt"))
        self.assertTrue(case.is_protected(other))               # several names: refused
        self.assertFalse(case.is_protected(os.path.join(elsewhere, "new.csv")))


class CacheTest(TempDirTest):
    def test_no_safe_answer_kept_for_a_missing_path(self):
        ev.clear_guard_cache()
        folder = os.path.join(self.tmp, "evidence")
        os.makedirs(folder)
        target = os.path.join(self.tmp, "later", "x.csv")
        self.assertFalse(ev.path_inside(target, folder))
        self.assertNotIn((target, folder), ev._INSIDE_CACHE)
        existing = os.path.join(self.tmp, "here.csv")
        open(existing, "w").close()
        self.assertFalse(ev.path_inside(existing, folder))
        self.assertIn((existing, folder), ev._INSIDE_CACHE)       # an existing path may be
        self.assertTrue(ev.path_inside(os.path.join(folder, "y"), folder))
        self.assertIn((os.path.join(folder, "y"), folder), ev._INSIDE_CACHE)
        ev.clear_guard_cache()
        self.assertEqual((ev._INSIDE_CACHE, ev._FOLDER_CACHE), ({}, {}))

    def test_a_change_of_folders_drops_the_answers(self):
        from case import Case
        case = Case()
        self.addCleanup(lambda: [case.remove(m) for m in list(case)])
        a = _db(os.path.join(self.tmp, "ev", "a.db"))
        out = os.path.join(self.tmp, "ev2", "x.csv")
        os.makedirs(os.path.dirname(out))
        open(out, "w").close()
        self.assertFalse(case.is_protected(out))
        with mock.patch.object(time, "monotonic", return_value=time.monotonic()):
            case.add(a)
            case.protect_folder(os.path.join(self.tmp, "ev2"))
            self.assertTrue(case.is_protected(out))             # not the remembered False
