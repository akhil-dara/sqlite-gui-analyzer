"""Browse: fields inside a JSON column as derived columns (ƒ name) - shown after the table's
own columns in their colour, said in the status, sortable, remembered per table name."""
import json
import os
import sqlite3

from tests.test_review_ui import AppCase


def make(tmp):
    path = os.path.join(tmp, "derived_ui.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE txn (id INTEGER PRIMARY KEY, data TEXT)")
    for i in range(1, 61):
        con.execute("INSERT INTO txn VALUES (?, ?)", (i, json.dumps(
            {"from": {"name": "A%02d" % (i % 7)}, "to": {"name": "B%02d" % (i % 5)},
             "amount": i * 10})))
    con.commit()
    con.close()
    return path


class BrowseDerivedTest(AppCase):
    def test_fields_become_named_columns_and_are_remembered(self):
        path = make(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._nb.select(app._browse_frame)
            app._browse_table_var.set("txn")
            app._load_browse_table()
            self.assertTrue(self.pump(lambda: app._browse_grid.row_count() > 0, 30))
            bf = app._browse_fields
            # the same key under two parents: two columns, named by the full path
            bf._add("txn", "data", "from.name", ["from.name"])
            bf._add("txn", "data", "to.name", ["to.name"])
            bf._add("txn", "data", "amount", ["amount"])
            bf.apply("txn", reload=True)
            self.assertTrue(self.pump(lambda: app._browse_grid.row_count() > 0, 30))
            g = app._browse_grid
            cols = g.columns()
            self.assertEqual(cols[-3:], ["ƒ from.name", "ƒ to.name", "ƒ amount"])
            self.assertEqual(cols[:3], ["_rid", "id", "data"])
            rows = g.fetch_rows(0, 3)
            self.assertEqual(rows[0][0][-3:], ["A01", "B01", 10])
            self.assertTrue(self.pump(lambda: "derived column" in
                                      app._browse_status.cget("text"), 10))
            self.assertIn("3 derived columns", app._browse_status.cget("text"))
            # drawn in their own colour, header too
            self.assertIsNotNone(g.column_color(len(cols) - 1))
            self.assertIsNone(g.column_color(1))
            # sorted in memory by a derived column
            g.sort_by(cols.index("ƒ amount"), True)
            self.assertTrue(self.pump(lambda: g.fetch_rows(0, 1) and
                                      g.fetch_rows(0, 1)[0][0][-1] == 600, 30))
            # remembered per table name
            saved = app.tags.settings.get("derived_columns")
            self.assertEqual([x[0] for x in saved["txn"]], ["from.name", "to.name", "amount"])
            bf.remove("txn", "to.name")
            self.assertTrue(self.pump(lambda: "ƒ to.name" not in app._browse_grid.columns(), 30))
            self.assertEqual(len(app.tags.settings["derived_columns"]["txn"]), 2)
            bf.set_off("txn")
            self.assertTrue(self.pump(lambda: not any(
                c.startswith("ƒ") for c in app._browse_grid.columns()), 30))
            self.assertNotIn("txn", app.tags.settings.get("derived_columns", {}))
        self.run_app(scenario)
