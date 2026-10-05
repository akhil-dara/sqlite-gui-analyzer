import os
import sqlite3
import unittest

from tests.helpers import TempDirTest, norm, oracle_rows
from tests.fixtures import make_fixtures as fx
from engine.fileformat.btree import BTreeReader, local_payload_size, parse_page_header, cell_pointers
from engine.fileformat.pager import Pager
from engine.fileformat.record import decode_record, read_varint
from engine.fileformat.wal import WalFile
from engine.issues import IssueLog
from engine.schema import SchemaModel


class LocalPayloadTest(unittest.TestCase):
    def test_boundaries_4096(self):
        u = 4096
        x = u - 35
        self.assertEqual(local_payload_size(x, u, True), x)          # fits exactly
        self.assertLess(local_payload_size(x + 1, u, True), x + 1)   # first overflowing size
        m = ((u - 12) * 32 // 255) - 23
        self.assertEqual(local_payload_size(10 ** 6, u, True) >= m, True)
        index_max = ((u - 12) * 64 // 255) - 23
        self.assertEqual(local_payload_size(index_max, u, False), index_max)
        self.assertLess(local_payload_size(index_max + 1, u, False), index_max + 1)


class NativeVsSqliteTest(TempDirTest):
    """Every table the native reader decodes must equal what SQLite returns."""

    def native_table_rows(self, path, name, wal=None):
        issues = IssueLog()
        pager = Pager(path, wal, issues=issues)
        self.addCleanup(pager.close)
        schema = SchemaModel.load(pager, None, issues)
        t = schema.get(name)
        reader = BTreeReader(pager, issues)
        cols = [i for i, c in enumerate(t.columns) if c.hidden != 2]
        out = []
        if t.without_rowid:
            for payload, ref in reader.iter_index(t.root_page):
                row, _ = t.record_to_row(None, decode_record(payload, pager.encoding))
                out.append(tuple(norm(row[i]) for i in cols))
        else:
            for rowid, payload, ref in reader.iter_table(t.root_page):
                row, _ = t.record_to_row(rowid, decode_record(payload, pager.encoding))
                out.append((norm(rowid),) + tuple(norm(row[i]) for i in cols))
        self.assertEqual(len(issues), 0, list(issues))
        return sorted(out), [t.columns[i].name for i in cols], not t.without_rowid, schema

    def check(self, path, name, wal=None):
        native, cols, with_rowid, schema = self.native_table_rows(path, name, wal)
        self.assertEqual(native, oracle_rows(path, name, cols, with_rowid, schema.collations))
        return len(native)

    def test_overflow_all_page_sizes(self):
        for ps in (512, 4096, 16384, 65536):
            p = fx.overflow(self.tmp, ps)
            self.assertEqual(self.check(p, "big"), 6, ps)
            self.assertEqual(self.check(p, "big_wr"), 4, ps)

    def test_without_rowid_trees_with_interior_rows(self):
        p = fx.without_rowid(self.tmp)
        self.assertEqual(self.check(p, "pk_last"), 2000)
        self.assertEqual(self.check(p, "blob_pk"), 500)
        self.assertEqual(self.check(p, "desc_pk"), 800)

    def test_utf16_databases(self):
        for enc in ("UTF-16le", "UTF-16be"):
            self.assertEqual(self.check(fx.encoded(self.tmp, enc), "t"), 300)

    def test_quirk_tables(self):
        p = fx.quirks(self.tmp)
        for name in ("partial_shadow", "wide", "altered", "ipk_desc", "collated", "real_aff"):
            self.check(p, name)

    def test_wal_overlay_matches_sqlite_view(self):
        for builder in (fx.wal_states, fx.wal_only_data):
            p = builder(self.tmp)
            wal = WalFile(p + "-wal")
            self.addCleanup(wal.close)
            name = "t" if builder is fx.wal_states else "activity"
            self.check(p, name, wal)


class TraversalTest(TempDirTest):
    def reader(self, path):
        pager = Pager(path)
        self.addCleanup(pager.close)
        return BTreeReader(pager, IssueLog()), SchemaModel.load(pager)

    def test_segments_count_every_row_without_decoding(self):
        r, schema = self.reader(fx.without_rowid(self.tmp))
        segs = r.segments(schema.get("pk_last").root_page, True)
        self.assertEqual(sum(n for _, _, n in segs), 2000)
        self.assertTrue(any(ci is not None for _, ci, _ in segs), "interior rows expected")

    def test_find_rowid(self):
        r, schema = self.reader(fx.freelist(self.tmp))
        root = schema.get("notes").root_page
        rowid, payload, ref = r.find_rowid(root, 7)
        self.assertEqual(rowid, 7)
        self.assertIsNone(r.find_rowid(root, 999))

    def test_read_cell_round_trip(self):
        r, schema = self.reader(fx.without_rowid(self.tmp))
        root = schema.get("blob_pk").root_page
        payload, ref = next(r.iter_index(root))
        self.assertEqual(r.read_cell(ref.page, ref.offset)[1], payload)

    def test_damaged_root_is_reported_not_raised(self):
        path = fx.corrupt(self.tmp)
        pager = Pager(path)
        self.addCleanup(pager.close)
        issues = IssueLog()
        schema = SchemaModel.load(pager)
        r = BTreeReader(pager, issues)
        self.assertEqual(list(r.iter_table(schema.get("broken").root_page)), [])
        self.assertEqual(issues.items[0].kind, "bad_page")
        self.assertEqual(len(list(r.iter_table(schema.get("ok").root_page))), 50)


class CorruptionIssueTest(TempDirTest):
    """Verify corruption issues are logged and iteration continues."""

    def test_bad_cell_pointer(self):
        """bad_cell_pointer is logged; other rows still yielded."""
        path = os.path.join(self.tmp, "bad_pointer.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(x)")
        for i in range(5):
            conn.execute("INSERT INTO t VALUES(?)", (i,))
        conn.commit()
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='t'").fetchone()[0]
        ps = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.close()

        # Corrupt: set first cell-pointer to 4095 (> usable-4 for 4096-byte pages)
        with open(path, "r+b") as f:
            hdr_off = 100 if root == 1 else 0
            f.seek((root - 1) * ps + hdr_off + 8)
            f.write(b'\x0f\xff')  # 4095 in big-endian

        pager = Pager(path)
        self.addCleanup(pager.close)

        issues = IssueLog()
        reader = BTreeReader(pager, issues)
        rows = list(reader.iter_table(root))
        self.assertTrue(any(i.kind == "bad_cell_pointer" for i in issues.items))
        self.assertGreater(len(rows), 0)  # Other rows still yielded

    def test_payload_clamped(self):
        """payload_clamped is logged; row still decoded."""
        path = os.path.join(self.tmp, "payload_clamped.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES(?)", ('hello',))
        conn.execute("INSERT INTO t VALUES(?)", (123,))
        conn.execute("INSERT INTO t VALUES(?)", (456,))
        conn.commit()
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='t'").fetchone()[0]
        ps = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.close()

        pager = Pager(path)
        self.addCleanup(pager.close)

        # Corrupt: find last cell offset and overwrite payload length to 127
        with open(path, "r+b") as f:
            page_data = bytearray(ps)
            f.seek((root - 1) * ps)
            f.readinto(page_data)

            hdr_off = 100 if root == 1 else 0
            cell_count = (page_data[hdr_off + 3] << 8) | page_data[hdr_off + 4]

            # Get last cell pointer
            ptr_off = hdr_off + 8 + (cell_count - 1) * 2
            last_cell_off = (page_data[ptr_off] << 8) | page_data[ptr_off + 1]

            # Overwrite payload length varint at last_cell_off
            if last_cell_off < ps:
                f.seek((root - 1) * ps + last_cell_off)
                f.write(b'\x7f')  # 127 > page end

        issues = IssueLog()
        reader = BTreeReader(pager, issues)
        rows = list(reader.iter_table(root))
        self.assertTrue(any(i.kind == "payload_clamped" for i in issues.items))
        self.assertGreater(len(rows), 0)  # Row still yielded

    def test_btree_loop(self):
        """btree_loop is logged; iteration completes without hanging."""
        path = os.path.join(self.tmp, "btree_loop.db")

        # Create table with ~2000 rows to force interior page
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(x)")
        for i in range(2000):
            conn.execute("INSERT INTO t VALUES(?)", (i,))
        conn.commit()
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='t'").fetchone()[0]
        ps = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.close()

        pager = Pager(path)
        self.addCleanup(pager.close)

        # Corrupt: set root's right_child to itself
        with open(path, "r+b") as f:
            hdr_off = 100 if root == 1 else 0
            f.seek((root - 1) * ps + hdr_off + 8)
            f.write(root.to_bytes(4, 'big'))

        issues = IssueLog()
        reader = BTreeReader(pager, issues)
        rows = list(reader.iter_table(root))  # Should not hang
        self.assertTrue(any(i.kind == "btree_loop" for i in issues.items))
        self.assertGreater(len(rows), 0)  # Some rows still yielded

    def test_short_page(self):
        """short_page is logged when file is truncated."""
        path = os.path.join(self.tmp, "short.db")

        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES(1)")
        original_ps = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.commit()
        conn.close()

        # Truncate file by 512 bytes (keep header's page count intact)
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.truncate(size - 512)

        pager = Pager(path)
        self.addCleanup(pager.close)

        # Try to read the last page
        issues = IssueLog()
        pager.issues = issues
        last_page = pager.page(pager.page_count)

        self.assertTrue(any(i.kind == "short_page" for i in issues.items))
        self.assertEqual(len(last_page), original_ps)  # Zero-filled

    def test_overflow_truncated(self):
        """overflow_truncated is logged when overflow chain is broken."""
        path = os.path.join(self.tmp, "overflow_truncated.db")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA page_size=1024")
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, s TEXT)")
        conn.execute("INSERT INTO t(s) VALUES(?)", ("x" * 3000,))
        conn.commit()
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='t'").fetchone()[0]
        conn.close()

        ps = 1024

        # Read root page, find cell, and corrupt overflow pointer
        with open(path, "r+b") as f:
            data = bytearray(ps)
            f.seek((root - 1) * ps)
            f.readinto(data)

            h = parse_page_header(data, root)
            ptrs = cell_pointers(data, h, ps)

            if ptrs:
                off = ptrs[0]
                # Parse cell: payload_len (varint), rowid (varint), then payload
                payload_len, p = read_varint(data, off)
                _rowid, p = read_varint(data, p)

                # Calculate local payload size
                local = local_payload_size(payload_len, ps, True)

                # Overflow pointer is at p + local
                overflow_ptr_off = (root - 1) * ps + p + local

                # Overwrite with zero (breaks the overflow chain)
                f.seek(overflow_ptr_off)
                f.write(b'\x00\x00\x00\x00')

        pager = Pager(path)
        self.addCleanup(pager.close)

        issues = IssueLog()
        reader = BTreeReader(pager, issues)
        rows = list(reader.iter_table(root))

        self.assertTrue(any(i.kind == "overflow_truncated" for i in issues.items),
                       f"Expected overflow_truncated, got: {[i.kind for i in issues.items]}")
        self.assertEqual(len(rows), 1)  # Row still yielded despite truncation


if __name__ == "__main__":
    unittest.main()
