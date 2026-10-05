"""The engine side of several databases examined together (a case): finding them in a folder,
their names, identities and colours, tags that name their database (and tag files written
before cases), saved cases, the named limits and the links between databases matched by value.
"""

import json
import os
import unittest
from unittest import mock

from tests.helpers import TempDirTest
from tests.fixtures import case_fixtures as cf
from engine import limits
from engine import tags as T
from engine.case import (Candidate, chip_colour, db_identity, display_names, is_sqlite_file,
                         scan_folder)
from engine.schema import Locator


class DataDirTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        patcher = mock.patch.dict(os.environ, {T.DATA_DIR_ENV: os.path.join(self.tmp, "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(limits.reset)


class ScanFolderTest(TempDirTest):
    def test_lists_only_sqlite_files_with_their_sidecars(self):
        a = os.path.join(self.tmp, "a")
        os.makedirs(os.path.join(a, "sub"))
        cf.messages(a)                          # with a live WAL
        cf.contacts(os.path.join(a, "sub"))
        with open(os.path.join(a, "notes.txt"), "w") as f:
            f.write("not a database")
        with open(os.path.join(a, "old.db-wal.bak"), "wb") as f:
            f.write(b"\x37\x7f\x06\x82" + b"\0" * 28)    # a WAL copy: not a database
        found, seen, problems = scan_folder(a)
        self.assertEqual([c.rel for c in found], ["messages.db"])
        self.assertTrue(found[0].wal and found[0].wal_size > 0)
        self.assertIn("WAL", found[0].sidecars_text())
        self.assertEqual(problems, [])
        self.assertEqual(seen, 3)               # the -wal sidecar is not looked at alone
        found, _seen, _p = scan_folder(a, recursive=True)
        self.assertEqual([c.rel.replace("\\", "/") for c in found],
                         ["messages.db", "sub/contacts.db"])
        self.assertTrue(is_sqlite_file(found[1].path))
        self.assertFalse(is_sqlite_file(os.path.join(a, "notes.txt")))

    def test_limit_and_missing_folder_are_reported(self):
        for i in range(5):
            cf.settings(self.tmp, "s%d.db" % i)
        found, seen, problems = scan_folder(self.tmp, limit=3)
        self.assertEqual((len(found), seen), (3, 3))
        self.assertIn("limit folder_scan_files", problems[0])
        found, seen, problems = scan_folder(os.path.join(self.tmp, "gone"))
        self.assertEqual((found, seen), ([], 0))
        self.assertIn("not a folder", problems[0])

    def test_cancel(self):
        cf.settings(self.tmp)
        found, _seen, _problems = scan_folder(self.tmp, cancel=lambda: True)
        self.assertEqual(found, [])


class NamesTest(unittest.TestCase):
    def test_unique_names(self):
        self.assertEqual(display_names(["/x/a/msgstore.db", "/x/b/msgstore.db", "/x/wa.db"]),
                         ["msgstore.db (a)", "msgstore.db (b)", "wa.db"])
        self.assertEqual(display_names(["/x/a/m.db", "/y/a/m.db"]), ["m.db (a)", "m.db (a) #2"])

    def test_identity_and_colours(self):
        p = os.path.join("x", "a.db")
        self.assertEqual(db_identity(p, 10), db_identity(os.path.abspath(p), 10))
        self.assertNotEqual(db_identity(p, 10), db_identity(p, 11))
        c1 = chip_colour([])
        self.assertNotEqual(chip_colour([c1]), c1)
        self.assertEqual(repr(Candidate("/p", "p", 5)), "Candidate('p', 5)")


class CaseTagsTest(DataDirTest):
    def test_entries_name_their_database_and_old_files_load(self):
        db = os.path.join(self.tmp, "evidence", "a.db")
        e = T.entry_from_db_row("t", Locator("rowid", 1), ["x"], [1])
        store = T.TagStore(db)
        store.add([e], "Review")
        store.set_evidence(db, 123)
        self.assertEqual(e.database["identity"], db_identity(db, 123))
        self.assertEqual(e.case_key, "%s\x1f%s" % (db_identity(db, 123), e.key))
        store.save(force=True)
        # a tag file written before cases: no database on its entries
        with open(store.path, encoding="utf-8") as f:
            data = json.load(f)
        for d in data["entries"]:
            d.pop("database", None)
        with open(store.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        again = T.TagStore(db)
        self.assertTrue(again.load())
        again.set_evidence(db, 123)
        loaded = again.entries()[0]
        self.assertEqual(loaded.identity, db_identity(db, 123))
        self.assertEqual(loaded.database["name"], "a.db")
        # an entry of another database keeps its own
        other = T.entry_from_db_row("t", Locator("rowid", 2), ["x"], [2])
        other.database = T.database_info(os.path.join(self.tmp, "b.db"), 5)
        again.add([other], "Review")
        self.assertEqual(again.get(other.key).database["name"], "b.db")
        self.assertEqual(T.TagEntry.from_dict(other.as_dict()).identity, other.identity)

    def test_case_file_round_trip_and_changes(self):
        paths = cf.build(self.tmp)
        dbs = []
        for p in paths:
            st = os.stat(p)
            dbs.append({"path": p, "name": os.path.basename(p), "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns, "sha256": "ab" * 32, "color": "#0065ff"})
        path = T.case_file_path(paths)
        self.assertEqual(path, T.case_file_path(list(reversed(paths))))
        T.write_case(path, dbs, active=paths[1], state={"search_databases": [paths[0]]})
        data = T.read_case(path)
        self.assertEqual([d["path"] for d in data["databases"]], list(paths))
        self.assertEqual(data["active"], paths[1])
        self.assertEqual(data["state"], {"search_databases": [paths[0]]})
        self.assertEqual(T.case_changes(data["databases"][0]), [])
        changed = dict(data["databases"][0], size=1)
        self.assertIn("size 1 -> ", T.case_changes(changed)[0])
        self.assertEqual(T.case_changes(dict(changed, path=paths[0] + ".gone")), ["missing"])
        self.assertEqual(T.sha256_change(dbs[0], "cd" * 32), "SHA-256 changed")
        self.assertEqual(T.sha256_change(dbs[0], "ab" * 32), "")
        with self.assertRaises(T.TagError):
            T.write_case(os.path.join(self.tmp, "c.json"), dbs, refuse_in=[self.tmp])
        with open(os.path.join(self.tmp, "bad.json"), "w") as f:
            f.write("{}")
        with self.assertRaises(T.TagError):
            T.read_case(os.path.join(self.tmp, "bad.json"))
        settings = {}
        T.add_recent_case(settings, path, ["a", "b"])
        T.add_recent_case(settings, path, ["a", "b", "c"])
        self.assertEqual(settings["recent_cases"], [{"path": os.path.abspath(path),
                                                     "names": ["a", "b", "c"]}])


class LimitsTest(DataDirTest):
    def test_bad_values_keep_the_default_and_are_reported(self):
        problems = limits.load({"limits": {"case_search_parallel": 2, "folder_scan_files": -5,
                                           "crossdb_min_found": "3", "nope": 1,
                                           "hash_parallel": True}})
        self.assertEqual(limits.get("case_search_parallel"), 2)
        self.assertEqual(limits.get("folder_scan_files"), limits.DEFAULTS["folder_scan_files"])
        self.assertEqual(limits.get("crossdb_min_found"), limits.DEFAULTS["crossdb_min_found"])
        self.assertEqual(len(problems), 4)
        self.assertEqual(limits.problems, problems)
        self.assertEqual(limits.load({"limits": "x"}),
                         ["settings 'limits' is not a set of name: value pairs; defaults used"])
        self.assertEqual(limits.load(None), [])
        self.assertEqual(sorted(limits.DEFAULTS), sorted(limits.RANGES))


class CrossDatabaseLinksTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        from database import DB
        self.dbs = []
        for p in cf.build(self.tmp):
            d = DB()
            d.open(p)
            self.dbs.append(d)
        self.addCleanup(lambda: [d.close() for d in self.dbs])
        self.addCleanup(limits.reset)

    def test_links_found_by_value_both_ways(self):
        from engine.crossdb import find_links, identifier_like
        res = find_links([(i, d.session) for i, d in enumerate(self.dbs)])
        got = sorted((l.src_db, l.src_table, l.src_col, l.dst_db, l.dst_table, l.dst_col,
                      round(l.fraction, 2), l.confident) for l in res.links)
        self.assertEqual(got, [(0, "jid", "raw_string", 1, "wa_contacts", "jid", 0.75, True),
                               (0, "message", "key_remote_jid", 1, "wa_contacts", "jid", 1.0,
                                True)])
        link = [l for l in res.links if l.src_table == "jid"][0]
        self.assertTrue(link.reason().startswith("matched by value: 75% of 20 sampled "
                                                 "jid.raw_string values found in "
                                                 "wa_contacts.jid"))
        self.assertEqual(link.other_end(1, "wa_contacts", "JID"), (0, "jid", "raw_string"))
        self.assertTrue(link.touches(0, "jid", "raw_string"))
        self.assertEqual(res.profiles[2], 2)     # settings: profiled, nothing in common
        self.assertIn("first 50,000 rows", res.limits_text())
        self.assertFalse(identifier_like("two words"))
        self.assertFalse(identifier_like("abc"))
        self.assertTrue(identifier_like("123@s.example.net"))

    def test_limits_and_cancel(self):
        from engine.crossdb import find_links
        limits.load({"limits": {"crossdb_max_pairs": 1}})
        res = find_links([(i, d.session) for i, d in enumerate(self.dbs)])
        self.assertEqual((len(res.links), res.unchecked), (1, 1))
        self.assertIn("crossdb_max_pairs", res.limits_text())
        res = find_links([(i, d.session) for i, d in enumerate(self.dbs)], cancel=lambda: True)
        self.assertTrue(res.cancelled)


class TimelineDatabaseTest(TempDirTest):
    def test_events_of_several_databases_export_their_database(self):
        from engine import timeline as tl
        from database import DB
        paths = cf.build(self.tmp)
        evs = []
        for p in paths:
            d = DB()
            d.open(p)
            self.addCleanup(d.close)
            det = tl.detect(d.session)
            res = tl.build_events(d.session, det.enabled(), det.descriptions)
            for e in res.events:
                e.database = os.path.basename(p)
            evs.extend(res.events)
        evs = tl.finish(evs)
        self.assertEqual(len(evs), 140)
        # the first contact's last_seen is T0; the first message a minute later
        self.assertEqual([e.as_dict()["database"] for e in evs[:2]],
                         ["contacts.db", "messages.db"])
        out = os.path.join(os.path.dirname(self.tmp), os.path.basename(self.tmp) + "_tl.csv")
        try:
            tl.export_events(out, "csv", evs)
            with open(out, encoding="utf-8-sig") as f:
                head = f.readline().strip().split(",")
            self.assertEqual(head[:3], ["time_utc", "database", "table"])
        finally:
            os.remove(out)


if __name__ == "__main__":
    unittest.main()
