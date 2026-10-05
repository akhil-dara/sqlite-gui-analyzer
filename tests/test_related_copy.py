"""Copy with related (engine.related_copy): the rows a bundle holds, batched lookups, caps and
counts, the renderers, the SQL run against a copy of the fixture, and streamed exports."""

import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
from unittest import mock

from tests.helpers import norm
from tests.fixtures import datamap_fixtures as dfx
from tests.fixtures import make_fixtures as fx
from tests.test_session import SessionTestBase
from engine import limits as lim
from engine import related_copy as rc
from engine import timeline as tl
from engine.datamap import Cancelled, md_escape
from engine.relations import relation_map
from engine.schema import Locator


class _Base(SessionTestBase):
    def setUp(self):
        SessionTestBase.setUp(self)
        self.path = dfx.browser(self.tmp)
        self.s = self.open(self.path)
        self.m = relation_map(self.s)
        # the SQL runs against a copy, and exports go to a folder outside the evidence folder
        self.other = tempfile.mkdtemp(prefix="sga_rc_out_")
        self.addCleanup(shutil.rmtree, self.other, True)
        self.copy = os.path.join(self.other, "browser.db")
        shutil.copyfile(self.path, self.copy)

    def run_sql(self, sql, path=None):
        c = sqlite3.connect(path or self.copy)
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def bundle(self, table="visits", rowids=(5,), **kw):
        return rc.related_bundle(self.s, self.m, table, [Locator("rowid", r) for r in rowids],
                                 **kw)


def _key(n):
    return n.locator.value


