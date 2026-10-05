"""The compatibility layer the Tk UI calls: database.DB and wal_parser.WALParser."""
import os
import sqlite3
import unittest
from unittest import mock

from tests.helpers import TempDirTest
from tests.fixtures import make_fixtures as fx
from engine.backends import SqlBackend
from engine.schema import Locator

from database import DB, as_locator


class DBFacadeTest(TempDirTest):
    def open(self, path, **kw):
        db = DB()
        db.open(path, **kw)
        self.addCleanup(db.close)
        return db

    def test_browse_puts_locator_in_rid_column(self):
        db = self.open(fx.without_rowid(self.tmp))
        cols, rows = db.browse("pk_last", 3, 0)
        self.assertEqual(cols, ["_rid", "a", "b", "c"])
        self.assertIsInstance(rows[0][0], Locator)
        data, dcols = db.full_row("pk_last", rows[0][0])
        self.assertEqual(data["a"], rows[0][1])
        self.assertEqual(dcols, cols)

    def test_view_full_row_uses_the_clicked_row_and_the_view_columns(self):
        db = self.open(fx.views(self.tmp))
        cols, rows = db.browse("vw", 5, 0, "name", "ASC")
        self.assertEqual(cols, ["_rid", "id", "name"])
        db.browse("other", 5, 0)            # the last browsed page now belongs to another table
        data, dcols = db.full_row("vw", rows[0][0])
        self.assertEqual(dcols, ["_rid", "id", "name"])
        self.assertEqual((data["id"], data["name"]), (100, "n001"))

    def test_browse_rows_carry_flags_and_the_page_note(self):
        with mock.patch.object(SqlBackend, "connect",
                               side_effect=sqlite3.DatabaseError("file is not a database")):
            db = self.open(fx.damaged_records(self.tmp))       # NATIVE: rows come with flags
        cols, rows = db.browse("ev", 10, 0)
        self.assertEqual(rows, [[Locator("rowid", 1), 1, None, None]])
        self.assertEqual(rows[0].flags, {"damaged_record"})
        self.assertIs(db.last_page.rows[0].flags, rows[0].flags)
        data, _cols = db.full_row("ev", rows[0][0])              # Row Detail sees them too
        self.assertEqual(data.flags, {"damaged_record"})
        sub = os.path.join(self.tmp, "c")
        os.makedirs(sub)
        db2 = self.open(fx.corrupt(sub))
        _cols, rows = db2.browse("broken", 10, 0)
        self.assertEqual(rows, [])
        self.assertIn("not a b-tree page", db2.last_page.note)    # why the page is empty

    def test_filtered_windows_counts_and_iteration(self):
        from engine.backends import Filter
        db = self.open(fx.without_rowid(self.tmp))
        flt = Filter(col_exprs={"b": ">=1990"}, words=["c0"])
        cols, rows, note = db.browse_window("pk_last", 2, 3, "b", True, flt)
        self.assertEqual((cols, note), (["_rid", "a", "b", "c"], ""))
        self.assertEqual([r[2] for r in rows], [1997, 1996, 1995])
        self.assertIsInstance(rows[0][0], Locator)
        self.assertEqual(db.count_filtered("pk_last", flt), 10)
        every = list(db.iter_filtered("pk_last", flt, "b", True))
        self.assertEqual([r[2] for r in every], list(range(1999, 1989, -1)))
        self.assertEqual(every[2:5], rows)
        self.assertEqual(db.encoding, "utf-8")
        cols, rows, note = db.browse_window("pk_last", 0, 2, "_rid")    # '_rid' = natural order
        self.assertEqual([r[1] for r in rows], ["a0000", "a0097"])

    def test_full_row_accepts_legacy_int_and_digit_string(self):
        db = self.open(fx.freelist(self.tmp))
        self.assertEqual(db.full_row("notes", 3)[0]["id"], 3)
        self.assertEqual(db.full_row("notes", "3")[0]["id"], 3)
        self.assertEqual(as_locator("pk=1"), None)

    def test_search_yields_locator_as_rowid(self):
        db = self.open(fx.without_rowid(self.tmp))
        hits = list(db.search("pk_last", db.columns("pk_last"), "a0042", "Case-Insensitive",
                              10, False, None))
        self.assertTrue(hits)
        self.assertIsInstance(hits[0]["rowid"], Locator)

    def test_counts_and_meta(self):
        db = self.open(fx.freelist(self.tmp))
        self.assertEqual(db.count("notes"), 20)
        self.assertEqual(db.approx_count("notes"), 20)
        m = db.meta()
        self.assertEqual(m["page_size"], 1024)
        self.assertGreater(m["freelist_count"], 0)

    def test_freelist_recovery_shape(self):
        # freed pages are read by the Forensics carver: one confidence scale with reasons
        db = self.open(fx.freelist(self.tmp))
        recs = db.freed_page_records()
        self.assertTrue(recs)
        rec = recs[0]
        self.assertEqual(rec.table, "notes")
        self.assertEqual(rec.prov.source, "freelist")
        self.assertIn(rec.confidence, ("high", "medium", "low"))
        self.assertTrue(rec.reasons)
        self.assertIn("note number", rec.values_dict()["body"])
        trunks, leaves = db.freelist_page_numbers()
        self.assertIn(rec.prov.page, trunks + leaves)
        self.assertIs(db.freed_page_records(), recs)          # read once

    def test_damaged_freelist_record_is_not_filled_with_defaults(self):
        db = self.open(fx.damaged_records(self.tmp))
        recs = [r for r in db.freed_page_records() if r.table == "gone"]
        self.assertTrue(any(r.values[1] == "active" for r in recs))
        # a record the carver could not read whole is never completed with the DEFAULT
        self.assertFalse(any(r.values[1] == "deleted" for r in recs))


