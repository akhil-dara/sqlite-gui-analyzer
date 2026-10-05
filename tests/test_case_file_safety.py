"""A crafted case or settings file: a database colour can only be '#rrggbb' (it ends up in
SVG and HTML attributes), and no network path it lists is checked or opened before the
examiner chooses it (checking a UNC path makes Windows send the user's credentials)."""
import json
import os
import xml.dom.minidom
from unittest import mock

from tests.helpers import TempDirTest
from tests.test_erd import node, spec
from tests.test_linkgraph import hub_links, mk
from engine import evidence as ev
from engine import tags as T
from engine.erd import ErdModel, arrange
from engine.linkgraph import DB, LinkGraph

EVIL = '#123456" onmouseover="alert(1)'
UNC = ["\\\\attacker\\share\\x.db", "//attacker/share/x.db", "\\\\?\\UNC\\attacker\\s\\x.db",
       "\\\\.\\UNC\\attacker\\s\\x.db", "\\??\\UNC\\attacker\\s\\x.db",
       "\\\\.\\pipe\\x", "\\\\?\\Volume{0000}\\x.db", "\\\\localhost\\c$\\x.db"]


class NetworkPathTest(TempDirTest):
    def test_forms(self):
        for p in UNC:
            self.assertTrue(ev.is_network_path(p), p)
        local = [os.path.join(self.tmp, "x.db"), "x.db"]
        if os.name == "nt":
            local += ["C:\\x.db", "\\\\?\\C:\\x.db", "c:/x.db"]
        for p in local:
            with mock.patch.object(ev, "_drive_is_remote", return_value=False):
                self.assertFalse(ev.is_network_path(p), p)
        for p in ("", None, 5):
            self.assertFalse(ev.is_network_path(p))

    def test_mapped_drive(self):
        with mock.patch.object(ev, "_drive_is_remote", side_effect=lambda d: d.upper() == "Z"):
            self.assertTrue(ev.is_network_path("Z:\\cases\\x.db"))
            self.assertTrue(ev.is_network_path("\\\\?\\Z:\\cases\\x.db"))
            self.assertFalse(ev.is_network_path("Y:\\x.db"))

    def test_nothing_is_touched(self):
        with mock.patch("os.stat", side_effect=AssertionError("touched")), \
                mock.patch.object(ev, "_drive_is_remote", return_value=False):
            for p in UNC + ["C:\\x.db"]:
                ev.is_network_path(p)


class _NoNetwork(object):
    """Fails the test when a network path is checked or opened."""

    def __init__(self, test):
        self.test, self.seen = test, []
        self.real_stat, self.real_isfile = os.stat, os.path.isfile

    def stat(self, p, *a, **kw):
        self.check(p)
        return self.real_stat(p, *a, **kw)

    def isfile(self, p):
        self.check(p)
        return self.real_isfile(p)

    def check(self, p):
        p = os.fspath(p) if not isinstance(p, int) else p
        self.seen.append(p)
        if isinstance(p, str) and ev.is_network_path(p):
            raise AssertionError("a network path was touched: %s" % p)

    def __enter__(self):
        self.patches = [mock.patch("os.stat", self.stat), mock.patch("os.path.isfile", self.isfile)]
        for x in self.patches:
            x.start()
        return self

    def __exit__(self, *exc):
        for x in self.patches:
            x.stop()


