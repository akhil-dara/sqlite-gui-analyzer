"""Safe parse in the app: the Open menu offers it, the header names it, a case keeps it."""

import os
import sqlite3

from tests.test_review_ui import AppCase


def make_db(path, rows=3):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(a, b)")
    c.executemany("INSERT INTO t VALUES (?, ?)", [(i, "v%d" % i) for i in range(rows)])
    c.execute("CREATE VIEW v AS SELECT a FROM t")
    c.commit()
    c.close()
    return path


class SafeParseUiTest(AppCase):
    def test_open_menu_header_and_case_file(self):
        d1, d2 = os.path.join(self.tmp, "one"), os.path.join(self.tmp, "two")
        os.makedirs(d1)
        os.makedirs(d2)
        p1 = make_db(os.path.join(d1, "a.db"))
        p2 = make_db(os.path.join(d2, "b.db"))

        def scenario():
            app = self.app
            app._fill_open_menu()
            m = app._open_menu
            labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)
                      if m.type(i) == "command"]
            self.assertIn("Open with Safe parse…", labels)
            app.set_safe_parse(p1, True)
            opened = app._open_paths([p1, p2], wait=True)
            self.assertEqual(len(opened), 2)
            self.assertTrue(self.pump(lambda: not app.opening(), 30))
            by = dict((os.path.basename(x.path), x) for x in app.case)
            self.assertEqual(by["a.db"].db.mode, "safe-parse")
            self.assertTrue(by["a.db"].db.session.safe_parse)
            self.assertNotEqual(by["b.db"].db.mode, "safe-parse")
            self.assertEqual(by["a.db"].db.count("t"), 3)
            path = app._save_case()
            self.assertTrue(path)
            from engine.tags import read_case
            saved = dict((os.path.basename(x["path"]), x) for x in read_case(path)["databases"])
            self.assertIs(saved["a.db"]["safe_parse"], True)
            self.assertIs(saved["b.db"]["safe_parse"], False)
            app._refresh_header()
            self.assertIn("1 Safe parse", app._evidence_chip.text)
            self.assertIn("Safe parse", app._evidence_tip.text)
            texts = " ".join(b.short for b in by["a.db"].db.banners())
            self.assertIn("Safe parse", texts)
        self.run_app(scenario)
