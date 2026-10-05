"""The one HTML report design (engine.html_report): well-formed, self-contained pages, every
value escaped, embedded rows that read back exactly, bounded memory on 200,000 rows, part files
above the limit, a sane script, the link-state format mirrored in Python, the palettes'
contrast and the responsive stylesheet."""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import tracemalloc
import unittest
from collections import OrderedDict
from html.parser import HTMLParser

from tests.helpers import TempDirTest
from engine import export as ex
from engine import html_report as hr
from engine import limits
from engine.fileformat.record import InvalidText
from engine.html_report_assets import (CONTRAST_PAIRS, CSS, FILTER_JS, JS, STATE_JS, TOKENS,
                                       VALUES_JS)
from engine.tags import PartialBlob

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}
HOSTILE = ["<script>alert(1)</script>", "</script><script>alert(2)</script>",
           "<!-- x --><img src=x onerror=alert(3)>", "\"'><b>quote</b>", "a & b &amp; c",
           "tab\tnew\nline\rcr", "ctl \x01\x02\x07\x08\x0b\x0c\x0e\x1f\x7f end", "nul \x00 end",
           "\u2028\u2029 line separators", "unicode \u00e9\u4e2d\u0416 \u05d0",
           "<svg onload=alert(4)>", "javascript:alert(5)", "http://evil.test/x.js",
           "{{template}} %s %(x)s"]
_NODE = shutil.which("node")


