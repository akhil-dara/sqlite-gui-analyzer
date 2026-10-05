"""The Database Map of a whole case: every database's sections, the links between databases
(matched by value), one diagram with the databases' colours; HTML, Markdown and JSON."""

import json
import os
import re
import shutil
import tempfile
from html.parser import HTMLParser

from tests.helpers import TempDirTest
from tests.test_html_report import check_page
from tests.fixtures import case_fixtures as cf
from engine import datamap as dm
from engine.crossdb import find_links
from engine.relations import relation_map
from engine.session import Session

COLORS = ["#1f77b4", "#d62728", "#2ca02c"]


class _Ids(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.ids, self.hrefs = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if a.get("href", "").startswith("#"):
            self.hrefs.append(a["href"][1:])


class CaseMapTest(TempDirTest):
    def setUp(self):
        TempDirTest.setUp(self)
        paths = cf.build(self.tmp)
        self.sessions = [Session.open(p, hash_evidence=False) for p in paths]
        for s in self.sessions:
            self.addCleanup(s.close)
        self.cross = find_links([(i + 1, s) for i, s in enumerate(self.sessions)])
        self.dbs = [dm.MapDatabase(i + 1, n, s, color=c) for i, (n, s, c) in enumerate(
            zip(["messages.db", "contacts.db", "settings.db"], self.sessions, COLORS))]
        self.out = tempfile.mkdtemp(prefix="sga_cm_out_")
        self.addCleanup(shutil.rmtree, self.out, True)

    def build(self, **kw):
        stages = []
        cm = dm.case_map(self.dbs, self.cross.links, dm.MapOptions(**kw),
                         progress=lambda *a: stages.append(a[0]), tool_version="9.9.9",
                         cross_note=self.cross.limits_text())
        self.assertIsNotNone(cm)
        self.assertTrue(any(s.startswith("contacts.db (2 of 3): ") for s in stages))
        return cm

    def test_sections_links_and_diagram(self):
        cm = self.build()
        d = cm.data
        self.assertEqual([x["name"] for x in d["databases"]],
                         ["messages.db", "contacts.db", "settings.db"])
        self.assertEqual(d["summary"]["databases"], 3)
        got = sorted((l["from"], l["to"]) for l in d["cross_links"]["confident"])
        self.assertEqual(got, [("messages.db › jid.raw_string", "contacts.db › wa_contacts.jid"),
                               ("messages.db › message.key_remote_jid",
                                "contacts.db › wa_contacts.jid")])
        self.assertTrue(all(l["kind"] == "matched by value" for l in
                            d["cross_links"]["confident"]))
        self.assertEqual(len(cm.maps), 3)
        # the diagram: every database's tables, named, outlined in its colour
        self.assertIn("messages.db › message", cm.svg)
        self.assertIn("contacts.db › wa_contacts", cm.svg)
        self.assertIn('stroke="%s"' % COLORS[1], cm.svg)
        self.assertIn("#c25100", cm.svg)                 # links between databases
        for fmt in ("html", "markdown", "json"):
            p = os.path.join(self.out, dm.case_file_name(d["databases"], fmt))
            self.assertTrue(p.endswith("Case of 3 databases - Database Map" +
                                       dm.MAP_EXTENSIONS[fmt]))
            dm.write_map(p, fmt, cm, self.sessions[0].evidence.is_protected)
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        self.assertTrue(j["case"])
        self.assertEqual([m["database"] for m in j["database_maps"]],
                         ["messages.db", "contacts.db", "settings.db"])

    def test_html_anchors_are_unique_and_resolve(self):
        h = dm.render_map(self.build(sample_rows=2), "html")
        p = _Ids()
        p.feed(h)
        self.assertEqual(len(p.ids), len(set(p.ids)))
        self.assertTrue(set(p.hrefs) <= set(p.ids), sorted(set(p.hrefs) - set(p.ids))[:5])
        self.assertEqual(len(re.findall("<script>", h)), 1)
        check_page(self, h)
        for name in ("messages.db", "contacts.db", "settings.db"):
            self.assertIn(">%s</h2>" % name, h)
        self.assertIn("Links between databases", h)

    def test_markdown_and_weaker(self):
        cm = self.build(weaker=True)
        md = dm.render_map(cm, "markdown")
        self.assertIn("# Case of 3 databases - Database Map", md)
        self.assertIn("## Links between databases", md)
        self.assertIn("## contacts.db - Database Map", md)      # each database, one level down
        self.assertIn("### Tables", md)
        # a single-database map is unchanged
        single = dm.database_map(self.sessions[1], relation_map(self.sessions[1]))
        self.assertIn("# contacts.db - Database Map", dm.render_map(single, "markdown"))
        with self.assertRaises(ValueError):
            dm.render_map(cm, "pdf")
        with self.assertRaises(ValueError):
            dm.case_map([], [])

    def test_cancel(self):
        self.assertIsNone(dm.case_map(self.dbs, self.cross.links, cancel=lambda: True))
