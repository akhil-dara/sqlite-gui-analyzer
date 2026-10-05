"""Find one value everywhere (engine.value_search): typed matching, whole vs contained, the
source cell left out, recovered WAL versions and freed-page records."""

import unittest

from tests.test_session import SessionTestBase, open_native
from tests.fixtures import make_fixtures as fx
from database import DB
from engine.schema import Locator
from engine.value_search import ValueMatcher, find_everywhere, is_common, number_texts


def hits_of(session, value, contains=False, **kw):
    out = []
    for _name, hits, err in find_everywhere(session, value, contains, **kw):
        assert err is None, err
        out.extend(hits)
    return out


def cells(hits):
    return sorted((h.source, h.table, h.column, h.locator.value if h.locator is not None
                   else None, h.kind) for h in hits)


class MatcherTest(unittest.TestCase):
    def test_numbers(self):
        m = ValueMatcher(5)
        self.assertTrue(m.cell_hit(5) and m.cell_hit(5.0) and m.cell_hit("5") and m.cell_hit(b"5"))
        self.assertIsNone(m.cell_hit("05") or m.cell_hit(55) or m.cell_hit("x5x"))
        self.assertEqual(number_texts(2.5), ["2.5"])
        self.assertIn("5", number_texts(5.0))

    def test_text_and_bytes(self):
        m = ValueMatcher("token-123", contains=True)
        self.assertEqual(m.cell_hit("token-123")[2], "whole: text")
        self.assertEqual(m.cell_hit("a token-123 b")[2], "contained: text")
        self.assertTrue(m.cell_hit(b"xx" + "token-123".encode("utf-16-le"))[2]
                        .startswith("contained: utf16le"))
        b = ValueMatcher(b"\x01\x02", contains=True)
        self.assertEqual(b.cell_hit(b"\x01\x02")[2], "whole: bytes")
        self.assertEqual(b.cell_hit(b"\x00\x01\x02")[2], "contained: bytes")
        self.assertIsNone(ValueMatcher("abc").cell_hit("xabcx"))    # whole only

    def test_common_values(self):
        for v in (0, 1, 7, -3, 2.5, "1", "t", "true", "No", b"\x01"):
            self.assertTrue(is_common(v), v)
        for v in (123456, "token-123", b"\x00\x01token"):
            self.assertFalse(is_common(v), v)


class FindEverywhereTest(SessionTestBase):
    def open_db(self, path, native=False):
        if native:
            return open_native(self, path)
        db = DB()
        db.open(path)
        self.addCleanup(db.close)
        return db

    def test_number_matches_integer_text_and_blob_columns(self):
        db = self.open_db(fx.values_everywhere(self.tmp))
        got = cells(hits_of(db.session, 5, origin=("a", "n", Locator("rowid", 1))))
        self.assertEqual(got, [("DB", "a", "b", 2, "whole"), ("DB", "a", "t", 2, "whole")])
        # without an origin the source cell is found too
        self.assertIn(("DB", "a", "n", 1, "whole"), cells(hits_of(db.session, 5)))
        # the text '5' finds the number 5 as well
        self.assertIn(("DB", "a", "n", 1, "whole"), cells(hits_of(db.session, "5")))

    def test_blob_whole_contained_and_as_text(self):
        db = self.open_db(fx.values_everywhere(self.tmp))
        whole = cells(hits_of(db.session, b"token-123", origin=("a", "b", Locator("rowid", 3))))
        self.assertEqual(whole, [("DB", "c", "code", 1, "whole")])      # text, same bytes
        inside = cells(hits_of(db.session, b"token-123", True,
                               origin=("a", "b", Locator("rowid", 3))))
        self.assertEqual(inside, [("DB", "a", "b", 1, "contained"),
                                  ("DB", "c", "code", 1, "whole"),
                                  ("DB", "c", "data", 1, "contained"),
                                  ("DB", "c", "data", 2, "contained")])
        equal = cells(hits_of(db.session, b"\x00\x01token-123\x02",
                              origin=("a", "b", Locator("rowid", 1))))
        self.assertEqual(equal, [("DB", "c", "data", 2, "whole")])

    def test_views_only_when_asked(self):
        db = self.open_db(fx.values_everywhere(self.tmp))
        self.assertNotIn("v", set(h.table for h in hits_of(db.session, "token-123")))
        self.assertIn("v", set(h.table for h in hits_of(db.session, "token-123",
                                                        include_views=True)))

    def test_sql_and_native_agree(self):
        path = fx.values_everywhere(self.tmp)
        db = self.open_db(path)
        native = self.open_db(path, native=True)
        for value, contains in ((5, False), ("5", False), (b"token-123", True),
                                ("token-123", True), (b"5", False)):
            self.assertEqual(cells(hits_of(db.session, value, contains)),
                             cells(hits_of(native, value, contains)), (value, contains))

    def test_wal_versions(self):
        db = self.open_db(fx.wal_states(self.tmp))
        found = hits_of(db.session, "after restart 1", records=db.recovered_records())
        self.assertTrue(found)
        self.assertEqual(set(h.source for h in found), set(["WAL"]))     # superseded version
        self.assertTrue(all(h.provenance.get("wal_record") is not None for h in found))
        # a WAL copy identical to the current row is left to the database hit
        now = hits_of(db.session, "after restart 2", records=db.recovered_records())
        self.assertEqual(set(h.source for h in now), set(["DB"]))

    def test_freed_page_records(self):
        db = self.open_db(fx.freelist(self.tmp))
        value = "note number 100 " * 4
        found = hits_of(db.session, value, records=db.recovered_records())
        self.assertEqual(set(h.source for h in found), set(["Freelist"]))
        self.assertTrue(all(h.kind == "whole" and h.provenance.get("page") for h in found))
        self.assertEqual(found[0].columns, ["id", "body"])


if __name__ == "__main__":
    unittest.main()
