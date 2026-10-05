"""Evidence folder aliases (path_inside), other files next to a database, comparing pasted
hashes and what verify_on_close says it checked."""
import hashlib
import os
import subprocess
import time
import unittest
from unittest import mock

from tests.helpers import TempDirTest, dir_snapshot
from engine.evidence import (EvidenceSet, JOURNAL_MAGIC, OTHER_FILES_CAP, parse_hash_list,
                             path_inside)
from engine.tags import inside

WIN = os.name == "nt"


class PathInsideTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.ev = os.path.join(self.tmp, "Evidence Folder")
        os.makedirs(os.path.join(self.ev, "sub"))
        self.other = os.path.join(self.tmp, "exports")
        os.makedirs(self.other)

    def test_plain(self):
        self.assertTrue(path_inside(self.ev, self.ev))
        self.assertTrue(path_inside(os.path.join(self.ev, "sub", "new", "x.csv"), self.ev))
        self.assertTrue(path_inside(os.path.join(self.other, "..", "Evidence Folder", "x"),
                                    self.ev))
        self.assertFalse(path_inside(os.path.join(self.other, "x.csv"), self.ev))
        self.assertFalse(path_inside(self.ev + " 2", self.ev))       # a sibling with the prefix
        self.assertFalse(path_inside(self.tmp, self.ev))
        self.assertFalse(path_inside("", self.ev))
        self.assertFalse(path_inside(os.path.join(self.other, "x"), os.path.join(self.tmp, "nope")))
        self.assertFalse(path_inside("\x00bad", self.ev))
        if WIN:
            self.assertTrue(path_inside(os.path.join(self.ev.upper(), "X.CSV"), self.ev))

    def test_tags_inside_delegates(self):
        self.assertTrue(inside(os.path.join(self.ev, "sub", "x.csv"), self.ev))
        self.assertFalse(inside(os.path.join(self.other, "x.csv"), self.ev))
        self.assertFalse(inside(None, self.ev))

    @unittest.skipUnless(WIN, "Windows junctions")
    def test_junction(self):
        link = os.path.join(self.other, "link")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, self.ev],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0 or not os.path.isdir(link):
            self.skipTest("cannot make a junction")
        try:
            for resolve in (os.path.realpath, os.path.abspath):   # also by file identity alone
                with mock.patch("os.path.realpath", resolve):
                    self.assertTrue(path_inside(os.path.join(link, "new", "x.csv"), self.ev))
                    self.assertTrue(path_inside(link, self.ev))
                    self.assertTrue(path_inside(os.path.join(self.ev, "x"), link))
                    self.assertFalse(path_inside(os.path.join(self.other, "x.csv"), self.ev))
            self.assertTrue(EvidenceSet(self.db()).is_protected(os.path.join(link, "o.csv")))
        finally:
            os.rmdir(link)          # removes the junction only

    def db(self):
        p = os.path.join(self.ev, "x.db")
        with open(p, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\x00" * 100)
        return p

    @unittest.skipUnless(WIN, "Windows path forms")
    def test_extended_and_unc_forms(self):
        ext = "\\\\?\\" + self.ev
        self.assertTrue(path_inside(os.path.join(ext, "sub", "n", "x.csv"), self.ev))
        self.assertTrue(path_inside(os.path.join(self.ev, "x.csv"), ext))
        drive, rest = os.path.splitdrive(self.ev)
        unc = "\\\\localhost\\%s$%s" % (drive[0].lower(), rest)
        if not os.path.isdir(unc):
            self.skipTest("administrative share %s not accessible" % unc)
        for resolve in (os.path.realpath, os.path.abspath):   # also by file identity alone
            with mock.patch("os.path.realpath", resolve):
                self.assertTrue(path_inside(os.path.join(unc, "new", "x.csv"), self.ev))
                self.assertTrue(path_inside(os.path.join(self.ev, "x.csv"), unc))
                self.assertTrue(path_inside(os.path.join(ext, "n", "x.csv"), unc))
                self.assertFalse(path_inside(os.path.join(self.other, "x.csv"), unc))

    @unittest.skipUnless(WIN, "8.3 short names")
    def test_short_name(self):
        import ctypes
        try:
            fn = ctypes.windll.kernel32.GetShortPathNameW
        except AttributeError:
            self.skipTest("no GetShortPathNameW")
        buf = ctypes.create_unicode_buffer(1024)
        if not fn(self.ev, buf, 1024) or buf.value.lower() == self.ev.lower():
            self.skipTest("no 8.3 short name on this volume")
        short = buf.value
        for resolve in (os.path.realpath, os.path.abspath):   # also by file identity alone
            with mock.patch("os.path.realpath", resolve):
                self.assertTrue(path_inside(os.path.join(short, "new", "x.csv"), self.ev))
                self.assertTrue(path_inside(os.path.join(self.ev, "x.csv"), short))
                self.assertFalse(path_inside(os.path.join(self.other, "x.csv"), short))


class OtherFilesTest(TempDirTest):
    def write(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_kinds(self):
        db = self.write("x.db", b"SQLite format 3\x00" + b"\x00" * 100)
        self.write("x.db-wal", b"\x37\x7f\x06\x82" + b"\x00" * 28)
        self.write("x.db-wal.bak", b"\x37\x7f\x06\x83" + b"\x00" * 28)
        self.write("x.db-wal (1)", b"\x37\x7f\x06\x82" + b"\x00" * 28)
        self.write("X.DB-journal.old", JOURNAL_MAGIC + b"\x00" * 20)
        self.write("x.db.bak", b"SQLite format 3\x00" + b"\x00" * 10)
        self.write("x.db-shm~", b"")
        self.write("y.db-wal", b"\x37\x7f\x06\x82")
        os.makedirs(os.path.join(self.tmp, "x.db-folder"))
        before = dir_snapshot(self.tmp)
        ev = EvidenceSet(db)
        got = dict((d["name"], d["kind"]) for d in ev.other_files())
        want = {"x.db-wal.bak": "WAL copy", "x.db-wal (1)": "WAL copy", "x.db.bak":
                "SQLite database copy", "x.db-shm~": "other"}
        want["X.DB-journal.old" if WIN else None] = "journal copy"
        want.pop(None, None)
        self.assertEqual(got, want)
        self.assertEqual(ev.other_files_more, 0)
        d = [x for x in ev.other_files() if x["name"] == "x.db.bak"][0]
        self.assertEqual((d["size"], d["path"]), (26, os.path.join(self.tmp, "x.db.bak")))
        self.assertIn("mtime_ns", d)
        self.assertEqual(sorted(ev.paths), ["main", "wal"])
        self.assertEqual(dir_snapshot(self.tmp), before)

    def test_cap(self):
        db = self.write("x.db", b"")
        for i in range(OTHER_FILES_CAP + 7):
            self.write("x.db.%03d" % i, b"z")
        ev = EvidenceSet(db)
        self.assertEqual(len(ev.other_files()), OTHER_FILES_CAP)
        self.assertEqual(ev.other_files_more, 7)


class CompareExpectedTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.db = os.path.join(self.tmp, "Case.db")
        with open(self.db, "wb") as f:
            f.write(b"main bytes")
        with open(self.db + "-wal", "wb") as f:
            f.write(b"wal bytes")
        self.h_main = hashlib.sha256(b"main bytes").hexdigest()
        self.h_wal = hashlib.sha256(b"wal bytes").hexdigest()
        self.ev = EvidenceSet(self.db)

    def results(self, text):
        return dict((d["role"], d["result"]) for d in self.ev.compare_expected(text))

    def test_not_hashed_yet_then_formats(self):
        self.assertEqual(self.results(self.h_main), {"main": "not hashed yet",
                                                     "wal": "no expected hash given"})
        self.ev.start_hashing()
        self.assertTrue(self.ev.wait_hashing(10))
        self.assertEqual(self.results(self.h_main.upper()),
                         {"main": "match", "wal": "no expected hash given"})
        self.assertEqual(self.results("  %s  \n" % ("0" * 64)),
                         {"main": "MISMATCH", "wal": "no expected hash given"})
        text = "%s  case.DB\n%s *C:\\somewhere\\Case.db-wal\n" % (self.h_main, self.h_wal)
        self.assertEqual(self.results(text), {"main": "match", "wal": "match"})
        bsd = "SHA256 (Case.db) = %s\nSHA256 (./case.db-wal) = %s\n" % (self.h_main.upper(),
                                                                          "1" * 64)
        res = self.ev.compare_expected(bsd)
        self.assertEqual(dict((d["role"], d["result"]) for d in res),
                         {"main": "match", "wal": "MISMATCH"})
        wal = [d for d in res if d["role"] == "wal"][0]
        self.assertEqual((wal["expected"], wal["actual"]), ("1" * 64, self.h_wal))
        self.assertEqual(res.unmatched, [])
        self.assertEqual(res.unparsed, [])

    def test_unmatched_and_unparsed(self):
        text = "\n".join([
            "%s  other.db" % ("a" * 64),
            "%s  Case.db" % ("b" * 63),
            "zz%s  Case.db" % ("c" * 62),
            "SHA256 (Case.db-wal) = xyz",
            "not a hash at all here",
            "%s" % self.h_main, "%s" % ("d" * 64)])
        res = self.ev.compare_expected(text)
        self.assertEqual(res.unmatched, ["other.db"])
        reasons = [r for _, r in res.unparsed]
        self.assertEqual(len(res.unparsed), 5, res.unparsed)
        self.assertIn("63 hex digits (a SHA-256 hash has 64)", reasons)
        self.assertTrue(any("another bare hash" in r for r in reasons))
        main = [d for d in res if d["role"] == "main"][0]
        self.assertEqual(main["expected"], self.h_main)

    def test_parse(self):
        named, bare, unparsed = parse_hash_list("\\%s  a\\\\b.db\n\n" % ("E" * 64))
        self.assertEqual(named, {"b.db": "e" * 64})
        self.assertEqual((bare, unparsed), ([], []))


class VerifyOnCloseTest(TempDirTest):
    def make(self):
        p = os.path.join(self.tmp, "v.db")
        with open(p, "wb") as f:
            f.write(b"v" * 5000)
        return p

    def test_texts(self):
        ev = EvidenceSet(self.make())
        r = ev.verify_on_close(1 << 30)
        self.assertEqual(r.text(), "Evidence unchanged \u2714 (size and mtime (SHA-256 was not "
                                   "computed yet))")
        self.assertFalse(r.rehashed)
        ev.start_hashing()
        self.assertTrue(ev.wait_hashing(10))
        r = ev.verify_on_close(1 << 30)
        self.assertEqual(r.text(), "Evidence unchanged \u2714 (size, mtime and SHA-256)")
        self.assertTrue(r.rehashed)
        r = ev.verify_on_close(1000)
        self.assertEqual(r.text(), "Evidence unchanged \u2714 (size and mtime (SHA-256 not "
                                   "re-computed: files larger than 1,000 bytes, limit "
                                   "verify_rehash_bytes))")
        skipped = ev.verify_on_close(1 << 30, cancel=lambda: True)
        self.assertFalse(skipped.rehashed)
        self.assertIn("SHA-256 re-hash skipped when closing", skipped.text())
        seen = []
        ev.verify(rehash=True, progress=seen.append)
        self.assertEqual(sum(seen), 5000)
        self.assertIn("files larger than 4 KiB", ev.verify_on_close(4096).text())
        self.assertEqual(ev.verify().text(), "Evidence unchanged \u2714 (size and mtime)")

    def test_changed_says_what_was_checked(self):
        p = self.make()
        ev = EvidenceSet(p)
        ev.start_hashing()
        self.assertTrue(ev.wait_hashing(10))
        time.sleep(0.02)
        with open(p, "r+b") as f:
            f.write(b"X")
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, ev.fingerprints["main"].mtime_ns))
        r = ev.verify_on_close(1 << 20)
        self.assertFalse(r.unchanged)
        self.assertIn("SHA-256 changed", r.text())
        self.assertTrue(r.text().endswith("(checked: size, mtime and SHA-256)"))
        r = ev.verify_on_close(10)
        self.assertTrue(r.unchanged)        # same size and mtime: only a re-hash sees it
        self.assertIn("SHA-256 not re-computed", r.text())
