"""The Query tab: tables added become cards joined by their trusted link, ticked columns and
conditions build the SQL shown, Run fills the grid (dates shown as dates, stored values
kept), and a saved query loads back."""

import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from tests.helpers import TempDirTest, free_tk


class QueryTabTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        self.data = tempfile.mkdtemp(prefix="sga_query_")
        os.environ["SGA_DATA_DIR"] = self.data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.addCleanup(os.environ.pop, "SGA_DATA_DIR", None)
        from app import App
        try:
            self.app = App()
        except Exception as e:          # noqa: BLE001 - no display
            self.skipTest("no display: %s" % e)
        self.addCleanup(free_tk, self)
        self.app.tags.warn_with_dialogs = False
        self.addCleanup(self.app.destroy)

    def settle(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            if until is not None and until():
                return True
            time.sleep(0.02)
        return until() if until is not None else True

    def history(self):
        path = os.path.join(self.tmp, "History")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
        c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, "
                  "visit_time INTEGER)")
        c.executemany("INSERT INTO urls VALUES (?,?,?)",
                      [(i, "https://site%d.example/" % i, "Site %d" % i) for i in range(1, 21)])
        c.executemany("INSERT INTO visits VALUES (?,?,?)",
                      [(i, 1 + i % 20, 13344473607000000 + i * 10 ** 7) for i in range(1, 41)])
        c.commit()
        c.close()
        return path

    def test_build_run_save_and_load_a_query(self):
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        app._open_db(self.history(), wait=True)
        self.settle(0.3)
        tab = app._query
        app._nb.select(tab)
        self.assertEqual(sorted(tab.tables_tree.get_children()), ["urls", "visits"])
        v = tab.add_table("visits")
        self.assertTrue(tab.model.has_column(v, "visit_time"))      # first columns ticked
        u = tab.add_table("urls")
        self.assertTrue(self.settle(15, lambda: tab.model.joins), "the join is suggested")
        j = tab.model.joins[0]
        self.assertEqual((j.left, j.left_col, j.right, j.right_col), (u, "id", v, "url"))
        self.assertIn("verified by values", tab.status.cget("text"))
        self.assertIn("LEFT JOIN", tab.sql_text.get("1.0", "end"))
        # untick visits.id and visits.url, tick urls.url; visit_time shown as a date
        tab.model.toggle_column(v, "id")
        tab.model.toggle_column(v, "url")
        tab.model.toggle_column(u, "url")
        tab._changed()
        i = [c.column for c in tab.model.columns].index("visit_time")
        tab.cols_tree.selection_set(str(i))
        tab._set_show(i, "webkit_us")
        # a condition through the dialog
        dlg = tab.add_filter(u, "url")
        dlg.op.set("contains")
        dlg._layout()                   # the value area follows the condition chosen
        dlg._entries[0].insert(0, "site2.")
        # the dialog says how many rows the condition keeps before it is added
        self.assertTrue(self.settle(10, lambda: "Matches" in dlg.result.cget("text")),
                        dlg.result.cget("text"))
        self.assertIn("Matches 2 of", dlg.result.cget("text"))
        dlg.ok()
        self.assertEqual(len(tab.model.filters), 1)
        tab.run()
        self.assertTrue(self.settle(15, lambda: tab.grid.row_count() > 0))
        self.assertEqual(tab.grid.row_count(), 2)                   # visits 1 and 21 -> url 2
        cols = tab.grid.columns()
        self.assertEqual(cols, ["visit_time", "url"])
        row = tab.grid.fetch_rows(0, 1)[0][0]
        self.assertIsInstance(row[0], int)                          # the stored value stays
        self.assertIn("2 rows", tab.status.cget("text"))
        self.assertIn("shown as dates (UTC)", tab.status.cget("text"))
        # meanings beside the stored values of a column
        tab.model.toggle_column(u, "id")
        tab._changed()
        k = [c.column for c in tab.model.columns].index("id")
        dlg = tab._meanings(k)
        dlg.text.insert("1.0", "2 = the second site")
        dlg.ok()
        self.assertEqual(tab.model.columns[k].meanings, {"2": "the second site"})
        tab.run()
        self.assertTrue(self.settle(15, lambda: "id" in tab.grid.columns()))
        ci = tab.grid.columns().index("id")
        self.assertEqual(tab.grid._formatters[ci][0](2), "2 (the second site)")
        tab.model.toggle_column(u, "id")
        tab._changed()
        tab.run()
        self.assertTrue(self.settle(15, lambda: "id" not in tab.grid.columns()))
        # export: the rows as shown, the SQL in the manifest
        out = os.path.join(self.data, "q.csv")
        tab.export("csv", out)
        self.assertTrue(self.settle(20, lambda: os.path.exists(out + ".manifest.json")))
        import csv
        with open(out, encoding="utf-8-sig", newline="") as f:
            lines = list(csv.reader(f))
        self.assertEqual(len(lines), 3)                                 # header + 2 rows
        # the date column keeps its stored value; the date text comes next to it
        head = lines[0]
        i = head.index("visit_time")
        self.assertEqual(head[i + 1], "visit_time (date, UTC)")
        row = lines[1]
        self.assertTrue(row[i].isdigit() and row[i + 1][:2] == "20", row)
        with open(out + ".manifest.json", encoding="utf-8") as f:
            self.assertIn("LEFT JOIN", f.read())
        # save, clear, load
        self.assertEqual(tab.save("visits with urls"), "visits with urls")
        sql = tab.model.to_sql()
        tab.clear()
        self.assertEqual(tab.model.to_sql(), "")
        self.assertTrue(tab.load("visits with urls"))
        self.assertEqual(tab.model.to_sql(), sql)
        # the SQL goes to the SQL tab as is
        tab.open_in_sql_tab()
        self.assertEqual(app._sql_editor.get("1.0", "end").strip(), sql)

    def test_a_query_across_two_databases_of_a_case(self):
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        a = os.path.join(self.tmp, "contacts.db")
        c = sqlite3.connect(a)
        c.execute("CREATE TABLE contacts (jid TEXT, name TEXT)")
        c.executemany("INSERT INTO contacts VALUES (?,?)", [("1@s", "Ann"), ("2@s", "Bob")])
        c.commit()
        c.close()
        b = os.path.join(self.tmp, "msgstore.db")
        c = sqlite3.connect(b)
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, jid TEXT, body TEXT)")
        c.executemany("INSERT INTO messages VALUES (?,?,?)",
                      [(1, "1@s", "hi"), (2, "2@s", "yo"), (3, "2@s", "ok")])
        c.commit()
        c.close()
        app._open_paths([a, b], wait=True)
        self.settle(0.5)
        tab = app._query
        app._nb.select(tab)
        groups = tab.tables_tree.get_children()
        self.assertEqual(sorted(tab.tables_tree.item(g, "text") for g in groups),
                         ["contacts.db", "msgstore.db"])
        m = tab.add_table("messages", b)
        k = tab.add_table("contacts", a)
        self.settle(5, lambda: not tab.runner.busy())
        if not tab.model.joins:                         # the value links may not be mapped yet
            tab._join_by_hand(k, "jid", m, "jid")
        tab.model.columns = []
        tab.model.toggle_column(m, "body")
        tab.model.toggle_column(k, "name")
        tab.model.sort = [(m, "id", False)]
        tab._changed()
        shown = tab.sql_text.get("1.0", "end")
        self.assertIn("-- db1 = msgstore.db", shown)
        self.assertIn('"db2"."contacts"', shown)
        tab.run()
        self.assertTrue(self.settle(15, lambda: tab.grid.row_count() > 0), tab.status.cget("text"))
        self.assertEqual([r[0] for r in tab.grid.fetch_rows(0, 3)],
                         [["hi", "Ann"], ["yo", "Bob"], ["ok", "Bob"]])
        self.assertEqual(tab.card_title(k), "contacts.db › contacts")
        # an HTML report of the results; the manifest names both databases
        out = os.path.join(self.data, "q.html")
        tab.export("html", out)
        self.assertTrue(self.settle(20, lambda: os.path.exists(out + ".manifest.json")))
        with open(out, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("Bob", page)
        with open(out + ".manifest.json", encoding="utf-8") as f:
            manifest = f.read()
        self.assertIn("contacts.db", manifest)
        self.assertIn("msgstore.db", manifest)
        tab.open_in_sql_tab()
        self.assertIn("joins several databases", tab.status.cget("text"))

    def test_recovered_rows_are_a_source_joined_to_live_tables(self):
        app = self.app
        app.geometry("1200x800+-4000+0")
        path = os.path.join(self.tmp, "notes.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA secure_delete=OFF")
        c.execute("CREATE TABLE folders (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, folder INTEGER, body TEXT)")
        c.executemany("INSERT INTO folders VALUES (?,?)", [(i, "folder %d" % i)
                                                           for i in range(1, 6)])
        c.executemany("INSERT INTO notes VALUES (?,?,?)",
                      [(i, 1 + i % 5, "note body number %d" % i) for i in range(1, 40)])
        c.execute("DELETE FROM notes WHERE id IN (7, 8, 9)")
        c.commit()
        c.close()
        app._forensics.AUTO_DELAY_MS = 100
        app._open_db(path, wait=True)
        self.assertTrue(self.settle(30, lambda: app._forensics._scanned))
        tab = app._query
        app._nb.select(tab)
        self.settle(0.3)
        names = [tab.tables_tree.item(i, "text") for i in tab.tables_tree.get_children("grec")]
        self.assertEqual(names, ["recovered_notes"])
        f = tab.add_table("folders")
        tab.model.columns = []
        r = tab.add_table("recovered_notes", "~recovered")
        if not self.settle(10, lambda: tab.model.joins):
            tab._join_by_hand(r, "folder", f, "id")
        tab._set_join_kind(0, "INNER")                  # only folders with recovered notes
        tab.model.columns = []
        for a, col in ((r, "id"), (r, "body"), (r, "recovered_state"), (f, "name")):
            tab.model.toggle_column(a, col)
        tab.model.sort = [(r, "id", False)]
        tab._changed()
        self.assertIn("temp.\"recovered_notes\"", tab.sql_text.get("1.0", "end"))
        tab.run()
        self.assertTrue(self.settle(15, lambda: tab.grid.row_count() > 0), tab.status.cget("text"))
        rows = [x[0] for x in tab.grid.fetch_rows(0, tab.grid.row_count())]
        # the deleted notes with the live folder each one was in (their rowids were written
        # over by the freeblock header: unknown, shown as NULL, never guessed)
        self.assertEqual(sorted(row[1:] for row in rows),
                         [["note body number 7", "deleted", "folder 3"],
                          ["note body number 8", "deleted", "folder 4"],
                          ["note body number 9", "deleted", "folder 5"]])

    def test_the_keyboard_path_adds_columns_and_joins(self):
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        app._open_db(self.history(), wait=True)
        tab = app._query
        tab.tables_tree.selection_set("visits")
        tab.add_selected()                              # Enter / Add on the list
        tab.tables_tree.selection_set("urls")
        tab.add_selected()
        self.settle(5, lambda: not tab.runner.busy())
        tab.model.joins = []
        tab.model.columns = []
        tab._changed()
        dlg = tab.join_dialog()
        dlg.boxes[0].set("visits.url")
        dlg.boxes[1].set("urls.id")
        dlg.ok()
        self.assertEqual([j.text() for j in tab.model.joins], ["visits.url = urls.id"])
        dlg = tab.add_column_dialog()
        dlg.boxes[0].set("urls.title")
        dlg.ok()
        dlg = tab.add_column_dialog()
        dlg.boxes[0].set("nope.nothing")
        dlg.ok()                                        # refused, the dialog stays
        self.assertTrue(dlg.winfo_exists())
        dlg.destroy()
        self.assertEqual([(c.alias, c.column) for c in tab.model.columns], [("urls", "title")])
        tab.count_var.set(True)
        tab._changed()
        tab.run()
        self.assertTrue(self.settle(15, lambda: tab.grid.row_count() > 0))
        self.assertEqual(tab.grid.columns(), ["title", "rows"])
        self.assertEqual(tab.grid.row_count(), 20)

    def test_an_unjoined_table_is_said(self):
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        path = os.path.join(self.tmp, "two.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE a (x TEXT)")
        c.execute("CREATE TABLE b (y TEXT)")
        c.executemany("INSERT INTO a VALUES (?)", [("p",), ("q",)])
        c.executemany("INSERT INTO b VALUES (?)", [("r",), ("s",), ("t",)])
        c.commit()
        c.close()
        app._open_db(path, wait=True)
        tab = app._query
        tab.add_table("a")
        tab.add_table("b")
        self.settle(10, lambda: "No trusted link" in tab.status.cget("text"))
        self.assertIn("No trusted link joins b", tab.status.cget("text"))
        self.assertIn("CROSS JOIN", tab.model.to_sql())
        # Run asks first: the counts would multiply; No leaves the query unrun
        from tkinter import messagebox
        from unittest import mock
        self.assertEqual(tab._lone, {"b"})              # its card is marked 'not joined'
        self.assertIn("Not joined: b", tab.summary.cget("text"))
        with mock.patch.object(messagebox, "askyesno", return_value=False) as ask:
            tab.run()
        self.assertTrue(ask.called)
        self.assertIn("Not run: join b first", tab.status.cget("text"))
        self.assertEqual(tab.grid.row_count(), 0)
        with mock.patch.object(messagebox, "askyesno", return_value=True):
            tab.run()
        self.assertTrue(self.settle(15, lambda: tab.grid.row_count() > 0))
        self.assertEqual(tab.grid.row_count(), 6)       # 2 x 3: every pair, as asked

    def test_tables_of_two_databases_are_joined_by_their_values(self):
        # no Relationships run: adding the second table compares the two tables' values
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        a = os.path.join(self.tmp, "wa.db")
        c = sqlite3.connect(a)
        c.execute("CREATE TABLE wa_contacts (_id INTEGER PRIMARY KEY, jid TEXT, "
                  "display_name TEXT)")
        c.executemany("INSERT INTO wa_contacts VALUES (?,?,?)",
                      [(i, "9198%08d@s.whatsapp.net" % i, "Person %d" % i)
                       for i in range(1, 61)])
        c.commit()
        c.close()
        b = os.path.join(self.tmp, "msgstore.db")
        c = sqlite3.connect(b)
        c.execute("CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT)")
        c.executemany("INSERT INTO jid VALUES (?,?,?)",
                      [(i, "9198%08d" % i, "9198%08d@s.whatsapp.net" % i)
                       for i in range(1, 81)])
        c.commit()
        c.close()
        app._open_paths([a, b], wait=True)
        tab = app._query
        self.assertEqual(app.relations.cross_links(), [])
        j = tab.add_table("jid", b)
        w = tab.add_table("wa_contacts", a)
        self.assertTrue(self.settle(30, lambda: not tab._looking))
        self.assertEqual([x.text() for x in tab.model.joins], ["%s.jid = %s.raw_string" % (w, j)])
        self.assertIn("matched by value between databases", tab.model.joins[0].why)

    def test_browse_shows_date_columns_and_remembers_off(self):
        # a column named like a date with no choice yet opens as dates; Off is remembered
        from engine import timeline as tl
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        app._open_db(self.history(), wait=True)
        app._browse_table_var.set("visits")
        app._load_browse_table()
        g = app._browse_grid
        c = g.columns().index("visit_time")
        self.assertTrue(self.settle(15, lambda: c in g._formatters), "shown as dates")
        self.assertTrue(g._formatters[c][0](13344473607000000).startswith("2023"))
        self.assertIsNone(app._browse_dates.choice("visits", "visit_time"))  # nothing stored
        app._browse_dates.set("visits", "visit_time", tl.OFF)
        self.assertNotIn(c, g._formatters)
        app._load_browse_table()                                    # shown again: stays off
        self.settle(2)
        self.assertNotIn(g.columns().index("visit_time"), g._formatters)
        self.assertIn("visit_time", app._browse_dates.turned_off("visits"))

    def test_browse_export_adds_the_dates_shown(self):
        # Browse: a column shown as a date keeps its stored value in an export and adds the
        # date text as a column of its own
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        app._open_db(self.history(), wait=True)
        app._browse_dates.set("visits", "visit_time", "webkit_us")
        extra = app._browse_dates.export_columns("visits", ["id", "url", "visit_time"])
        self.assertEqual([(i, nm) for i, nm, _fn in extra], [(2, "visit_time (date, UTC)")])
        self.assertTrue(extra[0][2](13344473607000000).startswith("2023-"))
        self.assertEqual(app._browse_dates.export_columns("urls", ["id", "url"]), [])

    def chats(self):
        """A messenger's shape: chat and message tied by several verified links (the chat
        a message is in; the chat's last message), jid by three (the chat's contact, the
        account, a message's sender)."""
        path = os.path.join(self.tmp, "msgstore.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT UNIQUE)")
        c.execute("CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER UNIQUE, "
                  "account_jid_row_id INTEGER, last_message_row_id INTEGER)")
        c.execute("CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, "
                  "sender_jid_row_id INTEGER, text_data TEXT)")
        c.executemany("INSERT INTO jid VALUES (?,?)",
                      [(i, "%d@s.example" % (9100 + i)) for i in range(1, 31)])
        c.executemany("INSERT INTO chat VALUES (?,?,?,?)",
                      [(i, i + 1, 1, 10 * i) for i in range(1, 21)])
        c.executemany("INSERT INTO message VALUES (?,?,?,?)",
                      [(i, 1 + (i - 1) // 10, 2 + (i - 1) // 10, "line %d" % i)
                       for i in range(1, 201)])
        c.commit()
        c.close()
        return path

    def test_one_link_per_table_and_the_others_in_the_menu(self):
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        app._open_db(self.chats(), wait=True)
        tab = app._query
        m = tab.add_table("message")
        c = tab.add_table("chat")
        self.assertTrue(self.settle(15, lambda: not tab._looking))
        j = tab.add_table("jid")
        self.assertTrue(self.settle(15, lambda: not tab._looking))
        texts = sorted(x.text() for x in tab.model.joins)
        # the chat a message is in (not ANDed with the chat's last message), and the chat's
        # contact (named for jid) - one link per table
        self.assertEqual(texts, sorted(["%s._id = %s.chat_row_id" % (c, m),
                                        "%s._id = %s.jid_row_id" % (j, c)]))
        self.assertIn("other link", tab.status.cget("text"))
        i = [x.text() for x in tab.model.joins].index("%s._id = %s.jid_row_id" % (j, c))
        alts = [a.text() for a in tab._alternatives(tab.model.joins[i])]
        self.assertIn("%s._id = %s.sender_jid_row_id" % (j, m), alts)
        alt = next(a for a in tab._alternatives(tab.model.joins[i])
                   if a.text() == "%s._id = %s.sender_jid_row_id" % (j, m))
        tab._use_link(i, alt)
        self.assertIn('"jid"."_id" = "message"."sender_jid_row_id"', tab.model.to_sql())
        self.assertNotIn('"jid"."_id" = "chat"."jid_row_id"', tab.model.to_sql())
        self.assertIn("%s._id = %s.jid_row_id" % (j, c),
                      [a.text() for a in tab._alternatives(tab.model.joins[i])])

    def test_run_waits_for_the_links(self):
        app = self.app
        app.geometry("1200x800+-4000+0")
        app._forensics.AUTO_DELAY_MS = 600000
        app._open_db(self.chats(), wait=True)
        tab = app._query
        tab.add_table("message")
        tab.add_table("chat")
        self.assertTrue(tab._looking)
        self.assertIn("finding links", "".join(
            tab.cv.itemcget(i, "text") for i in tab.cv.find_all()
            if tab.cv.type(i) == "text"))
        self.assertNotIn("Not joined", tab.summary.cget("text"))
        tab.run()                       # no 'not joined' question: it waits instead
        self.assertIn("Run starts as soon as the links", tab.status.cget("text"))
        self.assertTrue(self.settle(20, lambda: " rows" in tab.status.cget("text")))
        self.assertEqual(tab.grid.row_count(), 200)     # one line per message, not 200 x 20


if __name__ == "__main__":
    unittest.main()