class BundleTest(_Base):
    def test_one_hop_rows_links_and_evidence(self):
        b = self.bundle()
        self.assertEqual([n.locator for n in b.roots], [Locator("rowid", 5)])
        groups = dict((g.table, g) for g in b.roots[0].related)
        self.assertEqual(set(groups), {"urls", "favicons", "keyword_search_terms"})
        g = groups["urls"]
        self.assertEqual([r.locator.value for r in g.rows], [5])
        self.assertEqual(g.via.text(), "visits.url → urls.id, matched 100%")
        self.assertEqual((g.count, g.more, g.exact), (1, 0, True))
        self.assertIn("both refer to urls.id", groups["favicons"].via.text())
        # a row that refers to this one: visit_source.id REFERENCES visits(id), a declared key
        b7 = self.bundle(rowids=(7,))
        vs = [g for g in b7.roots[0].related if g.table == "visit_source"][0]
        self.assertEqual(vs.via.text(), "visit_source.id → visits.id, declared foreign key, "
                                        "matched 100%")
        self.assertEqual(vs.via.direction, "in")
        self.assertEqual(set(b.tables()), {"visits", "urls", "favicons", "keyword_search_terms"})
        self.assertIn("CREATE TABLE urls", b.schemas["urls"]["sql"])
        self.assertEqual(b.schemas["keyword_search_terms"]["rowid"], "rowid")
        self.assertEqual(b.schemas["visits"]["rowid_alias"], "id")
        self.assertEqual(b.rows, 4)

    def test_dates_converted_beside_the_raw_value(self):
        b = self.bundle()
        self.assertEqual(b.dates[("visits", "visit_time")].kind, "webkit_us")
        self.assertEqual(b.dates[("favicons", "created")].kind, "cocoa_s")
        root = b.roots[0]
        raw = root.values[root.columns.index("visit_time")]
        self.assertEqual(raw, dfx.webkit(dfx.visit_when(4)))
        want = tl.fmt_time(dfx.visit_when(4))
        self.assertEqual(b.date_of("visits", "visit_time", raw), ("webkit_us", want))
        d = rc.node_dict(b.ctx, root)
        self.assertEqual(d["values"][root.columns.index("visit_time")], raw)
        self.assertEqual(d["dates"]["visit_time"]["utc"], want)

    def test_blobs_summarised_hex_only_on_request(self):
        b = self.bundle()
        fav = [g for g in b.roots[0].related if g.table == "favicons"][0].rows[0]
        d = rc.node_dict(b.ctx, fav)
        self.assertIn("bplist", d["blobs"]["image"]["decodes_to"])
        self.assertNotIn("hex", d["blobs"]["image"])
        self.assertIsNone(d["values"][fav.columns.index("image")])
        raw = fav.values[fav.columns.index("image")]
        self.assertNotIn(raw.hex(), b.render("markdown") + b.render("json"))
        bh = self.bundle(include_hex=True)
        self.assertIn(raw.hex(), bh.render("json"))
        self.assertIn(raw.hex(), bh.render("markdown"))

    def test_two_hops_reach_further_and_list_each_row_once(self):
        b1, b2 = self.bundle(hops=1), self.bundle(hops=2)
        self.assertGreater(b2.rows, b1.rows)
        seen = [(n.table, n.locator) for n in b2.roots]
        for _path, g in b2.groups():
            seen.extend((r.table, r.locator) for r in g.rows)
        self.assertEqual(len(seen), len(set(seen)))
        visits = [r.locator.value for p, g in b2.groups() if g.table == "visits" for r in g.rows]
        self.assertEqual(sorted(visits), [25, 45])
        back = [g for p, g in b2.groups() if g.table == "visits"][0]
        self.assertEqual((back.listed, back.exact), (1, False))

    def test_per_link_cap_and_count(self):
        b = self.bundle("urls", (5,), per_link=1)
        g = [g for g in b.roots[0].related if g.table == "visits"][0]
        self.assertEqual((g.count, len(g.rows), g.more, g.capped), (3, 1, 2, True))
        self.assertIn("+2 more, not included: limit related_rows_per_link = 1",
                      b.render("markdown"))
        with mock.patch.dict(lim.RANGES, {"related_count_cap": (1, 10 ** 7)}):
            g = [g for g in self.bundle("urls", (5,), per_link=1,
                                        limits={"related_count_cap": 2}).roots[0].related
                 if g.table == "visits"][0]
        self.assertEqual((g.count, g.at_least), (2, True))
        self.assertEqual(g.more_text(1), "+1 or more more, not included: limit "
                                         "related_rows_per_link = 1")

    def test_many_rows_per_value_switch_to_seeks(self):
        rel_dir = tempfile.mkdtemp(prefix="sga_rc_rel_")
        self.addCleanup(shutil.rmtree, rel_dir, True)
        s = self.open(fx.relations(rel_dir))
        m = relation_map(s)
        light = rc.related_bundle(s, m, "chat", [Locator("rowid", 1)], per_link=30)
        heavy = rc.related_bundle(s, m, "chat", [Locator("rowid", 1)], per_link=1)
        for b, kept in ((light, 30), (heavy, 1)):
            g = [g for g in b.roots[0].related if g.table == "message"][0]
            self.assertEqual((g.count, len(g.rows), g.at_least), (30, kept, False))
        plans = [p for p in heavy.walker.plan(None, "chat") if p.table == "message"]
        self.assertTrue(plans and plans[0].strategy == "seek" and plans[0].heavy)
        self.assertFalse([p for p in light.walker.plan(None, "chat") if p.table == "message"][0].heavy)

    def test_options_are_checked(self):
        for kw in ({"hops": 0}, {"hops": 4}, {"hops": "2"}, {"per_link": 0},
                   {"per_link": 1.5}):
            with self.assertRaises(ValueError):
                self.bundle(**kw)
        with self.assertRaises(ValueError):
            self.bundle().render("xml")
        with self.assertRaises(ValueError):
            rc.export_related(self.s, self.m, "visits", [], io.StringIO(), "html")
        # a bad limit falls back to its default (and is reported), it never fails
        L, problems = lim.validate({"related_rows_per_link": -3, "nope": 1,
                                    "related_hops": True, "map_sample_rows": 7.0})
        self.assertEqual((L["related_rows_per_link"], L["related_hops"], L["map_sample_rows"]),
                         (lim.DEFAULTS["related_rows_per_link"], lim.DEFAULTS["related_hops"],
                          lim.DEFAULTS["map_sample_rows"]))
        self.assertEqual(len(problems), 4)
        self.assertEqual(lim.validate("junk")[0], lim.DEFAULTS)
        # settings.json values apply everywhere; a job's own choice overrides them
        self.addCleanup(lim.reset)
        lim.load({"limits": {"related_rows_per_link": 3}})
        g = [g for g in self.bundle("urls", (5,)).roots[0].related if g.table == "visits"][0]
        self.assertEqual(len(g.rows), 3)
        self.assertEqual(lim.checked({"related_rows_per_link": 0, "related_hops": 1})
                         ["related_rows_per_link"], 3)
        self.assertEqual(sorted(lim.DEFAULTS), sorted(lim.RANGES))
        self.assertEqual(sorted(lim.DEFAULTS), sorted(lim.DESCRIPTIONS))

    def test_many_rows_use_batched_lookups(self):
        self.bundle()                   # the links are checked once (and kept) first
        statements = []
        self.s.conn().set_trace_callback(statements.append)
        try:
            b = rc.related_bundle(self.s, self.m, "visits",
                                  list(r.locator for r in self.s.iter_rows("visits")))
        finally:
            self.s.conn().set_trace_callback(None)
        self.assertEqual(len(b.roots), dfx.VISITS)
        # every visit shows its url: written once, then named as already listed
        urls_groups = [g for n in b.roots for g in n.related if g.table == "urls"]
        self.assertEqual(len(urls_groups), dfx.VISITS)
        self.assertEqual(sum(1 for g in urls_groups if g.rows), dfx.URLS)
        self.assertEqual(urls_groups[-1].listed_text(), "already listed above: urls row 20")
        urls = [q for q in statements if q.startswith("SELECT") and 'FROM "urls" WHERE' in q]
        self.assertEqual(len(urls), 1)          # one IN query for the chunk's 20 urls
        self.assertIn(" IN (", urls[0])

    def test_unindexed_large_target_skipped_with_a_note(self):
        small = {"related_small_table_rows": 5}
        one = self.bundle("urls", (4,), limits=small)
        many = rc.related_bundle(self.s, self.m, "urls", [Locator("rowid", 4)],
                                 total=10 ** 6, limits=small)
        self.assertIn("keyword_search_terms", [g.table for g in one.roots[0].related])
        self.assertTrue(any("is read once" in n for n in one.notes))
        self.assertNotIn("keyword_search_terms", [g.table for g in many.roots[0].related])
        self.assertTrue(any("not followed" in n and "keyword_search_terms" in n
                            for n in many.notes))

    def test_start_from_row_objects(self):
        rows = list(self.s.iter_rows("visits"))[:3]
        b = rc.related_bundle(self.s, self.m, "visits", rows)
        self.assertEqual([n.locator.value for n in b.roots], [1, 2, 3])

    def test_no_related_rows_says_so(self):
        b = self.bundle(dfx.ODD, (1,))
        self.assertEqual(b.roots[0].related, [])
        md = b.render("markdown")
        self.assertIn("No related rows", md)

    def test_keyless_and_missing_rows_are_noted(self):
        b = rc.related_bundle(self.s, self.m, "visits", [Locator("rowid", 999999),
                                                         Locator("ordinal", 1)])
        self.assertEqual(b.roots, [])
        self.assertEqual(b.notes, ["1 selected row not found",
                                   "1 row without a key (rowid or PRIMARY KEY) left out"])

    def test_cancel(self):
        with self.assertRaises(Cancelled):
            self.bundle(cancel=lambda: True)