class ReopenCaseTest(TempDirTest):
    def write_case(self, dbs):
        path = os.path.join(self.tmp, "case.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"format": T.CASE_FORMAT, "version": 1, "databases": dbs}, f)
        return path

    def reopen(self, case_path, answer):
        from app import App
        opened, asked = [], []

        class Fake(object):
            def _open_paths(self, paths, **kw):
                opened.append((list(paths), kw))
                return []

        def ask(title, text):
            asked.append(text)
            return answer
        with _NoNetwork(self) as guard, mock.patch("app.messagebox") as mb:
            mb.askyesno.return_value = True
            App.reopen_case(Fake(), case_path, ask=ask)
        return opened, asked, guard

    def test_network_paths_listed_and_not_checked(self):
        local = os.path.join(self.tmp, "local.db")
        with open(local, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\x00" * 100)
        case = self.write_case([{"path": local, "size": 116}, {"path": UNC[0], "size": 9}])
        # No: the network path is left out, never touched
        opened, asked, guard = self.reopen(case, False)
        self.assertEqual(len(asked), 1)
        self.assertIn(local, asked[0])
        self.assertIn(UNC[0] + "   (network path — not checked)", asked[0])
        self.assertEqual(opened[0][0], [local])
        # Cancel: nothing opens
        opened, asked, guard = self.reopen(case, None)
        self.assertEqual(opened, [])

    def test_a_case_of_local_paths_asks_nothing_extra(self):
        local = os.path.join(self.tmp, "a.db")
        open(local, "wb").close()
        opened, asked, _g = self.reopen(self.write_case([{"path": local, "size": 0}]), True)
        self.assertEqual(asked, [])
        self.assertEqual(opened[0][0], [local])

    def test_reading_a_case_touches_no_listed_path(self):
        case = self.write_case([{"path": p} for p in UNC])
        with _NoNetwork(self):
            data = T.read_case(case)
        self.assertEqual(len(data["databases"]), len(UNC))


class RecentMenuTest(TempDirTest):
    def test_network_entries_are_not_checked(self):
        from tagging import recent_entry
        with _NoNetwork(self):
            for p in UNC:
                label, state = recent_entry(p)
                self.assertTrue(label.endswith("— network path, not checked"), label)
                self.assertEqual(state, "normal")
            label, state = recent_entry(UNC[0], "Case of 2: a, b")
            self.assertEqual(label, "Case of 2: a, b  — network path, not checked")
            gone = os.path.join(self.tmp, "gone.db")
            self.assertEqual(recent_entry(gone)[1], "disabled")


class ColourTest(TempDirTest):
    def test_case_file_colour_must_be_rrggbb(self):
        path = os.path.join(self.tmp, "case.json")
        good = os.path.join(self.tmp, "g.db")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"format": T.CASE_FORMAT, "databases": [
                {"path": good, "color": "#00ff00"}, {"path": good + "2", "color": EVIL},
                {"path": good + "3", "color": 12}]}, f)
        data = T.read_case(path)
        colours = [d["color"] for d in data["databases"]]
        self.assertEqual(colours, ["#00ff00", None, None])
        self.assertEqual(len(data["problems"]), 2)
        self.assertIn("is not #rrggbb", data["problems"][0])
        self.assertIn("is not #rrggbb", data["databases"][1]["color_problem"])

    def test_case_member_colour(self):
        from case import Case

        class FakeDB(object):
            ok = True

            class evidence(object):
                main = "x.db"
        c = Case()
        m = c.add_db(FakeDB(), color=EVIL)
        self.assertRegex(m.color, r"^#[0-9a-fA-F]{6}$")
        self.assertEqual(c.add_db(FakeDB(), color="#abcdef").color, "#abcdef")

    def check_svg(self, text):
        xml.dom.minidom.parseString(text)
        low = text.lower()
        self.assertNotIn("onmouseover", low)
        self.assertNotIn(EVIL, text)

    def test_relationships_svg(self):
        links = [mk("a", "b_id", 'b"q', "_id", kind="declared")]
        specs = {node("a"): spec("b_id INTEGER"), node('b"q'): spec("_id INTEGER pk")}
        text = arrange(ErdModel(links, specs)).svg(colors={DB: EVIL}, title='t" onload="x')
        self.check_svg(text)
        self.assertNotIn('onload="', text)
        self.assertIn("b&quot;q", text)
        ok = arrange(ErdModel(links, specs)).svg(colors={DB: "#123456"})
        self.assertIn('fill="#123456"', ok)

    def test_link_graph_svg(self):
        g = LinkGraph(hub_links() + [mk("x", "y_id", "y", "_id", cross=True, kind="value",
                                        db="contacts.db")])
        g.layout(None)
        self.check_svg(g.svg(colors={"contacts.db": EVIL}))
