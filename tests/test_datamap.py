"""The Database Map (engine.datamap): its sections, the generated queries run against a copy of
the fixture, estimated row counts, the renderers (escaping, valid JSON) and the write guard;
the SQL date conversions shared with Copy with related."""

import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from html.parser import HTMLParser

import tests.helpers  # noqa: F401 - puts src/ on sys.path
from tests.fixtures import datamap_fixtures as dfx
from tests.test_session import SessionTestBase
from tests.test_html_report import check_page
from engine import datamap as dm
from engine import timeline as tl
from engine.relations import relation_map


class _Base(SessionTestBase):
    def setUp(self):
        SessionTestBase.setUp(self)
        self.path = dfx.browser(self.tmp)
        self.s = self.open(self.path)
        self.m = relation_map(self.s)
        # queries run against a copy; exports go to a folder outside the evidence folder
        self.other = tempfile.mkdtemp(prefix="sga_dm_out_")
        self.addCleanup(shutil.rmtree, self.other, True)
        self.copy = os.path.join(self.other, "browser.db")
        shutil.copyfile(self.path, self.copy)

    def run_sql(self, sql):
        c = sqlite3.connect(self.copy)
        try:
            cur = c.execute(sql)
            return [d[0] for d in cur.description], cur.fetchall()
        finally:
            c.close()


class DateSqlTest(_Base):
    def test_every_kind_converts_like_the_detector(self):
        dt = datetime(2021, 6, 1, 8, 30, 15)
        c = sqlite3.connect(":memory:")
        try:
            for kind in tl.KINDS:
                raw = tl.from_utc(dt, kind)
                got = c.execute("SELECT " + dm.sql_date("?", kind), (raw,)).fetchone()[0]
                self.assertEqual(got, "2021-06-01 08:30:15", kind)
                self.assertEqual(dm.convert_date(raw, kind)[:19], "2021-06-01 08:30:15", kind)
        finally:
            c.close()
        self.assertEqual(set(dm.SQL_DATE), set(tl.KINDS))

    def test_literals(self):
        self.assertEqual(dm.sql_literal("it's"), "'it''s'")
        self.assertEqual(dm.sql_literal(b"\x01\xff"), "X'01ff'")
        self.assertEqual(dm.sql_literal(None), "NULL")
        self.assertEqual(dm.sql_literal(2.5), "2.5")


class _TextParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.tags, self.scripts = [], 0

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.scripts += tag == "script"


