"""Browse: a calculated column (ƒ= name) from a formula - after the table's columns, in the
derived colour, sortable, reading other columns of the row, remembered per table name,
edited and removed from the header menu; the calculator dialog's formula editor."""
import os
import sqlite3
import tkinter as tk

from tests.test_review_ui import AppCase


def make(tmp):
    path = os.path.join(tmp, "calc_ui.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE txn (id INTEGER PRIMARY KEY, amount INTEGER, qty INTEGER, "
                "name TEXT)")
    con.executemany("INSERT INTO txn VALUES (?, ?, ?, ?)",
                    [(1, 34005, 2, "kadambari"), (2, 1250, 3, "raghu"), (3, None, 1, "x"),
                     (4, 99900, 4, "venkat")])
    con.commit()
    con.close()
    return path


class BrowseCalcTest(AppCase):
    def test_formula_column_lifecycle(self):
        path = make(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._nb.select(app._browse_frame)
            app._browse_table_var.set("txn")
            app._load_browse_table()
            self.pump(lambda: False, 0.4)
            calcs = app._browse_calcs
            calcs._add("txn", "rupees", "amount", [["expr", "round(x / 100, 2)"]])
            self.pump(lambda: False, 0.4)
            g = app._browse_grid
            cols = g.columns()
            self.assertEqual(cols[-1], "ƒ= rupees")
            c = len(cols) - 1
            vals = [v[c] for v, _f in g.source.rows(0, 4)]
            self.assertEqual(vals, [340.05, 12.5, None, 999])
            self.assertEqual(g.column_color(c), app._browse_grid.column_color(c))
            self.assertIsNotNone(g.column_color(c))
            self.assertIn("round(x / 100, 2)", calcs.tooltip("ƒ= rupees"))
            # a formula reading another column of the row, and text
            calcs._add("txn", "total", "amount", [["expr", "x / 100 * qty"]])
            calcs._add("txn", "who", "name", [["expr", "upper(left(x, 3))"]])
            self.pump(lambda: False, 0.4)
            g = app._browse_grid
            cols = g.columns()
            rows = [v for v, _f in g.source.rows(0, 4)]
            self.assertEqual([r[cols.index("ƒ= total")] for r in rows],
                             [680.1, 37.5, None, 3996])
            self.assertEqual([r[cols.index("ƒ= who")] for r in rows],
                             ["KAD", "RAG", "X", "VEN"])
            # sorting on the calculated column (in memory)
            g.source.sort("ƒ= total", True)
            top = g.source.rows(0, 1)[0][0]
            self.assertEqual(top[cols.index("id")], 4)
            # remembered in the settings, per table name
            self.assertEqual([c[0] for c in app.tags.settings["calc_columns"]["txn"]],
                             ["rupees", "total", "who"])
            # the header menu offers edit and remove on a calculated column, Calculate on
            # a table column
            m = tk.Menu(app, tearoff=0)
            app._on_browse_header_menu(m, cols.index("ƒ= total"))
            labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)
                      if m.type(i) in ("command", "cascade")]
            self.assertTrue(any(l.startswith("Edit the calculation of") for l in labels))
            self.assertTrue(any(l.startswith("Remove the calculated column") for l in labels))
            m2 = tk.Menu(app, tearoff=0)
            app._on_browse_header_menu(m2, cols.index("amount"))
            labels = [m2.entrycget(i, "label") for i in range(m2.index("end") + 1)
                      if m2.type(i) in ("command", "cascade")]
            self.assertTrue(any(l.startswith("Calculate from amount") for l in labels))
            # edit: the steps change and the name too
            calcs._replace("txn", "total", "total2", [["expr", "x * qty"]])
            self.pump(lambda: False, 0.4)
            cols = app._browse_grid.columns()
            self.assertIn("ƒ= total2", cols)
            self.assertNotIn("ƒ= total", cols)
            # remove one, then all
            calcs.remove("txn", "who")
            self.pump(lambda: False, 0.4)
            self.assertNotIn("ƒ= who", app._browse_grid.columns())
            calcs.remove_all("txn")
            self.pump(lambda: False, 0.4)
            self.assertFalse([c for c in app._browse_grid.columns() if c.startswith("ƒ=")])
            self.assertNotIn("txn", app.tags.settings.get("calc_columns", {}))
        self.run_app(scenario)

    def test_calculator_dialog_formula_editor(self):
        path = make(self.tmp)

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._nb.select(app._browse_frame)
            app._browse_table_var.set("txn")
            app._load_browse_table()
            self.pump(lambda: False, 0.4)
            dlg = app._browse_calcs.dialog("txn", "amount")
            self.assertIsNotNone(dlg)
            dlg.attributes("-alpha", 0.0)
            self.pump(lambda: False, 0.3)
            self.assertEqual(len(dlg.samples), 4)
            self.assertIn("qty", dlg.others)
            dlg.add("expr")                 # the Formula step: its editor appears
            self.pump(lambda: False, 0.3)
            box = next(w for w in dlg._editors if isinstance(w, tk.Text))
            self.assertEqual(box.get("1.0", "end-1c"), "x / 100")
            box.delete("1.0", "end")
            box.insert("1.0", "x +")
            box.event_generate("<KeyRelease>")
            self.pump(lambda: False, 0.5)
            self.assertIn("disabled", dlg.ok_btn.state())
            self.assertIn("cannot be used", dlg.summary.cget("text"))
            box.delete("1.0", "end")
            box.insert("1.0", "round(x / 100 * qty, 2)")
            box.event_generate("<KeyRelease>")
            self.pump(lambda: False, 0.5)
            self.assertNotIn("disabled", dlg.ok_btn.state())
            shown = [dlg.preview.item(i, "values") for i in dlg.preview.get_children()]
            self.assertEqual(shown[0][1], "680.1")
            self.assertEqual(shown[2][1], "NULL")
            dlg.name_var.set("total")
            dlg.ok()
            self.pump(lambda: False, 0.5)
            cols = app._browse_grid.columns()
            self.assertIn("ƒ= total", cols)
            rows = [v for v, _f in app._browse_grid.source.rows(0, 4)]
            self.assertEqual(rows[1][cols.index("ƒ= total")], 37.5)
        self.run_app(scenario)
