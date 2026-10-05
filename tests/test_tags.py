"""Row tags (engine.tags): stable keys, the store's operations, persistence in the app-data
folder (never the evidence folder), merging, the evidence check and the recent list.

Every test points SGA_DATA_DIR at a temporary folder: nothing reaches the real profile."""
import json
import os
import time
import unittest
from unittest import mock

from tests.helpers import TempDirTest
from tests.fixtures import make_fixtures as fx
from engine.fileformat.record import InvalidText
from engine.schema import Locator
from engine import tags as T
from engine.tags import (TagError, TagStore, PartialBlob, add_recent, data_dir,
                         db_key, decode_value, encode_value, entry_from_db_row,
                         entry_from_freelist, entry_from_group, entry_from_record,
                         entry_from_wal_record, freelist_key, group_key, load_settings,
                         locator_from_json, locator_to_json, save_settings, tag_file_path,
                         wal_key)


class DataDirTest(TempDirTest):
    """A temp folder as the app-data folder (SGA_DATA_DIR) for the duration of each test."""

    def setUp(self):
        TempDirTest.setUp(self)
        self.data = os.path.join(self.tmp, "appdata")
        self.evidence = os.path.join(self.tmp, "evidence")
        os.makedirs(self.evidence)
        patcher = mock.patch.dict(os.environ, {T.DATA_DIR_ENV: self.data})
        patcher.start()
        self.addCleanup(patcher.stop)

    def db_path(self, name="case.db"):
        return os.path.join(self.evidence, name)

    def entry(self, n, table="t"):
        return entry_from_db_row(table, Locator("rowid", n), ["id", "s"], [n, "row %d" % n])


def open_db(path):
    from database import DB
    db = DB()
    db.open(path)
    return db