class _Page(HTMLParser):
    """Parses a page, checking that every non-void element is closed in order."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.stack, self.errors, self.scripts, self.ids, self.hrefs, self.srcs = \
            [], [], [], [], [], []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if tag == "script":
            self.scripts.append(a)
        if "id" in a:
            self.ids.append(a["id"])
        for k in ("href", "src", "action", "srcset", "poster", "data", "formaction", "xlink:href"):
            if a.get(k):
                (self.hrefs if k in ("href", "xlink:href") else self.srcs).append(a[k])
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if "id" in a:
            self.ids.append(a["id"])
        for k in ("href", "src", "xlink:href"):
            if a.get(k):
                (self.hrefs if k != "src" else self.srcs).append(a[k])

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append("</%s> without an open element at %s" % (tag, self.getpos()))
            return
        top, pos = self.stack.pop()
        if top != tag:
            self.errors.append("</%s> closes <%s> opened at %s (now %s)" % (
                tag, top, pos, self.getpos()))


def check_page(test, text, internal_links=True):
    """A page of the report design: well-formed, one script of ours (the others are JSON
    data), nothing loaded from anywhere, ids unique, '#' links resolving, no raw control
    characters. Returns the parser."""
    test.assertTrue(text.startswith("<!DOCTYPE html>"))
    for need in ('<meta charset="utf-8">', 'name="viewport"', "Content-Security-Policy",
                 "default-src 'none'"):
        test.assertIn(need, text)
    p = _Page()
    p.feed(text)
    p.close()
    test.assertEqual(p.errors, [])
    test.assertEqual([t for t, _pos in p.stack], [])
    runnable = [s for s in p.scripts if s.get("type") != "application/json"]
    test.assertEqual(len(runnable), 1, p.scripts)
    test.assertEqual(runnable[0], {})
    test.assertEqual(text.count("<script>"), 1)
    for s in p.scripts:
        test.assertNotIn("src", s)
    remote = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:|//)", re.I)
    for v in p.hrefs:
        test.assertFalse(remote.match(v), v)
    for v in p.srcs:
        test.assertTrue(v.startswith("data:image/"), v[:60])
    low = text.lower()
    test.assertNotIn("@import", low)
    for m in re.finditer(r"url\(\s*([^)]*)\)", text):
        test.assertTrue(m.group(1).strip("'\" ").startswith("#"), m.group(0))
    test.assertIsNone(re.search(r"<(?:link|iframe|object|embed|base|form)\b", low))
    test.assertEqual(len(p.ids), len(set(p.ids)), sorted(i for i in p.ids if p.ids.count(i) > 1))
    if internal_links:
        missing = [h[1:] for h in p.hrefs if h.startswith("#") and h[1:] not in set(p.ids)]
        test.assertEqual(missing, [])
    test.assertIsNone(re.search("[\x00-\x08\x0b\x0c\x0e-\x1f]", text))
    return p


def rows_of(text):
    return hr.read_rows(text)


def same(a, b):
    """Values equal as written (nan == nan; a PartialBlob with its size and hash; True is
    written as 1, as SQLite stores it)."""
    if isinstance(a, bool):
        a = int(a)
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if type(a) is not type(b) and not (isinstance(a, (list, tuple)) and isinstance(b, list)) \
            and not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    if isinstance(a, PartialBlob):
        return bytes(a) == bytes(b) and a.size == b.size and a.sha256 == b.sha256
    if isinstance(a, float):
        return repr(a) == repr(b)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    return a == b


def _partial():
    pb = PartialBlob(b"\x00\x01head")
    pb.size, pb.sha256 = 5000000, "ab" * 32
    return pb


SAMPLE = [
    [1, "plain", 1.5, None, b"\x00\x01\xfe\xff", True],
    [2 ** 60, "big int", 1.0, -0.0, b"", False],
    [-(2 ** 63), "neg", float("inf"), float("-inf"), InvalidText(b"ok\xff\xfeend"), 0],
    [0, "", float("nan"), 1e300, _partial(), 1],
    [9007199254740993, "2**53+1", 1e-05, 123456789.125, [1, "x", None, b"\x01"],
     {"k": b"\x02", "n": None}],
] + [[i, h, i / 3.0, None, h.encode("utf-8", "surrogatepass"), None]
     for i, h in enumerate(HOSTILE)]


def _info(rows=None):
    return ex.provenance("9.9.9", [("History", [{
        "role": "main", "path": "C:/evidence/<History>.db", "size": 12345, "mtime_ns": 10 ** 18,
        "mtime_utc": "2001-09-09 01:46:40 UTC", "sha256": "5e" * 32}])],
        "Browse table 'urls' <x>", "all rows", "title contains \"<b>\"",
        ["id", "text", "real", "other", "blob", "flag"], rows)


class PageTest(TempDirTest):
    def report(self, rows, **kw):
        rep = hr.Report("Report <script>alert('t')</script>", provenance=_info(),
                        case_name="Case <i>7</i> & co", subtitle="sub \x01 title")
        rep.add_section("Rows <b>1</b>", text="Some text <b>bold</b> \x02.")
        rep.add_table(kw.pop("name", "urls </script>"), ["id", "text <script>", "real", "other",
                                                          "blob", "flag"], rows, **kw)
        return rep

    def test_well_formed_self_contained_and_escaped(self):
        text = self.report(SAMPLE).render()
        p = check_page(self, text)
        for tag in ("header", "nav", "main", "section", "h1", "h2", "table", "thead"):
            self.assertIn(tag, p.tags)
        # every hostile value and name is escaped: no markup of theirs made it in
        for bad in ("<script>alert", "<b>quote</b>", "<img src=x", "<svg onload", "<i>7</i>",
                    "<b>bold</b>", "<b>1</b>"):
            self.assertNotIn(bad, text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertIn("Case &lt;i&gt;7&lt;/i&gt; &amp; co", text)
        self.assertIn("\\x01", text)                      # control characters shown, not raw
        # inside the JSON data no '<' at all: nothing can close the script element
        for block in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', text,
                                re.S):
            self.assertNotIn("<", block)
            json.loads(block)
        # the provenance: evidence with its hash, tool and version, UTC time, scope, filters
        for need in ("SHA-256 " + "5e" * 32, "SQLite GUI Analyzer 9.9.9", " UTC",
                     "all rows", "title contains &quot;&lt;b&gt;&quot;",
                     "12,345 bytes", "Complete"):
            self.assertIn(need, text)

    def test_rows_round_trip_exactly(self):
        for mode in ("hex", "base64"):
            text = self.report(iter(SAMPLE), blob_mode=mode).render()
            (t,) = rows_of(text).values()
            self.assertEqual(len(t["rows"]), len(SAMPLE))
            for want, got in zip(SAMPLE, t["rows"]):
                self.assertTrue(same(want, got), (mode, want, got))
        text = self.report(iter(SAMPLE), blob_mode="summary").render()
        (t,) = rows_of(text).values()
        got = t["rows"][0][4]
        self.assertEqual(got["size"], 4)
        self.assertEqual(len(got["sha256"]), 64)
        self.assertEqual(t["rows"][3][4]["sha256"], "ab" * 32)   # a partial BLOB: its hash

    def test_no_emoji_in_the_template(self):
        text = self.report([[1, "a", 1.0, None, b"x", 0]]).render()
        pict = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
        self.assertIsNone(pict.search(text))
        self.assertIsNone(pict.search(JS + CSS))

    def test_dates_badges_tags_and_detail(self):
        rows = [[i, 1700000000000 + i * 3600000, "high" if i % 2 else "low", ["Hot", "<Odd>"],
                 "note %d" % i] for i in range(50)]
        rep = hr.Report("R", provenance=_info())
        rep.add_section("S")
        rep.add_table("t", ["id", "sent_ms", "confidence", "Tags", "Note"], rows,
                      tags=("Tags", {"Hot": "#DC2626", "<Odd>": "red;url(x)"}), notes="Note",
                      detail={"prov": ["confidence"], "path": ["db", {"col": "id"}]})
        text = rep.render()
        check_page(self, text)
        (t,) = rows_of(text).values()
        m = t["meta"]
        self.assertEqual(m["dates"], [[1, "unix_ms", "Unix milliseconds", 5]])
        self.assertEqual(m["badges"]["2"], {"low": "bad", "high": "ok"})
        self.assertEqual(m["tags"]["col"], 3)
        self.assertEqual(m["tags"]["colors"]["<Odd>"], ["#8993a4", "#0F172A"])   # sanitised
        self.assertEqual(m["detail"], {"prov": [2], "path": ["db", 0]})
        self.assertNotIn("url(x)", text)
        self.assertIn("Date range (UTC)", text)
        self.assertIn("2023-11-14 22:13:20", text)
        self.assertIn("Activity over time", text)
        self.assertIn('<span class="chip" style="background:#DC2626;color:', text)
        self.assertIn("Confidence", text)

    def test_protected_and_cancel(self):
        target = os.path.join(self.tmp, "r.html")
        with self.assertRaises(hr.ReportError):
            self.report(SAMPLE).write(target, protected=lambda p: True)
        self.assertEqual(os.listdir(self.tmp), [])
        seen = []
        res = self.report(([i, "x", 0.5, None, b"", 0] for i in range(5000))).write(
            target, cancel=lambda: len(seen) > 0, progress=seen.append, every=1200)
        self.assertFalse(res.complete)
        self.assertEqual(res.rows, 1200)
        self.assertEqual(res.stopped, "by the user after 1,200 rows")
        with open(target, encoding="utf-8") as f:
            text = f.read()
        check_page(self, text)
        self.assertIn("INCOMPLETE", text)
        self.assertIn("stopped by the user after 1,200 rows", text)
        self.assertEqual(len(rows_of(text)["t-urls-script"]["rows"]), 1200)

    def test_split_into_parts_with_a_small_limit(self):
        target = os.path.join(self.tmp, "big.html")
        n = 1050
        rows = ([i, "row %d" % i, i * 0.5, None, b"\x01", i % 2] for i in range(n))
        rep = hr.Report("Big", provenance=_info(), limits={"html_rows_per_part": 400,
                                                           "html_chunk_rows": 64,
                                                           "html_print_rows": 10})
        rep.add_section("Rows")
        rep.add_table("big", ["id", "text", "real", "other", "blob", "flag"], rows)
        res = rep.write(target, protected=lambda p: os.path.basename(p) == "never.html")
        self.assertTrue(res.complete)
        self.assertEqual(res.rows, n)
        names = [os.path.basename(p["path"]) for p in res.parts]
        self.assertEqual(names, ["big_part002.html", "big_part003.html"])
        self.assertEqual(sorted(os.listdir(self.tmp)), ["big.html"] + names)
        got = []
        for path in [target] + [p["path"] for p in res.parts]:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            check_page(self, text)
            (t,) = rows_of(text).values()
            got.extend(r[0] for r in t["rows"])
            self.assertLessEqual(len(t["rows"]), 400)
        self.assertEqual(got, list(range(n)))
        with open(target, encoding="utf-8") as f:
            main = f.read()
        self.assertIn("html_rows_per_part", main)
        self.assertIn("big_part003.html", main)
        for p in res.parts:
            self.assertIn(p["sha256"], main)
            self.assertEqual(p["sha256"], hr._hash(p["path"]))
        with open(res.parts[-1]["path"], encoding="utf-8") as f:
            last = f.read()
        self.assertIn("rows 801 to 1,050", last)
        self.assertIn('href="big.html"', last)
        # a part file inside the evidence is refused before it is written
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        rep = hr.Report("Big", limits={"html_rows_per_part": 10})
        rep.add_table("big", ["id"], ([i] for i in range(25)))
        with self.assertRaises(hr.ReportError):
            rep.write(target, protected=lambda p: "_part" in p)

    def test_export_writer_html_manifest_lists_parts(self):
        target = os.path.join(self.tmp, "x.html")
        saved = limits.get("html_rows_per_part")
        limits._values["html_rows_per_part"] = 100
        try:
            res = ex.write_rows(target, "html", ["a", "b"], ([i, "v%d" % i] for i in range(250)),
                                ex.provenance("t", [], "src", columns=["a", "b"]))
        finally:
            limits._values["html_rows_per_part"] = saved
        self.assertTrue(res.complete)
        self.assertEqual(res.rows, 250)
        self.assertEqual(len(res.parts), 2)
        with open(res.manifest, encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual([f["path"] for f in man["files"]],
                         ["x.html", "x_part002.html", "x_part003.html"])
        self.assertEqual(man["provenance"]["rows"], 250)
        self.assertTrue(man["provenance"]["complete"])

    def test_200k_rows_bounded_memory(self):
        n = 200000
        target = os.path.join(self.tmp, "200k.html")
        texts = ["message text number %d with some words in it" % i for i in range(64)]

        def rows():
            for i in range(n):
                yield (i, texts[i % 64], 1600000000000 + i * 1000, i * 1.25,
                       "status %d" % (i % 5), None if i % 7 else "note", i % 3, "x" * (i % 40))
        def write():
            rep = hr.Report("200k", provenance=_info())
            rep.add_section("Rows")
            rep.add_table("messages", ["id", "text", "timestamp", "amount", "status", "note",
                                       "kind", "pad"], rows())
            return rep.write(target)
        t0 = time.time()
        write()                                 # timed without tracemalloc (it slows Python)
        took = time.time() - t0
        tracemalloc.start()
        res = write()
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        size = os.path.getsize(target)
        print("\n200,000 rows: %.1f MB written in %.1f s, peak Python memory %.1f MB"
              % (size / 1e6, took, peak / 1e6))
        self.assertTrue(res.complete)
        self.assertEqual(res.rows, n)
        self.assertEqual(res.parts, [])
        self.assertLess(peak, 30e6)
        self.assertLess(peak, size / 2)
        self.assertLess(took, 60)
        with open(target, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r'class="gmeta" data-grid="[^"]+">(.*?)</script>', text)
        meta = json.loads(m.group(1))
        self.assertEqual(meta["n"], n)
        self.assertEqual(meta["chunks"], n // limits.get("html_chunk_rows"))
        self.assertEqual(text.count('class="gdata"'), meta["chunks"])
        self.assertEqual(meta["dates"][0][:2], [2, "unix_ms"])
        # the no-script table holds only the first html_print_rows rows
        static = text[text.index('class="static-wrap"'):]
        self.assertEqual(static[:static.index("</table>")].count("<tr>"),
                         limits.get("html_print_rows") + 1)


class ScriptTest(unittest.TestCase):
    def test_brackets_balance_outside_strings_comments_and_regexes(self):
        for name, src in (("JS", JS), ("STATE", STATE_JS), ("FILTER", FILTER_JS),
                          ("VALUES", VALUES_JS)):
            self.assertEqual(js_balance(src), [], name)
        for bad in ("/*PURE*/", "{{", "%s", "%(", "</script", "<!--", "\t"):
            self.assertNotIn(bad, JS, bad)
        self.assertNotIn("<img ", JS)
        for fn in ("stEnc", "stDec", "parseFilter", "testFilter", "csvCell", "jsonText",
                   "Grid.prototype.paint", "requestAnimationFrame", "openDrawer", "fillStatic"):
            self.assertIn(fn, JS)
        self.assertNotIn("eval(", JS)

    def test_blobs_open_in_the_report_inspector(self):
        import plistlib
        from tests.decode_samples import pb_bytes, pb_varint
        rows = [[1, plistlib.dumps({"name": "Ann"}, fmt=plistlib.FMT_BINARY)],
                [2, pb_varint(1, 150) + pb_bytes(2, b"hello")]]
        rep = hr.Report("R", provenance=_info())
        rep.add_section("S")
        rep.add_table("t", ["id", "data"], rows)
        text = rep.render()
        check_page(self, text)
        self.assertIn('id="blobi"', text)                    # the inspector's overlay
        plist, proto = (hr.encode_cell(r[1])["b"] for r in rows)
        self.assertEqual(plist["dk"], "XML plist")
        self.assertIn("<string>Ann</string>", plist["dv"])
        self.assertEqual(proto["dk"], "protobuf fields")
        self.assertIn('2: "hello"', proto["dv"])
        for part in ("openBlob", "data-act=\"d-blob\"", "Inspect\\u2026", "bi-save",
                     "findBytes", "UTF-16", "v.b.dv"):
            self.assertIn(part, JS, part)
        self.assertNotIn("new Function", JS)
        self.assertNotIn("fetch(", JS)
        self.assertNotIn("XMLHttpRequest", JS)

    @unittest.skipUnless(_NODE, "node is not installed")
    def test_node_parses_the_script(self):
        d = tempfile.mkdtemp(prefix="sga_js_")
        try:
            path = os.path.join(d, "report.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(JS)
            r = subprocess.run([_NODE, "--check", path], capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            shutil.rmtree(d, ignore_errors=True)


def js_balance(src):
    """Problems with (){}[] outside strings, comments and regular expression literals."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack, problems = [], []
    i, n, prev = 0, len(src), ""
    while i < n:
        c = src[i]
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                if src[j] == "\n":
                    problems.append("newline in a string at %d" % i)
                    break
                j += 1
            i = j + 1
            prev = "a"
            continue
        if c == "`":
            problems.append("template literal at %d" % i)
            i += 1
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                problems.append("unclosed comment at %d" % i)
                break
            i = j + 2
            continue
        if c == "/" and (prev == "" or prev in "(,=:[!&|?{};+-*%<>~^") or (
                c == "/" and re.search(r"(?:return|typeof)\s*$", src[max(0, i - 8):i])):
            j, cls = i + 1, False
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    cls = True
                elif src[j] == "]":
                    cls = False
                elif src[j] == "/" and not cls:
                    break
                elif src[j] == "\n":
                    problems.append("newline in a regex at %d" % i)
                    break
                j += 1
            i = j + 1
            while i < n and src[i].isalpha():
                i += 1
            prev = "a"
            continue
        if c in "([{":
            stack.append((c, i))
        elif c in ")]}":
            if not stack or stack[-1][0] != pairs[c]:
                problems.append("unbalanced %s at %d: %r" % (c, i, src[max(0, i - 40):i + 1]))
                if stack:
                    stack.pop()
            else:
                stack.pop()
        if not c.isspace():
            prev = "a" if (c.isalnum() or c in "_$") else c
        i += 1
    problems.extend("unclosed %s at %d" % s for s in stack)
    return problems


