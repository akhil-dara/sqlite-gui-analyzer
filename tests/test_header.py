import os
import sqlite3
import unittest

from tests.helpers import TempDirTest
from engine.fileformat.header import DbHeader, HeaderError


def make_db(path, page_size=4096, encoding=None):
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=%d" % page_size)
    if encoding:
        c.execute("PRAGMA encoding='%s'" % encoding)
    c.execute("CREATE TABLE t(x)")
    c.executemany("INSERT INTO t VALUES (?)", [("row %d" % i,) for i in range(200)])
    c.commit()
    c.close()
    with open(path, "rb") as f:
        return f.read(100)


class HeaderTest(TempDirTest):
    def test_page_sizes_including_65536(self):
        for ps in (512, 4096, 65536):
            p = os.path.join(self.tmp, "ps%d.db" % ps)
            h = DbHeader.parse(make_db(p, ps))
            self.assertEqual(h.page_size, ps)
            self.assertEqual(h.usable_size, ps)
            self.assertEqual(h.page_count(os.path.getsize(p)), os.path.getsize(p) // ps)

    def test_encodings(self):
        for enc, name in (("UTF-16le", "utf-16-le"), ("UTF-16be", "utf-16-be"), (None, "utf-8")):
            p = os.path.join(self.tmp, "enc%s.db" % enc)
            self.assertEqual(DbHeader.parse(make_db(p, encoding=enc)).encoding, name)

    def test_not_sqlite(self):
        with self.assertRaises(HeaderError):
            DbHeader.parse(b"SQLite format 2\x00" + b"\x00" * 84)
        with self.assertRaises(HeaderError):
            DbHeader.parse(b"short")

    def test_bad_page_sizes_and_reserved_space_are_rejected(self):
        raw = bytearray(make_db(os.path.join(self.tmp, "h.db")))
        for size_field in (b"\x00\x00", b"\x01\x00", b"\x03\x00", b"\x10\x01"):  # 0, 256, 768, 4097
            bad = bytearray(raw)
            bad[16:18] = size_field
            with self.assertRaisesRegex(HeaderError, "invalid page size"):
                DbHeader.parse(bytes(bad))
        bad = bytearray(raw)
        bad[16:18], bad[20] = b"\x02\x00", 33          # 512-byte pages, 33 reserved: 479 usable
        with self.assertRaisesRegex(HeaderError, "reserved bytes 33"):
            DbHeader.parse(bytes(bad))
        bad[20] = 32                                    # 480 usable is the minimum allowed
        self.assertEqual(DbHeader.parse(bytes(bad)).usable_size, 480)

    def test_header_fields(self):
        p = os.path.join(self.tmp, "fields.db")
        c = sqlite3.connect(p)
        c.execute("PRAGMA user_version=42")
        c.execute("PRAGMA application_id=1234")
        c.execute("CREATE TABLE t(x)")
        c.execute("PRAGMA journal_mode=WAL")
        c.commit()
        c.close()
        with open(p, "rb") as f:
            h = DbHeader.parse(f.read(100))
        self.assertEqual((h.user_version, h.application_id), (42, 1234))
        self.assertTrue(h.is_wal_mode)
        self.assertEqual(h.schema_format, 4)
        self.assertEqual(h.reserved, 0)
        self.assertGreater(h.sqlite_version, 3000000)

    def test_untrusted_page_count_falls_back_to_file_size(self):
        p = os.path.join(self.tmp, "x.db")
        raw = bytearray(make_db(p))
        raw[92:96] = b"\x00\x00\x00\x00"        # version-valid-for no longer matches
        h = DbHeader.parse(bytes(raw))
        self.assertEqual(h.page_count(8192), 2)


if __name__ == "__main__":
    unittest.main()
