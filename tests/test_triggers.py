"""engine.triggers: what triggers do, in words (shapes taken from real phone databases)."""
import os
import sqlite3
import unittest

from tests.helpers import TempDirTest
from engine.triggers import (BLOCKS, CALLS, DELETES, INSERTS, UPDATES, TriggerMap, describe)

CASCADE = ("CREATE TRIGGER call_log_bd_for_call_log_participant_v2_trigger BEFORE DELETE ON "
           "call_log BEGIN DELETE FROM call_log_participant_v2 WHERE call_log_row_id=old._id; END")
PRUNE = ("CREATE TRIGGER recentTransaction_trigger after insert on recentTransaction for each "
         "row when ( SELECT COUNT (*) FROM recentTransaction where type=(select type from "
         "recentTransaction order by _id desc limit 1))>= 3 begin delete from recentTransaction "
         "where _id=(select min(_id) from recentTransaction where type=new.type); end")
LOG = ("CREATE TRIGGER agent_delete_for_backup_changes_trigger AFTER DELETE ON "
       "agent_chat_assignment BEGIN DELETE FROM backup_changes WHERE (table_name = "
       "'agent_chat_assignment') AND (table_row_id = OLD.jid_row_id); INSERT INTO backup_changes"
       "(operation, table_name, table_row_id) VALUES ('DELETE', 'agent_chat_assignment', "
       "OLD.jid_row_id); END")
COUNTER = ("CREATE TRIGGER status_seen AFTER UPDATE OF seen_timestamp ON status_seen_receipt "
           "BEGIN UPDATE status_interactions SET view_count = view_count + 1, last = new.ts "
           "WHERE status_row_id = new.status_row_id; END")
GUARD = ("CREATE TRIGGER dt_delete_protector BEFORE DELETE ON DateTimeMap BEGIN SELECT CASE "
         "WHEN ((SELECT type FROM DateTimeMap WHERE wordgroup = 100) IS NOT NULL) THEN "
         "RAISE (IGNORE) END; END")
REFUSE = ("CREATE TRIGGER no_edit BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, "
          "'ledger rows can''t change'); END")
APPFN = ("CREATE TRIGGER after_delete_on_attachment AFTER DELETE ON attachment BEGIN "
         "SELECT delete_attachment_path(OLD.filename); END")
VIEW = ("CREATE TRIGGER trigger_insert_on_usertag_view INSTEAD OF INSERT ON usertag_view FOR "
        "EACH ROW BEGIN INSERT OR IGNORE INTO tags(tag_type,tag_data) VALUES(0,NEW.d); END")
QUOTED = ('CREATE TRIGGER IF NOT EXISTS "main"."t1" AFTER DELETE ON [my table] BEGIN '
          'WITH x AS (SELECT 1) DELETE FROM "other ""q"" t" WHERE id = old.id; '
          "UPDATE OR IGNORE `stats` SET n = CASE WHEN n > 0 THEN n - 1 ELSE 0 END; END")


class DescribeTest(unittest.TestCase):
    def test_a_cascade_delete(self):
        t = describe(CASCADE)
        self.assertEqual((t.timing, t.event, t.table), ("BEFORE", "DELETE", "call_log"))
        self.assertEqual([(a.verb, a.table) for a in t.actions],
                         [(DELETES, "call_log_participant_v2")])
        self.assertEqual(t.kinds, ["removes rows"])
        self.assertIn("When a row is deleted from call_log (before the delete):", t.summary())
        self.assertIn("deletes rows of call_log_participant_v2 where call_log_row_id=old._id",
                      t.summary())

    def test_pruning_after_an_insert_says_the_table_deletes_its_own_rows(self):
        t = describe(PRUNE)
        self.assertEqual((t.timing, t.event, t.table), ("AFTER", "INSERT", "recentTransaction"))
        self.assertTrue(t.when.startswith("( SELECT COUNT"))
        self.assertEqual(t.actions[0].verb, DELETES)
        self.assertIn("deletes rows of recentTransaction itself", t.action_texts()[0])
        self.assertIn("only if", t.event_text())

    def test_a_change_log_with_the_deleted_rows_values(self):
        t = describe(LOG)
        self.assertEqual([a.verb for a in t.actions], [DELETES, INSERTS])
        ins = t.actions[1]
        self.assertEqual(ins.table, "backup_changes")
        self.assertEqual(ins.columns, ["operation", "table_name", "table_row_id"])
        self.assertTrue(ins.uses_old)
        self.assertEqual(t.kinds, ["removes rows", "copies or logs rows"])
        self.assertIn("with values of the deleted row", t.action_texts()[1])

    def test_a_counter_update_of_named_columns(self):
        t = describe(COUNTER)
        self.assertEqual(t.of_columns, ["seen_timestamp"])
        self.assertEqual(t.actions[0].verb, UPDATES)
        self.assertEqual(t.actions[0].columns, ["view_count", "last"])
        self.assertTrue(t.event_text().startswith(
            "When seen_timestamp of a row of status_seen_receipt changes"))

    def test_raise_ignore_inside_case_end_and_raise_abort_with_its_message(self):
        g = describe(GUARD)
        self.assertEqual([a.verb for a in g.actions], [BLOCKS])
        self.assertEqual(g.actions[0].raise_kind, "IGNORE")
        self.assertIn("silently skips the change", g.action_texts()[0])
        r = describe(REFUSE)
        self.assertEqual((r.actions[0].raise_kind, r.actions[0].message),
                         ("ABORT", "ledger rows can't change"))

    def test_an_app_function_is_said_to_be_outside_the_database(self):
        t = describe(APPFN)
        self.assertEqual(t.actions[0].verb, CALLS)
        self.assertEqual(t.actions[0].functions, ["delete_attachment_path"])
        self.assertIn("not in the database", t.action_texts()[0])
        self.assertEqual(t.kinds, ["calls app functions"])

    def test_instead_of_on_a_view(self):
        t = describe(VIEW)
        self.assertEqual((t.timing, t.event, t.table), ("INSTEAD OF", "INSERT", "usertag_view"))
        self.assertTrue(t.event_text().startswith("Instead of inserting into usertag_view"))
        self.assertEqual(t.actions[0].table, "tags")
        self.assertTrue(t.actions[0].uses_new)

    def test_quoted_names_schema_prefix_cte_and_case_end(self):
        t = describe(QUOTED)
        self.assertEqual((t.name, t.table), ("t1", "my table"))
        self.assertEqual([(a.verb, a.table) for a in t.actions],
                         [(DELETES, 'other "q" t'), (UPDATES, "stats")])
        self.assertEqual(t.actions[1].columns, ["n"])

    def test_not_a_trigger_or_garbage_gives_none(self):
        for sql in ("", "CREATE TABLE t(a)", "CREATE TRIGGER x", "CREATE TRIGGER x AFTER "
                    "DELETE ON", None):
            self.assertIsNone(describe(sql), sql)