class KeyStabilityTest(TempDirTest):
    """The same row gets the same key in every session, whichever way it was reached."""

    def keys_of(self, path, fn):
        out = []
        for _ in range(2):          # two sessions
            db = open_db(path)
            try:
                out.append(fn(db))
            finally:
                db.close()
        return out

    def test_rowid_rows(self):
        path = fx.views(self.tmp)

        def keys(db):
            _cols, rows = db.browse("t", 5, 0)
            cols = [c for c, _t in db.columns("t")]
            return [entry_from_db_row("t", r[0], cols, r[1:]).key for r in rows]
        first, second = self.keys_of(path, keys)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 'DB|t|{"kind":"rowid","value":1}')
        self.assertEqual(len(set(first)), 5)

    def test_without_rowid_blob_primary_key(self):
        path = fx.without_rowid(self.tmp)

        def keys(db):
            _cols, rows = db.browse("blob_pk", 3, 0)
            return [(r[0], entry_from_db_row("blob_pk", r[0], ["id", "v"], r[1:]))
                    for r in rows]
        first, second = self.keys_of(path, keys)
        self.assertEqual([e.key for _l, e in first], [e.key for _l, e in second])
        loc, e = first[0]
        self.assertIsInstance(loc.value[0], bytes)
        self.assertIn('"$hex"', e.key)
        back = e.row_locator()
        self.assertEqual(back, loc)                       # bytes and the tuple come back
        self.assertIsInstance(back.value, tuple)
        self.assertEqual(db_key("blob_pk", back), e.key)

    def test_view_rows_are_keyed_by_their_values(self):
        path = fx.views(self.tmp)

        def keys(db):
            _c, plain = db.browse("vw", 100, 0)
            _c, rev = db.browse("vw", 100, 0, "name", "DESC")
            by_values = dict((tuple(r[1:]), db_key("vw", r[0])) for r in plain)
            same = all(db_key("vw", r[0]) == by_values[tuple(r[1:])] for r in rev)
            return sorted(by_values.values()), same, plain[0][0]
        (first, same1, loc), (second, same2, _loc) = self.keys_of(path, keys)
        self.assertEqual(first, second)
        self.assertTrue(same1 and same2)     # sorted or not, the same row, the same key
        self.assertEqual(len(set(first)), 100)
        self.assertTrue(first[0].startswith("DB|vw|values:"))
        e = entry_from_db_row("vw", loc, list(loc.snapshot[0]), list(loc.snapshot[1]))
        self.assertEqual(e.key, db_key("vw", loc))
        self.assertEqual(e.row_locator().snapshot[1], list(loc.snapshot[1]))

    def test_wal_row_versions(self):
        path = fx.wal_states(self.tmp)

        def keys(db):
            recs = list(db.wal.recover_all_records())
            return [(r, entry_from_wal_record(r).key) for r in recs]
        first, second = self.keys_of(path, keys)
        self.assertEqual([k for _r, k in first], [k for _r, k in second])
        by_key = {}
        for rec, key in first:
            by_key.setdefault(key, set()).add(tuple(map(repr, rec["raw_values"])))
            self.assertEqual(key, wal_key(rec["table"], rec["locator"], rec["raw_values"]))
        # a version copied into several frames is one key; one key is one version
        self.assertLess(len(by_key), len(first))
        self.assertTrue(all(len(v) == 1 for v in by_key.values()))
        # two versions of row 3001 (before and after the UPDATE) are different keys
        versions = set(k for rec, k in first if rec["table"] == "t" and rec["rowid"] == 3001)
        self.assertGreaterEqual(len(versions), 2)
        rec = first[0][0]
        e = entry_from_wal_record(rec)
        self.assertEqual((e.source, e.provenance["frame"], e.provenance["frame_state"]),
                         ("WAL", rec["frame_idx"], rec["category"]))

    def test_freelist_records(self):
        path = fx.freelist(self.tmp)

        def keys(db):
            out = []
            for r in db.freed_page_records():
                out.append(entry_from_freelist(r.table, r.prov.page, r.prov.offset,
                                               r.columns, r.values, r.rowid,
                                               r.confidence).key)
            return out
        first, second = self.keys_of(path, keys)
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), len(first))
        self.assertTrue(all(k.startswith("Freelist|") for k in first))

    def test_search_groups_match_the_rows_they_show(self):
        from search_results import group_hits
        path = fx.freelist(self.tmp)
        db = open_db(path)
        try:
            hits = []
            for _t, found, _err in db.search_tables(["notes"], "note number 1", "Case-Insensitive",
                                                    50, False, None):
                hits.extend(found)
            for h in hits:
                h["source"] = "DB"
            hits += list(db.search_freelist("note number 3", "Case-Insensitive", limit=5))
            groups = group_hits(hits)
            db_groups = [g for g in groups if g.source == "DB"]
            free = [g for g in groups if g.source == "Freelist"]
            self.assertTrue(db_groups and free)
            g = db_groups[0]
            row = db.session.row("notes", g.locator)
            e = entry_from_group(g, db.session.visible_columns("notes"), row.values)
            self.assertEqual(group_key(g), e.key)
            self.assertEqual(e.key, db_key("notes", g.locator))
            with self.assertRaises(TagError):
                entry_from_group(g)          # a table row needs its values read first
            f = free[0]
            fe = entry_from_group(f)
            self.assertEqual(group_key(f), fe.key)
            self.assertEqual(fe.key, freelist_key(f.first["page"], f.first["cell_offset"]))
            self.assertEqual(fe.provenance["confidence"], f.first["confidence"])
        finally:
            db.close()


