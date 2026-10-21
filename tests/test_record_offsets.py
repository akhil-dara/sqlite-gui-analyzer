"""Every recovered record names the byte offset of its bytes in the file it was read from
(main file, WAL file, rollback journal): the bytes at that offset of that file are the
record's bytes."""
import os
import unittest

from tests.helpers import TempDirTest
from tests.fixtures import forensic_fixtures as ff
from engine.forensics.provenance import JOURNAL_FILE, MAIN_FILE, WAL_FILE, file_offset
from engine.session import Session


class _Base(TempDirTest):
    def check(self, path, want_files):
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        fx = s.forensics
        recs = list(fx.carve())
        self.assertTrue(recs)
        seen = set()
        for r in recs:
            for prov in [r.prov] + list(r.copies):
                if prov.offset is None or not prov.length:
                    continue
                off = fx.file_offset(prov)
                fpath = fx.file_path(prov)
                self.assertIsNotNone(off, prov.where())
                self.assertTrue(fpath and os.path.exists(fpath), prov.where())
                with open(fpath, "rb") as f:
                    f.seek(off)
                    got = f.read(prov.length)
                image = fx.page_image(prov)
                self.assertIsNotNone(image, prov.where())
                self.assertEqual(got, image[prov.offset:prov.offset + prov.length],
                                 "%s: file offset %d of %s" % (prov.where(), off, fpath))
                seen.add(prov.file)
        for f in want_files:
            self.assertIn(f, seen, "no record read from the %s" % f)


class MainAndWalOffsetsTest(_Base):
    def test_main_file_and_wal_records(self):
        self.check(ff.wal_history(self.tmp), (MAIN_FILE, WAL_FILE))


class JournalOffsetsTest(_Base):
    def test_journal_records(self):
        self.check(ff.hot_journal(self.tmp), (JOURNAL_FILE,))


class FileOffsetArithmeticTest(unittest.TestCase):
    def test_arithmetic(self):
        from engine.forensics.provenance import Provenance
        self.assertEqual(file_offset(Provenance("freeblock", MAIN_FILE, 3, 100), 4096),
                         2 * 4096 + 100)
        self.assertEqual(file_offset(Provenance("wal", WAL_FILE, 3, 100, frame=2), 1024),
                         32 + 2 * (24 + 1024) + 24 + 100)
        self.assertIsNone(file_offset(Provenance("wal", WAL_FILE, 3, 100), 1024))
        self.assertIsNone(file_offset(Provenance("freeblock", MAIN_FILE, 3, None), 1024))

        class J(object):
            def image_offset(self, n):
                return 512 + 4 if n == 3 else None
        self.assertEqual(file_offset(Provenance("journal", JOURNAL_FILE, 3, 7), 1024, J()),
                         512 + 4 + 7)
        self.assertIsNone(file_offset(Provenance("journal", JOURNAL_FILE, 4, 7), 1024, J()))
        self.assertIsNone(file_offset(Provenance("journal", JOURNAL_FILE, 3, 7), 1024))


if __name__ == "__main__":
    unittest.main()
