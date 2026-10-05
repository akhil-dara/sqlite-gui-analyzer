import unittest

from tests.helpers import TempDirTest
from tests.fixtures import make_fixtures as fx
from engine.schema import Locator

from database import DB
from search_results import ResultGrouper, group_hits, match_label, source_kind


def db_hit(table, rid, column, row, value="v"):
    return {"table": table, "locator": Locator("rowid", rid), "rowid": Locator("rowid", rid),
            "column": column, "value": value, "type": "TEXT", "row": row, "source": "DB"}


def wal_hit(table, rid, column, row, frames):
    newest = max(frames)
    return {"table": table, "locator": Locator("rowid", rid), "rowid": rid, "column": column,
            "value": "v", "type": "TEXT", "row": row, "frames": frames,
            "frame_idx": newest[0], "page_num": newest[1], "category": newest[2],
            "source": "WAL (%s)" % newest[2].title()}


class GroupingTest(unittest.TestCase):
    def test_cells_of_one_row_are_one_group(self):
        row = [1, "hello a", "hello b"]
        groups = group_hits([db_hit("t", 1, "a", row), db_hit("t", 1, "b", row),
                             db_hit("t", 2, "a", [2, "hello", None]), db_hit("u", 1, "x", [1, "hi"])])
        self.assertEqual([(g.table, len(g.hits), g.columns) for g in groups],
                         [("t", 2, ["a", "b"]), ("t", 1, ["a"]), ("u", 1, ["x"])])
        self.assertEqual(groups[0].source_label(), "DB")

    def test_wal_copy_identical_to_the_db_row_folds_into_it(self):
        row = [1, "hello", 2.0]
        frames = [(3, 7, "superseded"), (9, 7, "current")]
        groups = group_hits([db_hit("t", 1, "a", row),
                             wal_hit("t", 1, "a", [1, "hello", 2], frames)])   # 2 == 2.0
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].frames, frames)
        self.assertEqual(groups[0].source_label(), "DB + WAL ×2")
        self.assertEqual(len(groups[0].hits), 1)

    def test_a_changed_wal_version_is_its_own_group(self):
        groups = group_hits([db_hit("t", 1, "a", [1, "hello now"]),
                             wal_hit("t", 1, "a", [1, "hello before"], [(4, 7, "stale"), (2, 7, "stale")]),
                             wal_hit("t", 5, "a", [5, "hello gone"], [(6, 8, "superseded")])])
        self.assertEqual([g.source for g in groups], ["DB", "WAL", "WAL"])
        self.assertEqual(groups[1].source_label(), "WAL stale ×2")
        self.assertEqual(groups[1].category, "stale")
        self.assertEqual(groups[1].frames_label(), "#2 stale, #4 stale")
        self.assertEqual(groups[2].source_label(), "WAL superseded")

    def test_value_types_must_match_to_fold(self):
        groups = group_hits([db_hit("t", 1, "a", [1, "1"]), wal_hit("t", 1, "a", [1, 1], [(1, 2, "current")])])
        self.assertEqual(len(groups), 2)

    def test_multi_column_wal_version_folds_its_frames_once(self):
        row = [1, "hello", "hello"]
        frames = [(1, 2, "current")]
        g = ResultGrouper()
        for h in (db_hit("t", 1, "a", row), db_hit("t", 1, "b", row),
                  wal_hit("t", 1, "a", row, frames), wal_hit("t", 1, "b", row, frames)):
            g.add(h)
        self.assertEqual(len(g.groups), 1)
        self.assertEqual(g.groups[0].frames, frames)

    def test_column_name_hits_stay_separate(self):
        hit = {"table": "t", "column": "name", "locator": None, "rowid": "-", "value": "name",
               "type": "column_name"}
        self.assertEqual(len(group_hits([hit, dict(hit, column="name2")])), 2)

    def test_freelist_records_are_their_own_lines(self):
        def free(page, off, column):
            return {"table": "t", "locator": Locator("ordinal", 0, snapshot=(["a"], ["x"])),
                    "rowid": 7, "column": column, "value": "x", "type": "TEXT", "row": ["x"],
                    "source": "Freelist", "page": page, "cell_offset": off, "confidence": "high"}
        groups = group_hits([db_hit("t", 7, "a", [7, "x"]), free(5, 100, "a"), free(5, 100, "b"),
                             free(5, 300, "a"), free(9, 100, "a")])
        self.assertEqual([(g.source, len(g.hits)) for g in groups],
                         [("DB", 1), ("Freelist", 2), ("Freelist", 1), ("Freelist", 1)])
        self.assertEqual(groups[1].source_label(), "Freelist p.5")
        self.assertEqual(source_kind(free(1, 1, "a")), "Freelist")

    def test_match_label_says_how_a_blob_matched(self):
        hit = db_hit("t", 1, "a", [1])
        self.assertEqual(match_label(hit), "TEXT")
        self.assertEqual(match_label(dict(hit, type="BLOB", encoding="utf-16le", offset=30)),
                         "BLOB utf-16le @30")
        self.assertEqual(match_label(dict(hit, type="BLOB", encoding="decoded", offset=None)),
                         "BLOB decoded")

    def test_columns_label_shortens_long_lists(self):
        row = [1] + ["x"] * 5
        groups = group_hits([db_hit("t", 1, c, row) for c in "abcde"])
        self.assertEqual(groups[0].columns_label(), "a, b, c +2")


class GroupingOnRealSearchTest(TempDirTest):
    def test_wal_rows_already_in_the_database_are_not_repeated(self):
        db = DB()
        db.open(fx.wal_states(self.tmp))
        self.addCleanup(db.close)
        hits = []
        for t in db.tables():
            hits.extend(dict(h, source="DB") for h in db.search(t, db.columns(t), "after restart",
                                                                "Case-Insensitive", 500, False, None))
        db_rows = len(hits)
        hits.extend(db.wal.search("after restart", "Case-Insensitive"))
        groups = group_hits(hits)
        self.assertEqual(sum(1 for g in groups if g.source == "DB"), db_rows)
        folded = [g for g in groups if g.source == "DB" and g.frames]
        self.assertTrue(folded)                           # committed WAL copies = the DB rows
        for g in groups:
            if g.source == "WAL":                          # what stays is a different version
                self.assertFalse(any(d.source == "DB" and d.table == g.table and d.locator == g.locator
                                     and d.row == g.row for d in groups))


if __name__ == "__main__":
    unittest.main()