class ValueEncodingTest(unittest.TestCase):
    def test_values_round_trip_as_json(self):
        big = bytes(range(256)) * 5000                      # > 1 MiB
        values = [None, 0, -5, 2 ** 70, 1.5, float("nan"), float("inf"), "text é",
                  b"", b"\x00\xff", InvalidText(b"\xff\xfe"), big]
        encoded = json.loads(json.dumps([encode_value(v) for v in values]))
        back = [decode_value(v) for v in encoded]
        self.assertEqual(back[:5], values[:5])
        self.assertNotEqual(back[5], back[5])              # nan
        self.assertEqual(back[6], float("inf"))
        self.assertEqual((back[7], back[8], back[9]), ("text é", b"", b"\x00\xff"))
        self.assertIsInstance(back[10], InvalidText)
        self.assertEqual(bytes(back[10]), b"\xff\xfe")
        self.assertIsInstance(back[11], PartialBlob)
        self.assertEqual((back[11].size, len(back[11])), (len(big), T.BLOB_HEAD))
        self.assertEqual(bytes(back[11]), big[:T.BLOB_HEAD])
        self.assertEqual(encode_value(back[11]), encode_value(big))
        # 1 and 1.0, text and BLOB: different values, different digests
        self.assertNotEqual(T.values_digest([1]), T.values_digest([1.0]))
        self.assertNotEqual(T.values_digest(["a"]), T.values_digest([b"a"]))

    def test_locators(self):
        for loc in (Locator("rowid", 7), Locator("pk", ("a", 1, b"\x00", None, 2.5)),
                    Locator("ordinal", 3)):
            d = json.loads(json.dumps(locator_to_json(loc)))
            self.assertEqual(locator_from_json(d), loc)
        self.assertIsNone(locator_to_json(None))