class MapTest(TempDirTest):
    def make(self):
        path = os.path.join(self.tmp, "trg.db")
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE call_log (_id INTEGER PRIMARY KEY, ts INTEGER);
            CREATE TABLE call_log_participant_v2 (call_log_row_id INTEGER, jid TEXT);
            CREATE TABLE backup_changes (operation TEXT, table_name TEXT, table_row_id INT);
            CREATE TABLE agent_chat_assignment (jid_row_id INTEGER);
            CREATE TABLE plain (a);
        """ + CASCADE + ";\n" + LOG.replace("agent_delete_for", "acd") + ";")
        con.close()
        return path

    def test_per_table_on_and_changing_and_the_session_map(self):
        from engine.session import Session
        from engine.triggers import for_session
        s = Session.open(self.make(), hash_evidence=False)
        self.addCleanup(s.close)
        m = for_session(s)
        self.assertEqual(sorted(t.name for t in m.triggers),
                         ["acd_backup_changes_trigger", CASCADE.split()[2]])
        self.assertEqual([t.name for t in m.on("CALL_LOG")], [CASCADE.split()[2]])
        dels = m.changing("call_log_participant_v2", DELETES)
        self.assertEqual(len(dels), 1)
        self.assertEqual(m.table_text("plain"), [])
        child = m.table_text("call_log_participant_v2")
        self.assertEqual(len(child), 1)
        self.assertIn("When a row is deleted from call_log", child[0])
        self.assertIn("deletes rows of call_log_participant_v2", child[0])
        self.assertEqual(m.unreadable, [])

    def test_recovered_triggers_are_marked_and_unreadable_ones_listed(self):
        class E(object):
            def __init__(self, name, sql):
                self.type, self.name, self.sql = "trigger", name, sql
        m = TriggerMap([E("bad", "CREATE TRIGGER bad garbage")], [("old_one", CASCADE)])
        self.assertEqual(m.unreadable, [("bad", "live")])
        self.assertEqual(m.named("old_one").source, "recovered")
        self.assertIn("[recovered]", m.table_text("call_log")[0])


if __name__ == "__main__":
    unittest.main()


class EffectsTest(unittest.TestCase):
    def test_a_tables_own_trigger_names_the_tables_it_changes_and_the_tally(self):
        from engine.triggers import tally, tally_text

        class E(object):
            def __init__(self, sql):
                self.type, self.name, self.sql = "trigger", sql.split()[2], sql
        m = TriggerMap([E(s) for s in (CASCADE, LOG, GUARD, APPFN, PRUNE)])
        line = m.table_text("agent_chat_assignment")[0]
        self.assertIn("backup_changes: deletes rows, adds rows (with the deleted row's values)",
                      line)
        self.assertIn("recentTransaction itself: deletes rows",
                      m.table_text("recentTransaction")[0])
        self.assertIn("can refuse or skip the change", m.table_text("DateTimeMap")[0])
        self.assertIn("calls the app's delete_attachment_path()", m.table_text("attachment")[0])
        c = tally(m)
        self.assertEqual((c["total"], c["deletes"], c["logs"], c["blocks"], c["calls"]),
                         (5, 3, 1, 1, 1))
        self.assertEqual(tally_text(c), "5 triggers: 3 delete rows by themselves, 1 copy or "
                                        "log rows, 1 can refuse a change, 1 call app functions")
        self.assertEqual(tally_text(tally(TriggerMap([]))), "")
