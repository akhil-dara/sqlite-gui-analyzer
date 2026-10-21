"""Rows on pages nothing references are listed apart from deleted rows (and the audit says the
tree may be damaged), and older cells left in a page's content area are carved.

A manipulated root or child pointer hides live rows on a page no tree reaches (they are not
deleted); a page rewrite can leave older cells between the live ones of a page."""

import os
import sqlite3
import struct
import unittest

from tests.helpers import TempDirTest
from engine.session import Session


def make_db(path, rows=12):
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=1024")
    c.execute("CREATE TABLE people (id INTEGER, name TEXT, city TEXT)")
    c.execute("CREATE TABLE other (x INTEGER, y TEXT)")
    c.executemany("INSERT INTO people VALUES (?,?,?)",
                  [(i, "person%02d" % i, "city%02d" % i) for i in range(1, rows + 1)])
    c.executemany("INSERT INTO other VALUES (?,?)", [(i, "o%d" % i) for i in range(3)])
    c.commit()
    c.close()


def carve(path):
    s = Session(path, hash_evidence=False)
    try:
        fx = s.forensics
        res = fx.carve(time_limit=60, index_entries=False)
        findings = fx.audit(time_limit=30)
        return list(res), findings
    finally:
        s.close(verify=False)


class UnreferencedPagesTest(TempDirTest):
    def test_rows_hidden_by_a_changed_root_page_are_not_called_deleted(self):
        path = os.path.join(self.tmp, "hidden.db")
        make_db(path)
        c = sqlite3.connect(path)
        root_other = c.execute("SELECT rootpage FROM sqlite_master WHERE name='other'")\
            .fetchone()[0]
        c.execute("PRAGMA writable_schema=ON")
        # the table now points at another table's page: its own page is referenced by nothing
        c.execute("UPDATE sqlite_master SET rootpage=? WHERE name='people'", (root_other,))
        c.commit()
        c.close()
        records, findings = carve(path)
        hidden = [r for r in records if "person05" in [str(v) for v in r.values]]
        self.assertTrue(hidden, "the hidden live rows are found")
        for r in hidden:
            self.assertIn("unreferenced_page", r.flags)
            self.assertEqual(r.prov.source, "orphan")
            self.assertNotEqual(r.confidence, "high")
            self.assertTrue(any("no table, index or the freelist" in x for x in r.reasons))
        codes = [f.code for f in findings]
        self.assertIn("UNREFERENCED_PAGES", codes)
        f = next(f for f in findings if f.code == "UNREFERENCED_PAGES")
        self.assertEqual(f.level, "warning")
        self.assertIn("damaged or manipulated", f.message)

    def test_a_consistent_file_has_no_unreferenced_pages(self):
        path = os.path.join(self.tmp, "clean.db")
        make_db(path)
        c = sqlite3.connect(path)
        c.execute("DELETE FROM people WHERE id IN (3, 4)")
        c.commit()
        c.close()
        records, findings = carve(path)
        self.assertNotIn("UNREFERENCED_PAGES", [f.code for f in findings])
        self.assertFalse(any("unreferenced_page" in r.flags for r in records))


class DeadSpaceTest(TempDirTest):
    def test_a_cell_no_pointer_and_no_freeblock_covers_is_carved(self):
        path = os.path.join(self.tmp, "dead.db")
        make_db(path)
        c = sqlite3.connect(path)
        root = c.execute("SELECT rootpage FROM sqlite_master WHERE name='people'").fetchone()[0]
        c.close()
        with open(path, "rb") as f:
            data = bytearray(f.read())
        ps = 1024
        base = (root - 1) * ps
        self.assertEqual(data[base], 13)                    # a table leaf
        count = struct.unpack_from(">H", data, base + 3)[0]
        # drop the last cell pointer: that cell stays in the content area, in no freeblock
        # (what a page rewrite leaves behind)
        last = struct.unpack_from(">H", data, base + 8 + 2 * (count - 1))[0]
        struct.pack_into(">H", data, base + 3, count - 1)
        struct.pack_into(">H", data, base + 8 + 2 * (count - 1), 0)
        with open(path, "wb") as f:
            f.write(bytes(data))
        live = sqlite3.connect("file:%s?immutable=1" % path, uri=True)
        alive = live.execute("SELECT count(*) FROM people").fetchone()[0]
        live.close()
        self.assertEqual(alive, count - 1)
        records, _findings = carve(path)
        lost = [r for r in records if r.table == "people"]
        self.assertEqual(len(lost), 1, [r.values for r in records])
        r = lost[0]
        self.assertEqual(r.prov.source, "unallocated")
        self.assertEqual(r.prov.offset, last)
        self.assertNotIn("unreferenced_page", r.flags)
        # every live row stays out of the carve (no copy of a live cell is taken for one)
        names = [v for v in r.values if isinstance(v, str)]
        self.assertTrue(names and names[0].startswith("person"))