class DatabaseMapTest(_Base):
    def build(self, **kw):
        progress = []
        m = dm.database_map(self.s, self.m, dm.MapOptions(**kw), progress=lambda *a:
                            progress.append(a), tool_version="9.9.9")
        self.assertIsNotNone(m)
        self.assertTrue(progress)
        return m

    def test_weaker_links_only_when_asked(self):
        d = self.build().data
        self.assertEqual(d["links"]["weaker"], [])
        self.assertEqual(d["summary"]["weaker_links"], 1)
        self.assertIn("1 weaker left out", dm.map_markdown(self.build()))

    def test_sections(self):
        m = self.build(weaker=True)
        d = m.data
        tables = dict((t["name"], t) for t in d["tables"])
        self.assertEqual(set(tables), {"urls", "visits", "visit_source", "favicons",
                                       "keyword_search_terms", "settings", "message", dfx.ODD})
        self.assertEqual((tables["visits"]["rows"], tables["visits"]["rows_estimated"]),
                         (dfx.VISITS, False))
        self.assertEqual(tables["urls"]["rowid_alias"], "id")
        self.assertFalse(tables["urls"]["without_rowid"])
        cols = dict((c["name"], c) for c in tables["urls"]["columns"])
        self.assertEqual((cols["title"]["type"], cols["title"]["affinity"]),
                         ("LONGVARCHAR", "TEXT"))
        self.assertEqual(cols["visit_count"]["stored_as"], {"integer": dfx.URLS})
        self.assertEqual([i["name"] for i in tables["visits"]["indexes"]], ["visits_url_index"])
        conf = dict((l["from"], l) for l in d["links"]["confident"])
        self.assertEqual(set(conf), {"visits.url", "visit_source.id", "favicons.url_id",
                                     "keyword_search_terms.url_id"})
        self.assertEqual(conf["visits.url"]["kind"], "verified by values")
        self.assertEqual(conf["visits.url"]["matched_pct"], 100.0)
        self.assertEqual(conf["favicons.url_id"]["kind"], "declared foreign key")
        self.assertEqual([l["from"] for l in d["links"]["weaker"]], ["settings.message_id"])
        dates = dict(((x["table"], x["column"]), x) for x in d["date_columns"]["detected"])
        v = dates[("visits", "visit_time")]
        self.assertEqual(v["kind"], "webkit_us")
        self.assertEqual(v["example_utc"], dm.convert_date(v["example_raw"], "webkit_us"))
        self.assertEqual(v["sql"], "datetime(\"visit_time\" / 1000000 - 11644473600, "
                                   "'unixepoch')")
        self.assertEqual(dates[("favicons", "created")]["kind"], "cocoa_s")
        blobs = dict(((x["table"], x["column"]), x) for x in d["blob_columns"])
        self.assertEqual(set(blobs), {("favicons", "image")})
        self.assertEqual(blobs[("favicons", "image")]["sampled"], 10)
        self.assertTrue(blobs[("favicons", "image")]["decodes_to"][0][0].startswith("bplist"))
        self.assertEqual(d["tool_version"], "9.9.9")
        self.assertEqual(d["evidence"][0]["role"], "main")
        self.assertIn("<svg", m.svg)
        self.assertIn(">visits<", m.svg)
        self.assertEqual(d["samples"], {})

    def test_large_tables_get_an_estimate(self):
        m = self.build(limits={"map_exact_count_rows": 2})
        self.assertTrue(any("map_exact_count_rows" in n for n in m.data["notes"]))
        tables = dict((t["name"], t) for t in m.data["tables"])
        # a linked table was counted while its links were checked: exact; a table with no
        # link, larger than EXACT_COUNT_MAX, gets its largest rowid
        self.assertEqual((tables[dfx.ODD]["rows"], tables[dfx.ODD]["rows_estimated"]),
                         (3, True))
        self.assertEqual((tables["message"]["rows"], tables["message"]["rows_estimated"]),
                         (5, False))
        self.assertTrue(m.data["summary"]["rows_estimated"])
        self.assertIn("≈ 3 rows", dm.map_html(m))
        self.assertIn("≈ 3 rows (estimated: the largest rowid)", dm.map_markdown(m))

    def test_queries_follow_the_links_and_run(self):
        d = self.build().data
        by_base = dict((q["base"], q) for q in d["queries"])
        self.assertEqual(set(by_base), {"visits", "visit_source", "favicons",
                                        "keyword_search_terms"})
        vs = by_base["visit_source"]
        self.assertEqual(vs["tables"], ["visit_source", "visits", "urls"])
        self.assertEqual(vs["joins"], 2)
        for q in d["queries"]:
            _cols, rows = self.run_sql(q["sql"])
            _c, count = self.run_sql('SELECT count(*) FROM "%s"' % q["base"])
            self.assertEqual(len(rows), count[0][0], q["title"])
        cols, rows = self.run_sql(by_base["visits"]["sql"])
        self.assertIn("urls.title", cols)
        self.assertIn("visit_time (UTC)", cols)
        self.assertEqual(rows[0][cols.index("visit_time (UTC)")],
                         tl.fmt_time(dfx.visit_when(0)))

    def test_samples_are_truncated(self):
        d = self.build(sample_rows=2, sample_chars=10).data
        smp = d["samples"]["urls"]
        self.assertEqual(len(smp["rows"]), 2)
        self.assertEqual(smp["rows"][0][smp["columns"].index("url")], "https://ex…")
        fav = d["samples"]["favicons"]
        self.assertTrue(fav["rows"][0][fav["columns"].index("image")].startswith("BLOB "))
        self.assertIn("(UTC ", fav["rows"][0][fav["columns"].index("created")])

    def test_html_is_escaped_self_contained_with_contents(self):
        m = self.build(sample_rows=3, weaker=True)
        h = dm.map_html(m)
        self.assertNotIn("<script>alert", h)
        self.assertIn("odd &lt;table&gt; &amp; name", h)
        self.assertIn("&lt;script&gt;alert(0)&lt;/script&gt;", h)
        for ref in re.finditer(r"(?:src|href)=['\"]([^'\"]+)", h):
            self.assertFalse(ref.group(1).startswith(("http://", "https://")), ref.group(1))
        p = _TextParser()
        p.feed(h)
        self.assertEqual(p.scripts, 1)
        check_page(self, h)
        for tag in ("details", "svg", "nav"):
            self.assertIn(tag, p.tags)
        self.assertIn(self.s.evidence.main, h)
        self.assertIn("Weaker links (1)", h)

    def test_markdown_and_json(self):
        m = self.build(weaker=True)
        md = dm.map_markdown(m)
        for head in ("## Evidence", "## Relationships", "### Weaker links (not trusted)",
                     "## Tables", "## Date columns", "## BLOB columns", "## Queries"):
            self.assertIn(head, md)
        self.assertIn("### odd &lt;table&gt; & name", md)
        self.assertIn("None.", md.split("## Views, triggers and other objects")[1])
        d = json.loads(dm.map_json(m))
        self.assertEqual(d["format"], dm.MAP_FORMAT)
        self.assertTrue(d["diagram_svg"].startswith("<svg"))
        self.assertEqual(d["summary"]["confident_links"], 4)

    def test_limits_cut_is_said(self):
        d = self.build(limits={"map_queries": 1, "map_query_columns": 5,
                               "map_query_joins": 1}).data
        self.assertEqual(len(d["queries"]), 1)
        text = " ".join(d["notes"])
        for name in ("map_queries", "map_query_columns", "map_query_joins"):
            self.assertIn(name, text)
        self.assertIn("map_blob_samples", dm.map_markdown(self.build()))
        for bad in ({"sample_rows": -1}, {"sample_rows": "5"}, {"chain_depth": 0},
                    {"sample_chars": 10 ** 9}):
            with self.assertRaises(ValueError):
                dm.MapOptions(**bad)

    def test_cancel_returns_none(self):
        self.assertIsNone(dm.database_map(self.s, self.m, cancel=lambda: True))

    def test_file_name_and_write_guard(self):
        self.assertEqual(dm.map_file_name("History", "html"), "History - Database Map.html")
        self.assertEqual(dm.map_file_name("a:b/c", "markdown"), "a_b_c - Database Map.md")
        m = self.build()
        target = os.path.join(os.path.dirname(self.path), "History - Database Map.html")
        for fmt in ("html", "markdown", "json"):
            with self.assertRaises(ValueError):
                dm.write_map(target, fmt, m, self.s.evidence.is_protected)
            with self.assertRaises(ValueError):
                dm.write_text(target, "x", self.s.evidence.is_protected)
        self.assertFalse(os.path.exists(target))
        for fmt in ("html", "markdown", "json"):
            out = os.path.join(self.other, dm.map_file_name("browser.db", fmt))
            n = dm.write_map(out, fmt, m, self.s.evidence.is_protected)
            self.assertEqual(os.path.getsize(out), n)
        with open(os.path.join(self.other, "browser.db - Database Map.json"),
                  encoding="utf-8") as f:
            self.assertEqual(json.load(f)["database"], "browser.db")