class RenderTest(_Base):
    def test_json_is_valid_and_nested(self):
        d = json.loads(self.bundle(hops=2).render("json"))
        self.assertEqual(d["format"], rc.BUNDLE_FORMAT)
        self.assertTrue(d["complete"])
        root = d["rows"][0]
        for key in ("table", "row", "columns", "values", "related"):
            self.assertIn(key, root)
        rel = [r for r in root["related"] if r["table"] == "urls"][0]
        self.assertEqual((rel["via"]["from"], rel["via"]["to"]), ("visits.url", "urls.id"))
        self.assertEqual(rel["via"]["matched_pct"], 100.0)
        hop2 = dict((g["table"], g) for g in rel["rows"][0]["related"])
        self.assertEqual(len(hop2["visits"]["rows"]), 2)
        # favicons row 5 came at hop 1 (both refer to urls.id): named, not repeated
        self.assertEqual((hop2["favicons"]["rows"], hop2["favicons"]["already_listed"]),
                         ([], ["5"]))
        self.assertIn("urls", d["schema"])
        self.assertTrue(any(c["kind"] == "webkit_us" for c in d["date_columns"]))
        self.assertTrue(d["links_followed"])

    def test_markdown_escapes(self):
        md = self.bundle().render("markdown")
        self.assertIn("Page \\| &lt;b&gt;5&lt;/b&gt;", md)
        self.assertNotIn("<b>5</b>", md)
        self.assertIn("## Links followed", md)
        self.assertIn("visits.url → urls.id, matched 100%", md)
        self.assertIn("```sql", md)
        lines = [l for l in md.splitlines() if l.startswith("| 5 | 5 | https")]
        self.assertTrue(lines)
        for line in lines:
            # the escaped pipe does not split the row: row + 5 columns
            self.assertEqual(len(re.split(r"(?<!\\)\|", line)) - 2, 6)
        odd = self.bundle(dfx.ODD, (1,)).render("markdown")
        self.assertIn("&lt;script&gt;alert(0)&lt;/script&gt; \\| pipe", odd)
        self.assertNotIn("<script>", odd)
        self.assertEqual(md_escape("a\nb\\c"), "a<br>b\\c")
        self.assertEqual(md_escape("x\\|y `z` &amp; & y"), "x\\\\\\|y \\`z\\` &amp;amp; & y")

    def test_sql_returns_exactly_the_bundle_rows(self):
        for hops in (1, 2):
            for rowids in ((5,), (1, 2, 3, 25)):
                b = self.bundle(rowids=rowids, hops=hops)
                text = b.render("sql")
                queries = rc.chunk_queries(b.ctx, b.roots)
                paths = set(tuple((x.via.table, str(x.via.column), x.table, str(x.column))
                                  for x in p + [g] if isinstance(x, rc.RelGroup))
                            for p, g in b.groups() if g.rows)
                self.assertEqual(len(queries), 1 + len(paths))
                for title, sql, rows, _db in queries:
                    self.assertIn(sql, text)
                    got = self.run_sql(sql)
                    ncols = len(rows[0].columns)
                    self.assertEqual([tuple(norm(v) for v in r[:ncols]) for r in got],
                                     [tuple(norm(v) for v in n.values)
                                      for n in sorted(rows, key=_key)], title)
        rows = self.run_sql(rc.chunk_queries(self.bundle().ctx, self.bundle().roots)[0][1])
        self.assertEqual(rows[0][-1], tl.fmt_time(dfx.visit_when(4)))

    def test_sql_creates_the_tables(self):
        b = self.bundle(hops=2)
        text = b.render("sql")
        creates = text.split("-- Tables involved\n")[1].split("\n\n")[0]
        c = sqlite3.connect(":memory:")
        try:
            c.executescript(creates)
            names = set(r[0] for r in c.execute("SELECT name FROM sqlite_master "
                                                "WHERE type='table'"))
        finally:
            c.close()
        self.assertEqual(names, set(b.schemas))
        self.assertTrue(set(b.tables()) <= names)

    def test_sql_for_a_rowid_table_and_a_without_rowid_key(self):
        b = self.bundle("keyword_search_terms", (3,))
        for title, sql, rows, _db in rc.chunk_queries(b.ctx, b.roots):
            self.assertEqual(len(self.run_sql(sql)), len(rows), title)
        rel_dir = tempfile.mkdtemp(prefix="sga_rc_rel_")
        self.addCleanup(shutil.rmtree, rel_dir, True)
        s = self.open(fx.relations(rel_dir))
        page = s.browse("device", 0, 1)
        bw = rc.related_bundle(s, relation_map(s), "device", [page.rows[0].locator])
        self.assertEqual(bw.roots[0].locator.kind, "pk")
        self.assertIn("user_device", [g.table for g in bw.roots[0].related])
        copy = os.path.join(self.other, "relations.db")
        shutil.copyfile(os.path.join(rel_dir, "relations.db"), copy)
        for title, sql, rows, _db in rc.chunk_queries(bw.ctx, bw.roots):
            self.assertEqual(len(self.run_sql(sql, copy)), len(rows), title)


