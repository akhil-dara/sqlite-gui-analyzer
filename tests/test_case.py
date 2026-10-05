"""Several databases as one case, the UI side: the case object and its active database, search
results grouped per database, the merged timeline detection, the linked-value lookup and the
case widgets (Open Folder dialog, Databases: button). The engine side is test_case_engine."""

import os
import unittest

from tests.helpers import TempDirTest, tk_root
from tests.fixtures import case_fixtures as cf
from engine.case import db_identity
from engine.schema import Locator


class CaseObjectTest(TempDirTest):
    def test_members_active_and_remove(self):
        from case import Case
        paths = cf.build(self.tmp)
        case = Case()
        self.addCleanup(lambda: [case.remove(m) for m in list(case)])
        ms = [case.add(p) for p in paths]
        self.assertEqual([m.name for m in case], ["messages.db", "contacts.db", "settings.db"])
        self.assertIs(case.active, ms[0])
        self.assertTrue(case.multi)
        # (a Python without sqlite3 deserialize cannot merge the WAL for SQL)
        self.assertIn(ms[0].status(), ("WAL merged", "WAL not in SQL"))
        self.assertEqual(ms[1].status(), "immutable")
        self.assertEqual(len(set(m.color for m in ms)), 3)
        self.assertIs(case.by_path(paths[1]), ms[1])
        self.assertIs(case.of_db(ms[2].db), ms[2])
        self.assertTrue(case.is_protected(os.path.join(self.tmp, "x.csv")))
        self.assertFalse(case.is_protected(os.path.join(os.path.dirname(self.tmp), "x.csv")))
        self.assertEqual(ms[1].label("t"), "contacts.db › t")
        self.assertEqual(ms[1].identity, db_identity(paths[1], os.path.getsize(paths[1])))
        case.set_active(ms[1])
        report = case.remove(ms[1])
        self.assertTrue(report.unchanged)
        self.assertIs(case.active, ms[2])        # the next one takes over
        case.release_thread_connection()
        case.interrupt()
        self.assertEqual(len(case), 2)


class GroupsPerDatabaseTest(unittest.TestCase):
    def test_same_row_of_two_databases_is_two_lines(self):
        from search_results import ResultGrouper
        g = ResultGrouper()
        loc = Locator("rowid", 1)
        for dbid in (1, 2):
            for col in ("a", "b"):
                g.add({"table": "t", "column": col, "locator": loc, "rowid": 1, "value": "x",
                       "type": "text", "source": "DB", "dbid": dbid, "database": "d%d" % dbid})
        self.assertEqual([(x.dbid, x.database, len(x.hits)) for x in g.groups],
                         [(1, "d1", 2), (2, "d2", 2)])


class TimelineCaseTest(TempDirTest):
    def test_merged_detection(self):
        from engine import timeline as tl
        from timeline_tab import CaseDetection
        from types import SimpleNamespace
        from database import DB
        paths = cf.build(self.tmp)
        dbs = []
        for p in paths:
            d = DB()
            d.open(p)
            dbs.append(d)
        self.addCleanup(lambda: [d.close() for d in dbs])
        members = [SimpleNamespace(uid=i + 1, name=os.path.basename(p), db=d)
                   for i, (p, d) in enumerate(zip(paths, dbs))]
        det = CaseDetection((m, tl.detect(m.db.session)) for m in members)
        found = sorted((c.database, c.table, c.column, c.kind) for c in det.detected())
        self.assertEqual(found, [(1, "message", "timestamp", "unix_ms"),
                                 (2, "wa_contacts", "last_seen", "unix_s")])
        self.assertEqual(det.of(members[2]).detected(), [])     # settings: no dates
        self.assertIsNotNone(det.get("message", "timestamp", 1))
        self.assertIsNone(det.get("message", "timestamp", 2))
        self.assertEqual(det.tables, 5)


class LookupReadTest(TempDirTest):
    def test_linked_table_read_once_and_values_looked_up(self):
        from database import DB
        from lookups import BrowseLookups, Lookup, Target
        from engine.relations import ROWID
        d = DB()
        d.open(cf.contacts(self.tmp))
        self.addCleanup(d.close)
        t = Target(None, "wa_contacts", "jid", True, "matched by value")
        m, aff, capped, n = BrowseLookups._read(d.session, t, "display_name", 1000)
        self.assertEqual((aff, capped, n), ("TEXT", False, 20))
        lk = Lookup("jid", "raw_string", t, "display_name", "contacts.db › "
                    "wa_contacts.display_name")
        lk.map, lk.affinity = m, aff
        self.assertEqual(lk.value(cf.JIDS[3]), ["Zebracorn Person", 1])
        self.assertEqual(lk.text(cf.JIDS[0]),
                         "%s  → Person 0  (contacts.db › wa_contacts.display_name)"
                         % cf.JIDS[0])
        self.assertIsNone(lk.text(cf.JIDS[19]))            # not in contacts: raw only
        self.assertIsNone(lk.text(None))
        m, _aff, capped, n = BrowseLookups._read(d.session, t, "display_name", 5)
        self.assertEqual((capped, n, len(m)), (True, 5, 5))
        rowid = Target(None, "wa_contacts", ROWID, False, "x")
        m, _aff, _c, _n = BrowseLookups._read(d.session, rowid, "number", 1000)
        self.assertEqual(m[("n", 1)], ["+15550000000", 1])


