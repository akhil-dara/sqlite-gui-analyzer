"""Row version history across main file and WAL frames, rowid reuse, dropped schema
recovery and rollback-journal pre-state."""
import unittest

from tests.helpers import TempDirTest
from tests.fixtures import forensic_fixtures as ff
from engine.forensics.journal import journal_checksum
from engine.schema import Locator
from engine.session import Session


class Base(TempDirTest):
    def open(self, path):
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        return s


class RowHistoryTest(Base):
    def test_versions_changed_columns_and_states(self):
        s = self.open(ff.wal_history(self.tmp))
        h = s.forensics.row_history("acct", Locator("rowid", 5))
        present = [v for v in h.versions if v.present]
        self.assertEqual([v.values[3] for v in present], [500, 555, 555, 777])
        self.assertEqual([v.values[2] for v in present], ["open", "open", "frozen", "open"])
        self.assertEqual(present[0].era, "main")
        self.assertEqual([v.era for v in present[1:]], ["committed"] * 3)
        self.assertEqual([v.frame_state for v in present[1:]], ["superseded"] * 3)
        self.assertEqual([v.commit_group for v in present[1:]], [0, 1, 2])
        self.assertEqual(present[1].changed, ["balance"])
        self.assertEqual(present[2].changed, ["status"])
        self.assertEqual(present[3].changed, ["status", "balance"])
        self.assertTrue(present[3].current)
        self.assertTrue(present[3].copies)            # later frames repeat that version
        self.assertTrue(any(c[2] == "current" for c in present[3].copies))
        self.assertFalse(h.deleted)
        self.assertEqual(h.reuse, [])
        self.assertEqual(h.as_dict()["locator"], "5")

    def test_deleted_row_keeps_its_versions(self):
        s = self.open(ff.wal_history(self.tmp))
        h = s.forensics.row_history("acct", Locator("rowid", 7))
        self.assertTrue(h.deleted)
        self.assertIsNone(h.current)
        self.assertEqual(h.versions[0].values, list(ff.account(7)))
        gone = h.versions[-1]
        self.assertFalse(gone.present)
        self.assertEqual(gone.commit_group, 3)
        self.assertIn("commit 3", gone.note)

    def test_rowid_reuse_is_detected(self):
        s = self.open(ff.wal_history(self.tmp))
        h = s.forensics.row_history("acct", Locator("rowid", 20))
        self.assertFalse(h.deleted)
        self.assertTrue(h.reuse)
        self.assertEqual([v.present for v in h.versions], [True, False, True])
        self.assertEqual(h.versions[-1].values, [20, "someone else", "new", 1])
        self.assertEqual(h.versions[-1].changed, ["owner", "status", "balance"])

    def test_history_summary(self):
        s = self.open(ff.wal_history(self.tmp))
        sm = s.forensics.history_summary("acct")
        self.assertTrue(sm.complete)
        self.assertEqual(sorted(k.locator.value for k in sm.multi_version), [5, 20])
        self.assertEqual([k.locator.value for k in sm.deleted], [7])
        self.assertEqual([k.locator.value for k in sm.reused], [20])
        five = [k for k in sm.keys if k.locator.value == 5][0]
        self.assertEqual(five.versions, 4)
        self.assertEqual((five.first, five.last), ("main", "committed"))
        self.assertIn("multi_version", sm.as_dict())

    def test_row_without_wal_changes_has_one_version(self):
        s = self.open(ff.wal_history(self.tmp))
        h = s.forensics.row_history("acct", Locator("rowid", 1))
        self.assertEqual(len([v for v in h.versions if v.present]), 1)
        self.assertTrue(h.versions[-1].current)

    def test_without_rowid_history(self):
        from tests.fixtures import make_fixtures as fx
        s = self.open(fx.wal_states(self.tmp))
        h = s.forensics.row_history("wr", Locator("pk", ("key1",)))
        self.assertTrue(h.versions)
        self.assertEqual(h.versions[-1].values, ["key1", "committed in wal"])
        self.assertTrue(h.versions[-1].current)


