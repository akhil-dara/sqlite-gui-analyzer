"""The SQL tab: a result column named and valued like dates reads as dates (the stored
value kept in copies, exports and the tooltip), a header's right-click switches it, an export
adds the date beside the stored value, and the history line says when it is shortened."""

import csv
import os
import shutil
import sqlite3
import tempfile
import tkinter as tk

from tests.test_review_ui import AppCase


def history(folder):
    path = os.path.join(folder, "History")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
    c.executemany("INSERT INTO visits VALUES (?,?,?)",
                  [(i, 1 + i % 20, 13344473607000000 + i * 10 ** 7) for i in range(1, 41)])
    c.commit()
    c.close()
    return path


class SqlDatesTest(AppCase):
    def test_dates_header_switch_export_and_history(self):
        path = history(self.tmp)
        out = tempfile.mkdtemp(prefix="sga_sql_out_")     # never next to the evidence
        self.addCleanup(shutil.rmtree, out, True)
        target = os.path.join(out, "q.csv")
        import app as app_module
        app_module.export_options = lambda *a, **k: {"scope": "rows", "fmt": "csv",
                                                     "blob_mode": "hex",
                                                     "spreadsheet_safe": True}
        app_module.ask_path = lambda *a, **k: target
        self.addCleanup(setattr, app_module, "export_options",
                        __import__("jobs").export_options)
        self.addCleanup(setattr, app_module, "ask_path", __import__("jobs").ask_path)
        sql = ("SELECT id, url, visit_time FROM visits WHERE id > 0 AND visit_time > 0 "
               "AND url > 0 ORDER BY visit_time DESC, id DESC, url DESC LIMIT 1000 OFFSET 0 "
               "-- a long query: more than two hundred characters to see the shortened line")

        def scenario():
            app = self.app
            app._open_db(path, wait=True)
            app._sql_editor.insert("1.0", sql)
            app._sql_run()
            self.assertTrue(self.pump(lambda: "returned" in
                                      app._sql_status_label.cget("text")))
            self.assertEqual(app._sql_dates, {2: "webkit_us"})
            self.assertTrue(app._sql_grid._formatters[2][0](13344473607000000)
                            .startswith("2023"))
            self.assertIn("visit_time shown as dates", app._sql_status_label.cget("text"))
            line = app._sql_hist_listbox.get(0)
            self.assertTrue(line.endswith("…") and len(line) == 200, line)
            # export: the stored value, then the date
            app._sql_export("csv")
            self.assertTrue(self.pump(lambda: not app._jobs and os.path.exists(target)))
            with open(target, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], ["id", "url", "visit_time", "visit_time (date, UTC)"])
            self.assertTrue(rows[1][2].isdigit() and rows[1][3].startswith("2023"))
            # the header switches it off
            m = tk.Menu(app)
            app._sql_header_menu(m, 2)
            labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)
                      if m.type(i) == "command"]
            self.assertIn("Show the stored values (not as dates)", labels)
            idx = [i for i in range(m.index("end") + 1) if m.type(i) == "command" and
                   m.entrycget(i, "label") == "Show the stored values (not as dates)"][0]
            m.invoke(idx)
            self.assertEqual(app._sql_dates, {})
            self.assertNotIn("shown as dates", app._sql_status_label.cget("text"))
        self.run_app(scenario)