class WalAdapterTest(TempDirTest):
    def test_states_summary_and_records(self):
        db = DB()
        db.open(fx.wal_states(self.tmp))
        self.addCleanup(db.close)
        self.assertTrue(db.has_wal)
        s = db.wal.summary()
        for key in ("total_frames", "current", "superseded", "uncommitted", "stale", "commits",
                    "wal_size", "page_size", "unique_pages"):
            self.assertIn(key, s)
        recs = list(db.wal.recover_all_records())
        tables = set(r["table"] for r in recs)
        self.assertIn("t", tables)
        self.assertIn("wr", tables)               # WITHOUT ROWID rows from index-leaf WAL pages
        wr = [r for r in recs if r["table"] == "wr"][0]
        self.assertEqual(wr["locator"], Locator("pk", ("key1",)))
        self.assertEqual(wr["values_dict"]["v"], "committed in wal")
        self.assertTrue(any(f.commit_group is not None for f in db.wal.frames))
        self.assertTrue(any(f.category == "stale" for f in db.wal.frames))

    def test_damaged_wal_record_is_not_filled_with_defaults(self):
        db = DB()
        db.open(fx.damaged_wal_record(self.tmp))
        self.addCleanup(db.close)
        recs = [r for r in db.wal.recover_all_records() if r["table"] == "ev"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["category"], "uncommitted")
        self.assertEqual(recs[0]["raw_values"], [1, None, None])     # no DEFAULT 'deleted'
        self.assertIn("damaged_record", recs[0]["flags"])
        self.assertNotIn("pre_alter", recs[0]["flags"])

    def test_wal_browse_and_search(self):
        db = DB()
        db.open(fx.wal_states(self.tmp))
        self.addCleanup(db.close)
        cols, rows, total = db.wal_browse("t", 10, 0)
        self.assertEqual(cols[0], "_rid")
        self.assertEqual(cols[-1], "_wal_status")
        self.assertGreater(total, 0)
        hits = list(db.wal.search("uncommitted", "Case-Insensitive", limit=5))
        self.assertTrue(hits and all(h["category"] in ("uncommitted", "stale") for h in hits))

    def test_wal_search_returns_each_row_version_once_with_its_frames(self):
        db = DB()
        db.open(fx.wal_states(self.tmp))
        self.addCleanup(db.close)
        hits = list(db.wal.search("gen", "Case-Insensitive"))
        versions = [(h["table"], h["locator"], tuple(h["row"])) for h in hits]
        self.assertEqual(len(versions), len(set(versions)))     # only column 's' can match
        self.assertTrue(any(len(h["frames"]) > 1 for h in hits))
        for h in hits:
            self.assertEqual((h["frame_idx"], h["page_num"], h["category"]), max(h["frames"]))
            self.assertEqual(h["source"], "WAL (%s)" % h["category"].title())
        self.assertEqual(len(list(db.wal.search("gen", "ci", limit=4))), 4)   # mode keys work too

    def test_wal_search_uses_the_table_search_rules(self):
        db = DB()
        db.open(fx.wal_states(self.tmp))
        self.addCleanup(db.close)
        self.assertEqual(list(db.wal.search("FIRST-GEN", "Case-Sensitive")), [])
        self.assertTrue(list(db.wal.search("first-gen", "Case-Sensitive", limit=1)))
        self.assertTrue(list(db.wal.search(r"first-gen \d+ ", "Regex", limit=1)))
        blob_mode = list(db.wal.search("FIRST-GEN", "BLOB/Hex", limit=1))   # text too, any case
        self.assertTrue(blob_mode and "first-gen" in blob_mode[0]["value"])
        # the same rules as the table search: identical results where both see the same rows
        for mode in ("ci", "cs", "sw", "ew", "rx", "blob"):
            wal = set((h["locator"], h["column"], h["value"]) for h in db.wal.search("key1", mode)
                      if h["table"] == "wr")
            tbl = set((h["locator"], h["column"], h["value"]) for h in db.session.search("wr", "key1", mode))
            self.assertEqual(wal, tbl, mode)


if __name__ == "__main__":
    unittest.main()