class StoreTest(DataDirTest):
    def test_toggle_bulk_notes_and_counts(self):
        s = TagStore(self.db_path())
        a, b, c = self.entry(1), self.entry(2), self.entry(3, "u")
        self.assertTrue(s.toggle(a, "Relevant"))
        self.assertEqual(s.tags_of(a.key), ["Relevant"])
        self.assertFalse(s.toggle(self.entry(1), "Relevant"))   # a new object, the same row
        self.assertNotIn(a.key, s)
        self.assertEqual(s.add([self.entry(1), self.entry(2), c], "Review"), 3)
        self.assertEqual(s.add([self.entry(1), self.entry(2)], "Review"), 0)   # already
        self.assertEqual(s.add([self.entry(1)], "Suspicious"), 1)
        # tags are kept in definition order: the first one colours the row
        self.assertEqual(s.tags_of(a.key), ["Review", "Suspicious"])
        s.set_note(a.key, "seen in the chat export")
        self.assertEqual(s.get(a.key).note, "seen in the chat export")
        with self.assertRaises(TagError):
            s.set_note(self.entry(99).key, "x")                 # untagged rows have no note
        self.assertEqual(s.counts(), {"Relevant": 0, "Review": 3, "Suspicious": 1,
                                      "Not relevant": 0})
        self.assertTrue(s.has_table("u") and not s.has_table("zzz"))
        self.assertEqual(s.remove([a.key, b.key], "Review"), 2)
        self.assertEqual(s.tags_of(a.key), ["Suspicious"])     # keeps its other tag
        self.assertNotIn(b.key, s)                              # no tag left: dropped
        self.assertEqual(s.remove([c.key]), 1)
        self.assertEqual([e.key for e in s.entries()], [a.key])
        self.assertEqual(s.add([self.entry(5)], "Brand new"), 1)   # unknown tag: defined
        self.assertIsNotNone(s.def_of("Brand new"))
        self.assertTrue(s.dirty)

    def test_rename_recolor_move_delete(self):
        s = TagStore(self.db_path())
        s.add([self.entry(1), self.entry(2)], "Review")
        s.add([self.entry(2)], "Suspicious")
        s.rename_def("Review", "Checked")
        self.assertEqual(s.tags_of(self.entry(1).key), ["Checked"])
        with self.assertRaises(TagError):
            s.rename_def("Checked", "Suspicious")
        with self.assertRaises(TagError):
            s.add_def("Suspicious")
        s.recolor_def("Checked", "#123456")
        self.assertEqual(s.color_of("Checked"), "#123456")
        s.recolor_def("Checked", "red;} body{x")                # not a colour: ignored
        self.assertEqual(s.color_of("Checked"), "#123456")
        s.move_def("Suspicious", -5)
        self.assertEqual(s.names()[0], "Suspicious")
        self.assertEqual(s.tags_of(self.entry(2).key), ["Suspicious", "Checked"])
        self.assertEqual(s.delete_def("Checked"), 2)
        self.assertNotIn(self.entry(1).key, s)                  # had only that tag
        self.assertEqual(s.tags_of(self.entry(2).key), ["Suspicious"])
        self.assertIsNone(s.def_of("Checked"))

    def test_saves_in_the_app_data_folder_and_reloads(self):
        path = self.db_path()
        s = TagStore(path)
        self.assertEqual(s.path, tag_file_path(path))
        self.assertTrue(s.path.startswith(os.path.join(self.data, "cases")))
        self.assertTrue(os.path.basename(s.path).startswith("case.db-"))
        big = b"\x89PNG\r\n\x1a\n" + bytes(2 << 20)
        rows = [entry_from_db_row("t", Locator("rowid", 1), ["a", "b", "c"],
                                  [big, InvalidText(b"\xff"), float("nan")]),
                entry_from_db_row("w", Locator("pk", (b"\x01", "k")), ["k1", "k2", "v"],
                                  [b"\x01", "k", 3])]
        s.add(rows, "Relevant")
        s.add(rows[1:], "Suspicious")
        s.set_note(rows[1].key, "note\nwith two lines")
        s.set_table_state("t", {"widths": {"a": 120}, "hidden": ["b"], "sort": ["c", True]})
        s.last_table = "t"
        s.set_evidence(path, 100, 200, "ab" * 32)
        self.assertTrue(s.save())
        self.assertFalse(s.save())                               # nothing changed since
        self.assertEqual(os.listdir(os.path.dirname(s.path)), [os.path.basename(s.path)])
        self.assertEqual(os.listdir(self.evidence), [])          # nothing beside the evidence
        with open(s.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual((data["format"], data["version"]), ("sqlite-gui-analyzer-tags", 1))
        self.assertEqual(data["evidence"]["sha256"], "ab" * 32)
        t = TagStore(path)
        self.assertTrue(t.load())
        self.assertEqual([e.key for e in t.entries()], [e.key for e in s.entries()])
        self.assertEqual(t.tags_of(rows[1].key), ["Relevant", "Suspicious"])
        self.assertEqual(t.get(rows[1].key).note, "note\nwith two lines")
        vals = t.get(rows[0].key).row_values()
        self.assertIsInstance(vals[0], PartialBlob)
        self.assertEqual(vals[0].size, len(big))
        self.assertIsInstance(vals[1], InvalidText)
        self.assertNotEqual(vals[2], vals[2])
        self.assertEqual(t.get(rows[1].key).row_locator(), Locator("pk", (b"\x01", "k")))
        self.assertEqual(t.table_state("t"), {"widths": {"a": 120}, "hidden": ["b"],
                                              "sort": ["c", True]})
        self.assertEqual(t.last_table, "t")
        self.assertFalse(t.dirty)

    def test_an_interrupted_save_keeps_the_previous_file(self):
        s = TagStore(self.db_path())
        s.add([self.entry(1)], "Relevant")
        s.save()
        with open(s.path, "rb") as f:
            before = f.read()
        s.add([self.entry(2)], "Relevant")

        def broken_dump(obj, fp, **kw):
            fp.write('{"format": "sqlite-gui-analyzer-tags", "entr')
            raise OSError("disk full")
        with mock.patch("engine.tags.json.dump", broken_dump):
            with self.assertRaises(OSError):
                s.save()
        with open(s.path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.listdir(os.path.dirname(s.path)), [os.path.basename(s.path)])
        self.assertTrue(s.dirty)                                 # still to be saved
        s.save()
        t = TagStore(self.db_path())
        t.load()
        self.assertEqual(len(t), 2)

    def test_merge_on_load(self):
        s = TagStore(self.db_path())
        a, b = self.entry(1), self.entry(2)
        s.add([a], "Relevant")
        s.set_note(a.key, "short")
        s.add([b], "Review")
        s.set_note(b.key, "a note that is longer than the other one")
        other = TagStore(self.db_path(), path=os.path.join(self.tmp, "other.json"))
        other.defs.append(T.TagDef("Imported", "#0065ff"))
        a2, b2, c2 = self.entry(1), self.entry(2), self.entry(3)
        other.add([a2, c2], "Imported")
        other.add([b2], "Review")
        time.sleep(0.01)
        other.set_note(a2.key, "newer note")
        other.set_note(b2.key, "older")
        other.get(b2.key).updated_at = "2000-01-01T00:00:00.000Z"
        other.save_as(other.path)
        added, merged = s.merge_file(other.path)
        self.assertEqual((added, merged), (1, 1))
        self.assertEqual(s.tags_of(a.key), ["Relevant", "Imported"])
        self.assertEqual(s.get(a.key).note, "newer note")        # the newer note wins
        self.assertEqual(s.get(b.key).note, "a note that is longer than the other one")
        self.assertIn(c2.key, s)
        self.assertEqual(s.color_of("Imported"), "#0065ff")
        # equally new: the longer note is kept
        same = s.get(b.key).updated_at
        data = {"format": T.FILE_FORMAT, "version": 1, "entries": [
            dict(b2.as_dict(), note="x" * 80, updated_at=same, tags=["Review"])]}
        s.merge_data(data)
        self.assertEqual(s.get(b.key).note, "x" * 80)
        with self.assertRaises(TagError):
            s.merge_data({"format": "something else"})

    def test_evidence_changed_warning(self):
        path = self.db_path()
        s = TagStore(path)
        s.add([self.entry(1)], "Relevant")
        self.assertEqual(s.set_evidence(path, 1000, 5, None), [])   # nothing saved before
        s.save()
        t = TagStore(path)
        t.load()
        self.assertEqual(t.set_evidence(path, 1000, 5, None), [])
        t.evidence["sha256"] = "aa" * 32
        self.assertEqual(t.evidence_differences(), [])          # no hash was saved
        u = TagStore(path)
        u.load()
        diffs = u.set_evidence(path, 1024, 6, None)
        self.assertEqual(diffs, ["size 1000 -> 1024", "modification time changed"])
        self.assertEqual(len(u), 1)                              # the tags are kept
        u.set_evidence(path, 1000, 5, "aa" * 32)
        u.save(force=True)
        v = TagStore(path)
        v.load()
        self.assertEqual(v.set_evidence(path, 1000, 5, "bb" * 32), ["SHA-256 changed"])

    def test_refuses_to_save_inside_the_evidence_folder(self):
        path = self.db_path()
        inside = TagStore(path, path=os.path.join(self.evidence, "tags", "case.json"))
        inside.add([self.entry(1)], "Relevant")
        with self.assertRaises(TagError):
            inside.save()
        with self.assertRaises(TagError):
            inside.save_as(os.path.join(self.evidence, "export.json"))
        with self.assertRaises(TagError):
            inside.save_as(self.evidence)
        # an app-data folder misconfigured to point into the evidence folder
        with mock.patch.dict(os.environ, {T.DATA_DIR_ENV: os.path.join(self.evidence, "x")}):
            s = TagStore(path)
            s.add([self.entry(1)], "Relevant")
            with self.assertRaises(TagError):
                s.save()
            with self.assertRaises(TagError):
                save_settings({"recent": [path]}, refuse_in=self.evidence)
        with mock.patch.dict(os.environ, {T.DATA_DIR_ENV: self.evidence}):
            with self.assertRaises(TagError):
                TagStore(path).save(force=True)
        self.assertEqual(os.listdir(self.evidence), [])          # nothing was created there

    def test_an_unreadable_tag_file_is_kept_aside(self):
        s = TagStore(self.db_path())
        os.makedirs(os.path.dirname(s.path))
        with open(s.path, "w") as f:
            f.write("{not json")
        t = TagStore(self.db_path())
        self.assertFalse(t.load())
        self.assertTrue(t.warnings and "could not be read" in t.warnings[0])
        names = os.listdir(os.path.dirname(s.path))
        self.assertEqual(len(names), 1)
        self.assertIn(".unreadable-", names[0])
        t.add([self.entry(1)], "Relevant")
        t.save()
        self.assertEqual(len(os.listdir(os.path.dirname(s.path))), 2)

    def test_generic_and_forensic_records(self):
        rec = {"source": "Carved", "table": "messages", "columns": ["id", "body"],
               "values": [7, b"\x00"], "rowid": 7, "id": "0123456789abcdef",
               "provenance": {"page": 12, "offset": 300, "confidence": "high"}}
        e = entry_from_record(rec)
        self.assertEqual(e.key, "Carved|0123456789abcdef")
        self.assertEqual((e.table, e.rowid, e.row_values()), ("messages", "7", [7, b"\x00"]))
        self.assertEqual(e.provenance["confidence"], "high")
        no_id = dict(rec, id=None)
        self.assertEqual(entry_from_record(no_id).key, entry_from_record(dict(no_id)).key)
        from engine.forensics.provenance import Provenance, Record
        r = Record("notes", ["id", "body"], [3, "gone"], Provenance("freeblock", "main", 5, 100),
                   "medium", ["fits the schema"], rowid=3)
        fe = entry_from_record(r)
        self.assertEqual(fe.key, "Carved|%s" % r.id)
        self.assertEqual((fe.provenance["source"], fe.provenance["page"],
                          fe.provenance["confidence"]), ("freeblock", 5, "medium"))
        # a DB.record_rows() line (display values beside the raw ones and the Record)
        from database import DB
        line = DB.record_rows([r])[0]
        le = entry_from_record(line)
        self.assertEqual((le.key, le.row_values(), le.provenance), (fe.key, [3, "gone"],
                                                                    fe.provenance))
        raw_first = dict(rec, values=["[BLOB: 1 B]", "x"], raw_values=[7, b"\x00"])
        self.assertEqual(entry_from_record(raw_first).row_values(), [7, b"\x00"])

    def test_data_dir_and_file_names(self):
        self.assertEqual(data_dir(), os.path.abspath(self.data))
        self.assertEqual(data_dir("linux", {"XDG_DATA_HOME": os.path.join(self.tmp, "xdg")}),
                         os.path.join(self.tmp, "xdg", "sqlite-gui-analyzer"))
        self.assertEqual(data_dir("win32", {"APPDATA": os.path.join(self.tmp, "roaming")}),
                         os.path.join(self.tmp, "roaming", "SQLite GUI Analyzer"))
        self.assertTrue(data_dir("linux", {}).endswith(
            os.path.join(".local", "share", "sqlite-gui-analyzer")))
        self.assertEqual(data_dir("win32", {T.DATA_DIR_ENV: self.tmp}), os.path.abspath(self.tmp))
        a = tag_file_path(os.path.join(self.tmp, "x", "msgstore.db"))
        b = tag_file_path(os.path.join(self.tmp, "y", "msgstore.db"))
        self.assertNotEqual(a, b)                  # same name, different folder
        self.assertEqual(os.path.basename(a)[:len("msgstore.db-")], "msgstore.db-")
        self.assertEqual(tag_file_path(os.path.join(self.tmp, "x", "..", "x", "msgstore.db")), a)

    def test_recent_list(self):
        settings = load_settings()
        self.assertEqual(settings, {})
        for i in range(12):
            add_recent(settings, os.path.join(self.tmp, "db%d.sqlite" % i))
        add_recent(settings, os.path.join(self.tmp, "db3.sqlite"))
        self.assertEqual(len(settings["recent"]), T.RECENT_MAX)
        self.assertEqual(settings["recent"][0], os.path.join(self.tmp, "db3.sqlite"))
        self.assertEqual(len(set(settings["recent"])), T.RECENT_MAX)
        save_settings(settings)
        self.assertEqual(load_settings(), settings)
        self.assertTrue(os.path.isfile(os.path.join(self.data, "settings.json")))


if __name__ == "__main__":
    unittest.main()