class StateFormatTest(unittest.TestCase):
    STATES = [
        {},
        {"sec": "summary"},
        {"sec": "s-rows", "tbl": "t-urls", "q": "a&b=c#d%e f+g/h?i", "m": 0,
         "sort": [[3, 1], [0, -1]], "f": {"10": {"e": ">5"}, "2": {"e": "x", "v": ["a", "\u0000NULL",
                                                                                     "\u00e9\u4e2d"]}},
         "row": 17, "hide": [1, 4], "ord": [2, 0, 1, 3, 4], "pin": 1},
        {"tbl": "t-x", "q": "</script><!-- \"quoted\" 'single' \u2028", "f": {"0": {"v": []}}},
        {"tbl": "t-y", "f": {"1": {"e": "{a} AND {b} ~ ^= $= *= !x"}}, "row": 0},
    ]

    def test_round_trip_and_escaping(self):
        for st in self.STATES:
            h = hr.state_encode(st)
            self.assertTrue(h.startswith("v=1"))
            self.assertIsNone(re.search(r"[^A-Za-z0-9\-_.!~*'()%=&,]", h), h)
            back = hr.state_decode("#" + h)
            for k, v in st.items():
                if k == "f":
                    self.assertEqual({c: dict(x) for c, x in back["f"].items()},
                                     {c: x for c, x in v.items()}, h)
                else:
                    self.assertEqual(back[k], v, (k, h))
            self.assertEqual(hr.state_encode(back), h)
        self.assertEqual(hr.state_decode("#plain-id")["sec"], "plain-id")
        self.assertEqual(hr.state_decode("#tbl=%E0%A4&q=ok")["tbl"], "")     # bad UTF-8
        self.assertEqual(hr.state_decode("#f=%7Bnot json")["f"], {})
        self.assertEqual(hr.state_decode("#sort=1x,2d&row=-3")["sort"], [[2, -1]])
        self.assertEqual(hr.state_decode("#sort=1x,2d&row=-3")["row"], -1)
        enc = hr.state_encode({"f": {"10": {"e": "a"}, "9": {"e": "b"}}})
        self.assertIn(hr._uri('{"9":{"e":"b"},"10":{"e":"a"}}'), enc)     # numeric key order

    @unittest.skipUnless(_NODE, "node is not installed")
    def test_script_and_python_agree(self):
        prog = STATE_JS + "\nvar S=%s;\nprocess.stdout.write(JSON.stringify(S.map(function(s)" \
            "{var h=stEnc(s);return [h, stEnc(stDec('#'+h))];})));" % json.dumps(self.STATES)
        out = run_node(prog)
        for st, (h, again) in zip(self.STATES, out):
            self.assertEqual(h, hr.state_encode(st), st)
            self.assertEqual(again, h)


