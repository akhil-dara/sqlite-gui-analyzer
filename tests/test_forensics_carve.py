"""Carving deleted records: freeblocks, unallocated space, freelist pages, WAL frames,
overflow chains, WITHOUT ROWID tables; live rows excluded; confidence and provenance."""
import os
import unittest

from tests.helpers import TempDirTest, dir_snapshot
from tests.fixtures import forensic_fixtures as ff
from tests.fixtures import make_fixtures as fx
from engine.forensics import CARVE_SOURCES, Record
from engine.forensics.cellparse import parse_intact, reconstruct, varint
from engine.forensics.provenance import (FREEBLOCK, FREELIST, HIGH, LOW, MEDIUM, REPLACED, WAL,
                                         value_key)
from engine.forensics.templates import Template
from engine.schema import TableInfo, describe_table
from engine.session import Session


class ForensicsTestBase(TempDirTest):
    def open(self, path):
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        return s

    @staticmethod
    def of(records, table):
        return [r for r in records if r.table == table]

    @staticmethod
    def live(s, table):
        return set(tuple(value_key(v) for v in row.values) for row in s.iter_rows(table))


class CarveDeletedRowsTest(ForensicsTestBase):
    def test_deleted_rows_are_carved_with_exact_values(self):
        s = self.open(ff.deleted_rows(self.tmp))
        res = s.forensics.carve()
        self.assertTrue(res.complete)
        people = self.of(res, "people")
        got = set(tuple(r.values[1:]) for r in people)
        deleted = ff.PEOPLE_SINGLE + ff.PEOPLE_RUN
        for i in deleted:
            self.assertIn(ff.person(i)[1:], got, "person %d not recovered" % i)
        wanted = set(ff.person(i)[1:] for i in deleted)
        for r in people:
            self.assertIn(tuple(r.values[1:]), wanted)
            if r.rowid is not None:
                self.assertEqual(tuple(r.values), ff.person(r.rowid))
        notes = self.of(res, "notes")
        expected = dict((ff.note(i)[1], i) for i in ff.NOTES_DELETED)
        exact = set(r.values[1] for r in notes if r.values[1] in expected)
        self.assertGreaterEqual(len(exact), 0.9 * len(expected))
        for r in notes:
            if r.values[1] in expected:
                self.assertIn(r.rowid, (None, expected[r.values[1]]))
            else:
                self.assertEqual(r.confidence, LOW)          # only damaged copies differ
        self.assertTrue(any(r.source == FREELIST and r.confidence == HIGH for r in notes))

    def test_live_rows_are_never_reported(self):
        s = self.open(ff.deleted_rows(self.tmp))
        res = s.forensics.carve()
        self.assertGreater(res.stats["live_skipped"], 0)
        for table in ("people", "notes"):
            live = self.live(s, table)
            for r in self.of(res, table):
                self.assertNotIn(tuple(value_key(v) for v in r.values), live)

    def test_secure_delete_leaves_nothing(self):
        s = self.open(ff.deleted_rows(self.tmp, secure_delete=True))
        res = s.forensics.carve()
        self.assertEqual(self.of(res, "people") + self.of(res, "notes"), [])
        codes = set(f.code for f in s.forensics.audit())
        self.assertIn("FREELIST_ZEROED", codes)
        self.assertIn("SECURE_DELETE_LIKELY", codes)

    def test_records_carry_provenance_and_a_stable_id(self):
        path = ff.deleted_rows(self.tmp)
        s = self.open(path)
        res = s.forensics.carve()
        for r in res:
            self.assertIsInstance(r, Record)
            self.assertIn(r.source, CARVE_SOURCES)
            self.assertIn(r.confidence, (HIGH, MEDIUM, LOW))
            self.assertTrue(r.reasons)
            self.assertEqual(r.prov.file, "main")
            self.assertGreaterEqual(r.prov.page, 1)
            self.assertEqual(len(r.id), 16)
            d = r.as_dict()
            self.assertEqual(d["provenance"]["page"], r.prov.page)
        ids = [r.id for r in res]
        again = Session.open(path, hash_evidence=False)
        try:
            self.assertEqual([r.id for r in again.forensics.carve()], ids)
        finally:
            again.close()

    def test_sources_limit_what_is_carved(self):
        s = self.open(ff.deleted_rows(self.tmp))
        only = s.forensics.carve(sources=(FREELIST,))
        self.assertTrue(only)
        self.assertEqual(set(r.source for r in only), {FREELIST})
        self.assertEqual(self.of(only, "people"), [])

    def test_table_filter(self):
        s = self.open(ff.deleted_rows(self.tmp))
        res = s.forensics.carve(tables=["people"])
        self.assertTrue(res)
        self.assertEqual(set(r.table for r in res), {"people"})

    def test_cancel_and_progress(self):
        s = self.open(ff.deleted_rows(self.tmp))
        seen = []
        res = s.forensics.carve(cancel=lambda: True, progress=lambda d, t: seen.append((d, t)))
        self.assertFalse(res.complete)
        self.assertEqual(res.stats["stopped"], "cancelled")
        full = s.forensics.carve(progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(full.complete)
        self.assertEqual(seen[-1][0], seen[-1][1])


class FreeblockTest(ForensicsTestBase):
    def test_freeblock_leading_bytes_are_rebuilt(self):
        s = self.open(ff.freeblock_rows(self.tmp))
        res = s.forensics.carve(sources=(FREEBLOCK,))
        m = self.of(res, "m")
        self.assertEqual(sorted(tuple(r.values[1:]) for r in m),
                         sorted(ff.line(i)[1:] for i in ff.LINES_DELETED))
        for r in m:
            self.assertEqual(r.source, FREEBLOCK)
            self.assertEqual(r.confidence, MEDIUM)
            self.assertIsNone(r.rowid)                   # its bytes were overwritten
            self.assertIn("rowid_unknown", r.flags)
            self.assertTrue(any("rebuilt" in x for x in r.reasons))

    def test_lost_first_column_is_solved_from_the_size(self):
        s = self.open(ff.freeblock_rows(self.tmp))
        res = s.forensics.carve(sources=(FREEBLOCK,))
        got = sorted(tuple(r.values) for r in self.of(res, "plain"))
        self.assertEqual(got, sorted(ff.plain(i) for i in ff.PLAIN_DELETED))

    def test_all_sources_merge_copies_of_one_row(self):
        s = self.open(ff.freeblock_rows(self.tmp))
        res = s.forensics.carve()
        m = self.of(res, "m")
        self.assertEqual(sorted(tuple(r.values[1:]) for r in m),
                         sorted(ff.line(i)[1:] for i in ff.LINES_DELETED))
        merged = [r for r in m if r.copies]
        self.assertTrue(merged)
        for r in merged:                   # an intact copy gives the rowid back
            if r.rowid is not None:
                self.assertEqual(tuple(r.values), ff.line(r.rowid))

    def test_reconstruct_unit(self):
        """A table-leaf cell with its first 4 bytes zeroed is rebuilt exactly."""
        t = TableInfo("m", "table", 2, "CREATE TABLE m(id INTEGER PRIMARY KEY, a TEXT, b INT)")
        describe_table(t, set())
        tpl = Template.for_table(t)
        body = b"hello" + bytes([42])
        record = bytes([4, 0, 2 * 5 + 13, 1]) + body            # header len 4: NULL, text5, int8
        cell = bytes([len(record), 7]) + record
        page = bytearray(64)
        page[10:10 + len(cell)] = cell
        self.assertIsNotNone(parse_intact(page, 10, 10 + len(cell), 1024, False))
        page[10:14] = b"\x00\x00\x00\x0c"                       # freeblock header over it
        cells = reconstruct(page, 10, 10 + len(cell), 1024, False, tpl)
        self.assertTrue(cells)
        self.assertEqual(cells[0].types, [0, 23, 1])
        self.assertEqual(bytes(page[cells[0].body:cells[0].end]), body)
        self.assertEqual(varint(bytes([0x81, 0x00]), 0, 2), (128, 2))
        self.assertIsNone(varint(bytes([0x80, 0x01]), 0, 2))    # not minimal


class WithoutRowidTest(ForensicsTestBase):
    def test_without_rowid_deletions(self):
        s = self.open(ff.without_rowid_deleted(self.tmp))
        res = s.forensics.carve()
        items = self.of(res, "items")
        got = set(tuple(r.values) for r in items)
        for i in ff.ITEMS_SINGLE:
            self.assertIn(ff.item(i), got)
        run = set(ff.item(i) for i in ff.ITEMS_RUN)
        self.assertGreaterEqual(len(got & run), len(run) // 2)
        genuine = run | set(ff.item(i) for i in ff.ITEMS_SINGLE)
        self.assertTrue(got <= genuine, got - genuine)
        self.assertTrue(any(r.source == FREEBLOCK for r in items))
        for r in items:
            self.assertIsNone(r.rowid)
            self.assertNotIn("rowid_unknown", r.flags)


class OverflowTest(ForensicsTestBase):
    def test_deleted_rows_follow_their_overflow_chain(self):
        s = self.open(ff.overflow_deleted(self.tmp))
        res = s.forensics.carve()
        big = dict((r.values[1], r) for r in self.of(res, "big"))
        for i in ff.BIG_DELETED:
            r = big["title %02d" % i]
            self.assertEqual(tuple(r.values), ff.big_row(i))
            self.assertTrue(r.prov.overflow)
            self.assertNotIn("overflow_partial", r.flags)
            self.assertNotEqual(r.confidence, LOW)

    def test_reused_overflow_pages_give_a_partial_low_record(self):
        s = self.open(ff.overflow_deleted(self.tmp, reuse=True))
        res = s.forensics.carve()
        big = dict((r.values[1], r) for r in self.of(res, "big"))
        for i in ff.BIG_DELETED:
            r = big["title %02d" % i]
            self.assertEqual(r.confidence, LOW)
            self.assertIn("overflow_partial", r.flags)
            want = ff.big_row(i)
            self.assertEqual(r.values[:2], list(want[:2]))
            self.assertTrue(r.values[2] is None or want[2].startswith(r.values[2]))


class WalCarveTest(ForensicsTestBase):
    def test_rows_deleted_in_the_wal_come_from_older_page_copies(self):
        s = self.open(ff.wal_history(self.tmp))
        res = s.forensics.carve()
        acct = self.of(res, "acct")
        seven = [r for r in acct if r.rowid == 7]
        self.assertEqual(len(seven), 1)
        self.assertEqual(tuple(seven[0].values), ff.account(7))
        self.assertIn(seven[0].source, (REPLACED, WAL))
        self.assertTrue(any(c.file == "wal" for c in seven[0].copies))
        older = [r for r in acct if r.rowid == 5]
        self.assertEqual(sorted(r.values[3] for r in older), [500, 555, 555])
        for r in older:
            self.assertIn("prior_version", r.flags)
        self.assertTrue(any(r.prov.frame_state == "superseded" for r in older))

    def test_wal_states_fixture_carves_without_error(self):
        s = self.open(fx.wal_states(self.tmp))
        res = s.forensics.carve()
        self.assertTrue(res.complete)
        self.assertEqual(res.stats["errors"], 0)
        self.assertTrue(any(r.prov.frame_state in ("stale", "uncommitted") for r in res
                            if r.prov.file == "wal"))


class EvidenceSafetyTest(ForensicsTestBase):
    def test_forensics_writes_nothing_near_the_evidence(self):
        work = os.path.join(self.tmp, "ev")
        os.makedirs(work)
        path = ff.wal_history(work)
        before = dir_snapshot(work)
        s = Session.open(path, hash_evidence=False)
        try:
            fx_ = s.forensics
            fx_.carve()
            fx_.dropped_schema()
            fx_.audit()
            fx_.history_summary("acct")
        finally:
            report = s.close()
        self.assertTrue(report.unchanged, report.text())
        self.assertEqual(dir_snapshot(work), before)


if __name__ == "__main__":
    unittest.main()
