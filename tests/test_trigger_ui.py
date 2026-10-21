"""Triggers in the UI: the navigator says what a trigger does above its SQL and, for a table,
which triggers fire on it or change it; a recovered record says when the database deletes its
table's rows by itself or logs them elsewhere; an index entry says what it proves."""

import os
import sqlite3

from tests.test_review_ui import AppCase
from tests.test_triggers import CASCADE, LOG


def make(folder):
    path = os.path.join(folder, "calls.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE call_log (_id INTEGER PRIMARY KEY, ts INTEGER);
        CREATE TABLE call_log_participant_v2 (call_log_row_id INTEGER, jid TEXT);
        CREATE TABLE backup_changes (operation TEXT, table_name TEXT, table_row_id INT);
        CREATE TABLE agent_chat_assignment (jid_row_id INTEGER);
        CREATE TABLE plain (a);
        INSERT INTO plain VALUES (1);
    """ + CASCADE + ";\n" + LOG + ";")
    con.close()
    return path


class TriggerUiTest(AppCase):
    def test_navigator_forensics_and_index_entry_notes(self):
        path = make(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            nav = app._navigator
            self.assertTrue(self.pump(lambda: any(n[0] == "trigger"
                                                  for n in nav._nodes.values())))

            def pick(kind, name):
                iid = [i for i, n in nav._nodes.items() if n[0] == kind and n[2] == name][0]
                nav.tree.see(iid)
                nav.tree.focus(iid)
                nav._on_select()
                return nav.sql_text.get("1.0", "end")

            trg = CASCADE.split()[2]
            shown = pick("trigger", trg)
            self.assertIn("What it does (read from the SQL below, never run):", shown)
            self.assertIn("When a row is deleted from call_log (before the delete):", shown)
            self.assertIn("deletes rows of call_log_participant_v2", shown)
            self.assertIn("CREATE TRIGGER " + trg, shown)           # the SQL is kept
            self.assertEqual(nav.sql_text.tag_ranges("note")[0].string, "1.0")
            shown = pick("table", "call_log_participant_v2")
            self.assertIn("Triggers (the database does this by itself):", shown)
            self.assertIn(trg, shown)
            self.assertIn("CREATE TABLE call_log_participant_v2", shown)
            shown = pick("table", "plain")
            self.assertNotIn("Triggers", shown)                     # only when it applies
            # Forensics: a record of a table triggers delete from / log elsewhere
            from engine.forensics import Record
            from engine.forensics.provenance import Provenance, FREEBLOCK
            prov = Provenance(FREEBLOCK, "main", 3, 100, 20)
            fx = app._forensics
            rec = Record("call_log_participant_v2", ["call_log_row_id", "jid"], [7, "x@s"],
                         prov, rowid=4)
            note = fx._record_note(rec)
            self.assertIn("deletes rows of call_log_participant_v2 by itself - 1 trigger", note)
            self.assertIn("may have gone that way", note)
            rec = Record("agent_chat_assignment", ["jid_row_id"], [9], prov, rowid=2)
            note = fx._record_note(rec)
            self.assertIn("add a row to backup_changes", note.replace("adds", "add"))
            rec = Record("plain", ["a"], [1], prov, rowid=9)
            self.assertEqual(fx._record_note(rec), "")
            rec = Record("plain", ["a", "rowid"], [1, 9], prov, rowid=9,
                         flags=("index_entry",), index="idx_a")
            note = fx._record_note(rec)
            self.assertIn("An entry of index idx_a", note)
            self.assertIn("other columns are not in it", note)
            fx._show_record_panel(rec)
            self.assertTrue(fx.rec_note.winfo_manager())
            self.assertEqual(tuple(fx.rec_tree["displaycolumns"]), ("col", "value"))
            # a value read as a date: the Date column shows and every column fits the panel
            dated = Record("plain", ["a", "timestamp"], [1, 1727762495000], prov, rowid=3)
            fx._show_record_panel(dated)
            app.update()
            t = fx.rec_tree
            self.assertEqual(tuple(t["displaycolumns"]), ("col", "value", "date"))
            self.assertIn("2024-10-01", t.set("1", "date"))
            self.assertLessEqual(sum(t.column(c, "width") for c in ("col", "value", "date")),
                                 max(t.winfo_width(), 180) + 2)
            self.assertIn("2024-10-01", fx._rec_hover("1"))
            fx._show_record_panel(None)
            self.assertFalse(fx.rec_note.winfo_manager())
        self.run_app(scenario)
