"""The HTML report's own script: only it may run (a CSP with its SHA-256, no inline handler);
values from the evidence can not change the JSON it builds
(no '__raw' key trick, no '__proto__' loss), and an id taken from the page address is never
put into a CSS selector."""
import base64
import hashlib
import html
import json
import os
import re
import unittest

from tests.helpers import TempDirTest
from tests.test_html_report import _NODE, _info, run_node
from engine import html_report as hr
from engine.html_report import encode_cell, script_json
from engine.html_report_assets import JS, VALUES_JS


@unittest.skipUnless(_NODE, "node is not installed")
class JsonTextTest(unittest.TestCase):
    def js(self, values):
        enc = [v if isinstance(v, dict) and set(v) <= {"i", "f", "j", "r"} else
               encode_cell(v, "hex") for v in values]
        # parsed as the page parses its data blocks (JSON.parse, not a JS object literal)
        prog = VALUES_JS + "\nvar V=JSON.parse(%s);process.stdout.write(JSON.stringify(" \
            "V.map(function(v){return jsonText(v);})));" % json.dumps(script_json(enc))
        return run_node(prog)

    def test_dict_keys_and_raw_numbers(self):
        crafted = [
            {"j": {"__raw": '1,"injected":true'}},
            {"j": {"__raw": "1", "b": 2}},
            {"j": {"__proto__": "kept", "x": 1}},
            {"j": [{"j": {"__raw": "]"}}, 3]},
            {"i": "1,\"x\":2"},            # not digits: written as a string
            {"f": "1e5"}, {"f": "-2.5"}, {"f": "nan"},
            {"i": "-9007199254740993"},
        ]
        out = self.js(crafted)
        parsed = [json.loads(t) for t in out]       # every one is valid JSON
        self.assertEqual(parsed[0], {"__raw": '1,"injected":true'})
        self.assertEqual(parsed[1], {"__raw": "1", "b": 2})
        self.assertEqual(parsed[2], {"__proto__": "kept", "x": 1})
        self.assertEqual(parsed[3], [{"__raw": "]"}, 3])
        self.assertEqual(parsed[4], '1,"x":2')
        self.assertEqual(parsed[5:8], [1e5, -2.5, "nan"])
        self.assertEqual(out[8], "-9007199254740993")        # exact, as the digits stored


class PolicyTest(TempDirTest):
    def page(self):
        rep = hr.Report("R", provenance=_info())
        rep.add_section("S", text='x" onmouseover="alert(1)')
        rep.add_table("t", ["a"], [[1], ["<svg onload=alert(1)>"]])
        path = os.path.join(self.tmp, "r.html")
        rep.write(path)
        with open(path, "rb") as f:
            return f.read().decode("utf-8")

    def test_only_the_reports_own_script_may_run(self):
        text = self.page()
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', text)
        policy = dict((d.split(" ", 1) + [""])[:2] for d in
                      (x.strip() for x in html.unescape(csp.group(1)).split(";")) if d)
        self.assertEqual(policy["default-src"], "'none'")
        self.assertNotIn("unsafe-inline", policy["script-src"])
        self.assertNotIn("unsafe-eval", policy["script-src"])
        for d in ("base-uri", "form-action"):
            self.assertEqual(policy[d], "'none'", d)
        # the hash is that of the one script as the browser reads it (line breaks as LF)
        scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
        self.assertEqual(len(scripts), 1)
        body = scripts[0].replace("\r\n", "\n").replace("\r", "\n")
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        self.assertEqual(policy["script-src"], "'sha256-%s'" % digest)

    def test_no_inline_handlers(self):
        text = self.page()
        self.assertIsNone(re.search(r"<[a-z][^>]*\son[a-z]+\s*=", text, re.I))
        self.assertIsNone(re.search(r"\.on[a-z]+\s*=", JS))       # handlers are listeners
        self.assertNotIn("javascript:", text.lower())


class SelectorTest(unittest.TestCase):
    def test_no_id_from_the_address_in_a_selector(self):
        self.assertNotIn("data-sec=\"'+", JS)
        self.assertIsNone(re.search(r"querySelector(All)?\([^)]*'\+\s*(id|st\.sec|s\.sec)\b", JS))
        self.assertIn("function tocItem(", JS)
        self.assertNotIn("__raw", JS)