def run_node(prog):
    d = tempfile.mkdtemp(prefix="sga_js_")
    try:
        path = os.path.join(d, "t.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(prog)
        r = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=60,
                           encoding="utf-8")
        if r.returncode:
            raise AssertionError(r.stderr)
        return json.loads(r.stdout)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@unittest.skipUnless(_NODE, "node is not installed")
class FilterScriptTest(unittest.TestCase):
    """The report's filter language (a subset of engine.filters) and value encodings, run by
    node on the values as embedded."""

    def test_filters(self):
        cases = [  # expression, [(value, date ms), expected]
            ("abc", [("xABCx", True), (None, False), (5, False)]),
            ("!abc", [("abc", False), (None, True), ("x", True)]),
            (">5", [(6, True), (5, False), ("text", True), (None, False), ({"i": "9007199254740993"}, True)]),
            ("<=2.5", [(2.5, True), ({"f": "2.0"}, True), (3, False)]),
            ("1~3", [(2, True), (4, False), ("2", False)]),
            ("NULL", [(None, True), ("", False)]),
            ("NOT NULL", [(None, False), (0, True)]),
            ("EMPTY", [(None, True), ("", True), (0, False)]),
            ('"exact"', [("exact", True), ("exactly", False)]),
            ("^=ab", [("Abc", True), ("cab", False)]),
            ("$=ab", [("cAB", True), ("abc", False)]),
            ("!*=b", [("abc", False), ("xyz", True), (None, True)]),
            ("/^a.c$/", [("abc", True), ("ABC", False)]),
            ("/^a.c$/i", [("ABC", True)]),
            ("a%c", [("abbbc", True), ("abcd", False)]),
            ("IN (1, 'x', NULL)", [(1, True), ("x", True), (None, True), (2, False)]),
            ("NOT IN (1, 2)", [(1, False), (3, True), (None, True)]),
            ("PROGRA~1", [("c:/PROGRA~1/x", True), ("program", False)]),
            ("=x'00ff'", [({"b": {"n": 2, "hex": "00ff"}}, True), ({"b": {"n": 1, "hex": "00"}}, False)]),
        ]
        prog = VALUES_JS + FILTER_JS + "\nvar C=%s;var out=[];C.forEach(function(c){var f=" \
            "parseFilter(c[0],false);c[1].forEach(function(t){out.push([c[0],t[0]," \
            "testFilter(f,t[0],null)]);});});process.stdout.write(JSON.stringify(out));" % \
            json.dumps([[e, [[v, want] for v, want in t]] for e, t in cases])
        out = run_node(prog)
        want = [(e, v, w) for e, t in cases for v, w in t]
        self.assertEqual(len(out), len(want))
        for (e, v, got), (_e, _v, w) in zip(out, want):
            self.assertEqual(got, w, (e, v))
        # dates: a date operand compares the decoded date of a date column
        prog = VALUES_JS + FILTER_JS + "\nvar f=parseFilter('>2024-01-01',false),g=parseFilter(" \
            "'2024-01-01~2024-01-31 23:59',false);process.stdout.write(JSON.stringify([" \
            "testFilter(f,1,Date.UTC(2024,0,2)),testFilter(f,1,Date.UTC(2023,11,31))," \
            "testFilter(g,1,Date.UTC(2024,0,15)),testFilter(g,1,Date.UTC(2024,1,1))]));"
        self.assertEqual(run_node(prog), [True, False, True, False])

    def test_csv_and_json_text_match_the_export_encoding(self):
        values = [None, 5, "t", 1.5, 2 ** 60, 1.0, float("inf"), InvalidText(b"a\xff"),
                  b"\x00\xff", [1, b"\x01", None], _partial()]
        enc = [hr.encode_cell(v, "hex") for v in values]
        prog = VALUES_JS + "\nvar V=%s;process.stdout.write(JSON.stringify(V.map(function(v)" \
            "{return [csvCell(v),jsonText(v)];})));" % hr.script_json(enc).replace(
                "\\u003c", "<")
        out = run_node(prog)
        for v, (csv_text, json_text) in zip(values, out):
            self.assertEqual(csv_text, ex.csv_cell(v, "hex"), v)
            self.assertEqual(json.loads(json_text), json.loads(json.dumps(ex.json_cell(v, "hex"))),
                             v)