class DamagedSignatureTest(TempDirTest):
    def test_a_wiped_signature_opens_with_the_built_in_parser(self):
        from engine.case import is_sqlite_file
        path = os.path.join(self.tmp, "wiped.db")
        make_db(path)
        with open(path, "rb") as f:
            data = bytearray(f.read())
        data[:16] = bytes(16)                       # the signature only
        with open(path, "wb") as f:
            f.write(bytes(data))
        self.assertTrue(is_sqlite_file(path))
        s = Session(path, hash_evidence=False)
        try:
            self.assertEqual(s.mode, "native")
            self.assertIn("signature is damaged", s.safe_parse_reason)
            self.assertEqual(s.count("people"), 12)
            self.assertIn("header_signature_damaged", [i.kind for i in s.issues])
        finally:
            s.close(verify=False)

    def test_other_files_are_still_refused(self):
        from engine.case import is_sqlite_file
        path = os.path.join(self.tmp, "random.bin")
        with open(path, "wb") as f:
            f.write(bytes(range(256)) * 8)
        self.assertFalse(is_sqlite_file(path))
        path2 = os.path.join(self.tmp, "almost.db")
        make_db(path2)
        with open(path2, "rb") as f:
            data = bytearray(f.read())
        data[:16] = bytes(16)
        data[21] = 99                               # payload fraction wrong too
        with open(path2, "wb") as f:
            f.write(bytes(data))
        self.assertFalse(is_sqlite_file(path2))


class OverflowPastTrunkTest(TempDirTest):
    def test_a_chain_whose_first_page_became_a_trunk_is_read_on(self):
        """Deleting a row with an empty freelist makes its first overflow page the freelist
        trunk and the next one a leaf of it: the rest of the value is still there."""
        path = os.path.join(self.tmp, "docs.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA page_size=4096")
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT, content TEXT)")
        c.execute("INSERT INTO docs VALUES (1, 'kept', 'short')")
        big = "MEMO:" + "A" * 9000
        c.execute("INSERT INTO docs VALUES (2, 'secret memo', ?)", (big,))
        c.execute("INSERT INTO docs VALUES (3, 'after', 'short too')")
        c.commit()
        c.execute("DELETE FROM docs WHERE id = 2")
        c.commit()
        c.close()
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        recs = [r for r in s.forensics.carve() if r.table == "docs" and
                r.values_dict().get("title") == "secret memo"]
        self.assertTrue(recs)
        r = recs[0]
        content = r.values_dict()["content"]
        self.assertEqual(len(content), len(big))                   # not cut at the trunk
        self.assertTrue(content.startswith("MEMO:AAAA"))
        lost = content.count("\0")
        self.assertTrue(0 < lost <= 64, lost)                      # only the trunk's list
        self.assertEqual(content.replace("\0", "A"), big)
        self.assertEqual(r.confidence, "low")
        self.assertIn("overflow_rebuilt", r.flags)
        self.assertTrue(any("zero bytes here" in x for x in r.reasons), r.reasons)



class InferredRowidTest(TempDirTest):
    def test_an_updated_rows_older_version_gets_its_rowid_back(self):
        """An update that changes a cell's size frees the old cell; the freeblock header
        overwrites its rowid. The one live row agreeing in every column but the changed one
        (and on a nearly unique column) is named, flagged rowid_inferred; deleted rows that
        only share common values are left alone."""
        path = os.path.join(self.tmp, "items.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, invoice INTEGER, track INTEGER, "
                  "price REAL, qty INTEGER, state TEXT)")
        c.executemany("INSERT INTO items VALUES (?,?,?,?,?,?)",
                      [(i, 1000 + i // 5, 5000 + i, 0.99, 1, "open") for i in range(1, 401)])
        c.commit()
        c.execute("UPDATE items SET state = 'closed after a long review' "
                  "WHERE id IN (300, 320, 340, 360, 380)")
        c.execute("DELETE FROM items WHERE id IN (390, 391)")
        c.commit()
        c.close()
        s = Session.open(path, hash_evidence=False)
        self.addCleanup(s.close)
        recs = [r for r in s.forensics.carve() if r.table == "items"]
        inferred = [r for r in recs if "rowid_inferred" in r.flags]
        self.assertTrue(inferred, [(r.rowid, r.values, sorted(r.flags)) for r in recs])
        for r in inferred:
            self.assertIn(r.rowid, (300, 320, 340, 360, 380))
            self.assertEqual(r.values_dict()["state"], "open")         # the value before
            self.assertEqual(r.values_dict()["track"], 5000 + r.rowid)
            self.assertIn("prior_version", r.flags)
            self.assertNotEqual(r.confidence, "high")
            self.assertTrue(any("inferred as %d" % r.rowid in x for x in r.reasons))
        self.assertFalse([r for r in recs if r.rowid in (390, 391) and
                          "rowid_inferred" in r.flags])


if __name__ == "__main__":
    unittest.main()