class CaseWidgetsTest(TempDirTest):
    def test_open_folder_dialog(self):
        from case_ui import OpenFolderDialog
        root = tk_root(self)
        cf.build(self.tmp)
        with open(os.path.join(self.tmp, "readme.txt"), "w") as f:
            f.write("x")
        dlg = OpenFolderDialog(root, self.tmp)
        dlg.wait()
        self.assertEqual(sorted(c.name for c in dlg.candidates),
                         ["contacts.db", "messages.db", "settings.db"])
        self.assertEqual(len(dlg.ticked), 3)                 # all ticked by default
        self.assertEqual(dlg.open_btn.cget("text"), "Open 3 databases")
        dlg.filter_var.set("contacts")
        self.assertEqual(len(dlg.tree.get_children()), 1)
        dlg.set_all(False)
        self.assertEqual(len(dlg.ticked), 2)
        dlg.filter_var.set("")
        dlg.toggle(dlg.tree.get_children()[1])             # messages.db off
        dlg.ok()
        self.assertEqual(len(dlg.result), 1)

    def test_scope_picker(self):
        """One scope control: the button says '3 of 16 databases'; the popover ticks
        databases and groups (at least one stays), presets, 'only for this tab' and
        'follow global scope'."""
        from types import SimpleNamespace
        from scope import ScopePicker, Scopes
        from tests.helpers import off_screen_windows
        root = tk_root(self)
        off_screen_windows(self)
        folders = ["/x/com.a/databases/", "/x/com.a/databases/", "/x/com.b/databases/"]
        members = [SimpleNamespace(uid=i, name="d%d.db" % i, path=folders[i - 1] + "d%d.db" % i,
                                   counts={}, db=SimpleNamespace(ok=True, tables=lambda: []))
                   for i in (1, 2, 3)]
        root.case = SimpleNamespace(active=members[0])
        root.scopes = Scopes(lambda: members)
        changed = []
        p = ScopePicker(root, root, "search", on_change=lambda: changed.append(1))
        t = ScopePicker(root, root, "timeline")
        self.assertIsNone(p.selection())
        self.assertEqual(p.cget("text"), "All 3 databases ▾")
        p.open()
        pop = p._pop
        self.assertEqual([pop.tree.item(i, "text") for i in pop.tree.get_children()],
                         ["☑  com.a", "☑  com.b"])
        db2 = next(i for i, (k, x) in pop._items.items() if k == "db" and x.uid == 2)
        pop.toggle(db2)
        pop.done()
        self.assertEqual(p.selection(), set([1, 3]))
        self.assertEqual([m.name for m in p.members()], ["d1.db", "d3.db"])
        self.assertEqual(p.cget("text"), "2 of 3 databases ▾")
        self.assertEqual(t.cget("text"), "2 of 3 databases ▾")   # both follow the global scope
        # a group with some ticked is partial; ticking it ticks all of its databases
        p.open()
        pop = p._pop
        group = next(i for i, (k, x) in pop._items.items() if k == "group"
                     and pop.tree.item(i, "text").endswith("com.a"))
        self.assertTrue(pop.tree.item(group, "text").startswith("◪"))
        pop.toggle(group)
        self.assertEqual(pop.ticked, set([1, 2, 3]))
        # only for this tab: the timeline keeps the global scope
        pop.preset("active")
        pop.own_var.set(True)
        pop.done()
        self.assertEqual(p.selection(), set([1]))
        self.assertEqual(t.cget("text"), "2 of 3 databases ▾")
        self.assertEqual(p.cget("text"), "d1.db ▾")
        self.assertTrue(p.follow_lbl.winfo_manager())
        p.follow()
        self.assertEqual(p.selection(), set([1, 3]))
        self.assertFalse(p.follow_lbl.winfo_manager())
        # the case file keeps it (by path) and gives it back
        state = root.scopes.to_state()
        s2 = Scopes(lambda: members)
        s2.load_state(state)
        self.assertEqual(s2.selection("search"), set([1, 3]))
        self.assertGreaterEqual(len(changed), 3)
        p.destroy()
        t.destroy()


if __name__ == "__main__":
    unittest.main()