class DesignTest(unittest.TestCase):
    def test_contrast_of_both_palettes(self):
        tok = dict((t[0], (t[1], t[2])) for t in TOKENS)
        for which in (0, 1):
            for fg, bg in CONTRAST_PAIRS:
                ratio = hr.contrast(tok[fg][which], tok[bg][which])
                self.assertGreaterEqual(ratio, 4.5, (("light", "dark")[which], fg, bg, ratio))
        for c in ("#FFFFFF", "#000000", "#DC2626", "#FDE68A", "#1E40AF", "#8993a4"):
            self.assertGreaterEqual(hr.contrast(hr.text_on(c), c), 4.5, c)
        want = {"primary": "#1E40AF", "secondary": "#3B82F6", "amber": "#D97706",
                "bg": "#F8FAFC", "card": "#FFFFFF", "border": "#CBD5E1", "text": "#0F172A",
                "muted": "#475569", "danger": "#DC2626", "success": "#16A34A"}
        for k, v in want.items():
            self.assertEqual(tok[k][0], v)
        self.assertIn("prefers-color-scheme:dark", CSS)
        self.assertIn(":root[data-theme=dark]", CSS)

    def test_responsive_print_and_motion(self):
        text = hr.Report("x").render()
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">',
                      text)
        for bp in ("max-width:1023px", "max-width:767px", "min-width:1440px",
                   "prefers-reduced-motion:reduce", "@media print", "pointer:coarse"):
            self.assertIn(bp, CSS)
        self.assertIn("min-height:44px", CSS)
        self.assertRegex(CSS, r"max-width:767px\)\{[^@]*font-size:14px")
        # outside media queries, no fixed width wider than a phone
        flat = _outside_media(CSS)
        for m in re.finditer(r"(?<![-\w])width:\s*(\d+)px", flat):
            self.assertLessEqual(int(m.group(1)), 360, flat[max(0, m.start() - 60):m.end()])
        self.assertIn("overflow:auto", flat)              # tables scroll in their own box
        self.assertIn(".st th:first-child,.st td:first-child{position:sticky", flat)
        self.assertIn(".gt .rn{position:sticky", flat)
        # print: no interactive chrome, the plain tables shown, a page per section
        pr = CSS[CSS.index("@media print"):]
        for need in (".topbar", "nav.toc", ".drawer", ".static-wrap{display:block!important}",
                     "break-before:page"):
            self.assertIn(need, pr)


def _outside_media(css):
    out, i = [], 0
    while True:
        j = css.find("@media", i)
        if j < 0:
            out.append(css[i:])
            return "".join(out)
        out.append(css[i:j])
        k = css.index("{", j)
        depth, p = 1, k + 1
        while depth:
            if css[p] == "{":
                depth += 1
            elif css[p] == "}":
                depth -= 1
            p += 1
        i = p


if __name__ == "__main__":
    unittest.main()