class ExportTest(_Base):
    def export(self, fmt, rows=None, total=None, **kw):
        out = io.StringIO()
        rows = self.s.iter_rows("visits") if rows is None else rows
        res = rc.export_related(self.s, self.m, "visits", rows, out, fmt, total=total, **kw)
        return res, out.getvalue()

    def test_streams_every_row_in_chunks(self):
        progress = []
        with mock.patch.object(rc, "CHUNK", 7):
            res, text = self.export("json", progress=lambda d, t: progress.append(d),
                                    total=dfx.VISITS)
        self.assertTrue(res.complete)
        d = json.loads(text)
        self.assertEqual((d["rows_written"], len(d["rows"])), (dfx.VISITS, dfx.VISITS))
        self.assertEqual(progress[-1], dfx.VISITS)
        self.assertEqual(len(progress), (dfx.VISITS + 6) // 7)
        # every visit names its url: its row the first time, "already listed" after that
        urls = [g for r in d["rows"] for g in r["related"] if g["table"] == "urls"]
        self.assertEqual(len(urls), dfx.VISITS)
        self.assertEqual(sum(1 for g in urls if g["rows"]), dfx.URLS)
        self.assertEqual(sum(1 for g in urls if g["already_listed"]), dfx.VISITS - dfx.URLS)
        self.assertEqual(res.starts, dfx.VISITS)

    def test_sql_export_runs_per_chunk(self):
        with mock.patch.object(rc, "CHUNK", 25):
            res, text = self.export("sql")
        self.assertTrue(res.complete)
        roots = [q for q in text.split("\n\n") if q.startswith("-- the ") and "rows of visits"
                 in q]
        self.assertEqual(len(roots), 3)
        got = 0
        for block in roots:
            got += len(self.run_sql(block.split("\n", 1)[1]))
        self.assertEqual(got, dfx.VISITS)

    def test_stop_ends_the_output_properly(self):
        for fmt in ("json", "markdown", "sql"):
            stop = [False]
            with mock.patch.object(rc, "CHUNK", 5):
                res, text = self.export(fmt, cancel=lambda: stop[0],
                                        progress=lambda d, t: stop.__setitem__(0, d >= 10))
            self.assertFalse(res.complete, fmt)
            self.assertEqual(res.starts, 10, fmt)
            if fmt == "json":
                d = json.loads(text)
                self.assertEqual((d["complete"], d["rows_written"]), (False, 10))
            else:
                self.assertIn("Stopped after 10 rows", text)
        # stopped before the first row: nothing is written, the caller is told
        with self.assertRaises(Cancelled):
            self.export("markdown", cancel=lambda: True)

    def test_clipboard_limit(self):
        sink = rc.LimitedText(1000)
        with self.assertRaises(rc.TooLarge):
            rc.export_related(self.s, self.m, "visits", self.s.iter_rows("visits"), sink,
                              "markdown")
        small = rc.LimitedText(10 ** 7)
        rc.export_related(self.s, self.m, "visits", [Locator("rowid", 5)], small, "markdown")
        self.assertIn("## visits row 5", small.getvalue())

    def test_file_export_refused_in_the_evidence_folder(self):
        target = os.path.join(self.tmp, "related.json")
        with self.assertRaises(ValueError):
            rc.export_related_file(self.s, self.m, "visits", [Locator("rowid", 5)], target,
                                   "json", is_protected=self.s.evidence.is_protected)
        self.assertFalse(os.path.exists(target))
        ok = os.path.join(self.other, "related.json")
        rc.export_related_file(self.s, self.m, "visits", [Locator("rowid", 5)], ok, "json",
                               is_protected=self.s.evidence.is_protected)
        with open(ok, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["rows_written"], 1)