class DroppedSchemaTest(Base):
    def test_dropped_table_statement_and_rows(self):
        s = self.open(ff.dropped_table(self.tmp))
        objs = s.forensics.dropped_schema()
        tables = [o for o in objs if o.type == "table"]
        self.assertEqual([o.name for o in tables], ["secrets"])
        t = tables[0]
        self.assertEqual(t.status, "dropped")
        self.assertIn("CREATE TABLE secrets", t.sql)
        self.assertEqual(t.info.column_names, ["id", "account", "password"])
        self.assertTrue(t.readable, t.root_status)
        rows = set(tuple(r) for _l, r, _f in t.rows())
        everything = set(ff.secret(i) for i in range(1, 151))
        self.assertTrue(rows <= everything)
        self.assertGreaterEqual(len(rows), 100)
        self.assertIn("secrets_account", [o.name for o in objs if o.type == "index"])
        self.assertEqual(t.record.source, "unallocated")
        self.assertTrue(t.as_dict()["readable"])

    def test_dropped_table_rows_are_carved_too(self):
        s = self.open(ff.dropped_table(self.tmp))
        res = s.forensics.carve()
        secrets = set(tuple(r.values) for r in res if r.table == "secrets" and r.index is None)
        self.assertTrue(secrets)
        self.assertTrue(secrets <= set(ff.secret(i) for i in range(1, 151)))
        # the dropped index's entries too: [account, rowid]
        entries = [r for r in res if r.index == "secrets_account"]
        self.assertGreaterEqual(len(entries), 100)
        for r in entries:
            self.assertEqual(r.table, "secrets")
            self.assertEqual(r.columns, ["account", "rowid"])
            self.assertEqual(r.values, [ff.secret(r.rowid)[1], r.rowid])
            self.assertIn("index_entry", r.flags)

    def test_nothing_dropped(self):
        s = self.open(ff.freeblock_rows(self.tmp))
        self.assertEqual(s.forensics.dropped_schema(), [])


class JournalTest(Base):
    def test_hot_journal_parses_and_verifies(self):
        s = self.open(ff.hot_journal(self.tmp))
        j = s.forensics.journal()
        self.assertIsNotNone(j)
        self.assertTrue(j.hot)
        self.assertTrue(j.checksums_ok)
        self.assertEqual(j.page_size, 1024)
        self.assertTrue(j.pages())
        self.assertTrue(all(seg.magic_ok for seg in j.segments))
        self.assertIn("HOT_JOURNAL", [f.code for f in s.forensics.audit()])

    def test_pre_state_rows(self):
        s = self.open(ff.hot_journal(self.tmp))
        rows = s.forensics.journal_rows("t")
        self.assertEqual([tuple(r) for _l, r, _f in rows], [ff.original(i) for i in range(1, 1501)])
        now = [tuple(r.values) for r in s.iter_rows("t")]
        self.assertNotEqual(now, [ff.original(i) for i in range(1, 1501)])   # spilled changes

    def test_checksum_failure_is_reported(self):
        path = ff.hot_journal(self.tmp)
        with open(path + "-journal", "r+b") as f:
            f.seek(512 + 4 + 1024 - 200)             # first record, a byte the checksum covers
            b = f.read(1)
            f.seek(512 + 4 + 1024 - 200)
            f.write(bytes([(b[0] + 1) % 256]))
        s = self.open(path)
        j = s.forensics.journal()
        self.assertFalse(j.checksums_ok)
        self.assertFalse(j.records[0].checksum_ok)
        self.assertIn("JOURNAL_CHECKSUM", [f.code for f in s.forensics.audit()])

    def test_persist_journal_with_zeroed_header(self):
        s = self.open(ff.persist_journal(self.tmp))
        j = s.forensics.journal()
        self.assertIsNotNone(j)
        self.assertFalse(j.hot)
        self.assertTrue(j.header_zeroed)
        self.assertTrue(j.checksums_ok)
        self.assertTrue(j.segments[0].nonce_derived)
        rows = s.forensics.journal_rows("t")
        self.assertEqual([tuple(r) for _l, r, _f in rows], [ff.original(i) for i in range(1, 301)])
        self.assertIn("JOURNAL_RESIDUE", [f.code for f in s.forensics.audit()])

    def test_no_journal(self):
        s = self.open(ff.freeblock_rows(self.tmp))
        self.assertIsNone(s.forensics.journal())
        self.assertEqual(s.forensics.journal_rows("m"), [])

    def test_checksum_function(self):
        page = bytes(range(256)) * 4
        self.assertEqual(journal_checksum(page, 5, 1024),
                         (5 + page[824] + page[624] + page[424] + page[224] + page[24]) & 0xFFFFFFFF)


if __name__ == "__main__":
    unittest.main()
