import random
import time
import unittest
import xml.etree.ElementTree as ET

import tests.helpers  # noqa: F401
from engine import xmlpretty
from tests.helpers import within

pretty = xmlpretty.pretty_xml

ERR = r"^not XML: .+ at line \d+ column \d+$"

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>Name</key><string>  Alice &amp; Bob  </string>
<key>Count</key><integer>3</integer><key>Flag</key><true/>
<key>List</key><array><string>a</string><string/><data>AAEC</data></array></dict></plist>"""

PLIST_PRETTY = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
  <dict>
    <key>Name</key>
    <string>  Alice &amp; Bob  </string>
    <key>Count</key>
    <integer>3</integer>
    <key>Flag</key>
    <true/>
    <key>List</key>
    <array>
      <string>a</string>
      <string/>
      <data>AAEC</data>
    </array>
  </dict>
</plist>"""

SVG = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink='http://www.w3.org/1999/xlink'
     width="100" height='50' viewBox="0 0 100 50">
  <!-- a comment with <markup> & stuff -->
  <defs><linearGradient id="g"><stop offset="0" stop-color='#fff'/></linearGradient></defs>
  <g transform="translate(1 2)"><rect x="0" y="0" width="10" height="10" fill="url(#g)"/>
  <text x="5" y="5" font-family='"Noto Sans", serif'>Hi &lt;there&gt; &#x2019;</text>
  <use xlink:href="#g"/></g>
  <style><![CDATA[ rect > text { fill: red } ]]></style>
</svg>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<?xml-stylesheet type="text/xsl" href="feed.xsl"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title type="text">Example Feed</title>
  <link href="http://example.org/?a=1&amp;b=2"/>
  <updated>2003-12-13T18:30:02Z</updated>
  <author><name>John Doe</name></author>
  <id>urn:uuid:60a76c80-d399-11d9-b93C-0003939e0af6</id>
  <entry>
    <title>Atom-Powered Robots Run Amok</title>
    <id>urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a</id>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Some <b>bold</b>
      and <i>italic</i> text &#169; 2003</div></content>
  </entry>
</feed>
<!-- trailing comment -->"""

MIXED = "<p>Hello <b>big</b> world <!--c--> again<br/>end</p>"
MIXED_PRETTY = """<p>
  Hello
  <b>big</b>
  world
  <!--c-->
  again
  <br/>
  end
</p>"""

SAMPLES = (PLIST, SVG, ATOM, MIXED)


def _norm(s):
    return " ".join((s or "").split())


def _signature(text):
    """Element structure as the standard library reads it: tags, attributes, text."""
    root = ET.fromstring(text.encode("utf-8"))
    return [(e.tag, sorted(e.attrib.items()), _norm(e.text), _norm(e.tail))
            for e in root.iter()]


class TypicalDocuments(unittest.TestCase):
    def test_plist(self):
        self.assertEqual(pretty(PLIST), PLIST_PRETTY)

    def test_mixed_content(self):
        self.assertEqual(pretty(MIXED), MIXED_PRETTY)

    def test_svg_attributes_and_cdata(self):
        out = pretty(SVG)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith('<svg xmlns="http://www.w3.org/2000/svg" '
                                            'xmlns:xlink="http://www.w3.org/1999/xlink"'))
        self.assertIn('  <!-- a comment with <markup> & stuff -->', lines)
        self.assertIn('      <stop offset="0" stop-color="#fff"/>', lines)
        self.assertIn('    <text x="5" y="5" font-family="&quot;Noto Sans&quot;, serif">'
                      'Hi &lt;there&gt; &#x2019;</text>', lines)
        self.assertIn('  <style><![CDATA[ rect > text { fill: red } ]]></style>', lines)
        self.assertEqual(lines[-1], "</svg>")

    def test_atom_prolog_and_epilog(self):
        lines = pretty(ATOM).splitlines()
        self.assertEqual(lines[0], '<?xml version="1.0" encoding="utf-8"?>')
        self.assertEqual(lines[1], '<?xml-stylesheet type="text/xsl" href="feed.xsl"?>')
        self.assertEqual(lines[2], '<feed xmlns="http://www.w3.org/2005/Atom">')
        self.assertEqual(lines[-1], "<!-- trailing comment -->")
        self.assertEqual(lines[-2], "</feed>")
        self.assertIn('  <link href="http://example.org/?a=1&amp;b=2"/>', lines)
        self.assertIn("        text &#169; 2003", lines)

    def test_round_trip_structure_and_idempotence(self):
        for sample in SAMPLES:
            out = pretty(sample)
            self.assertEqual(_signature(out), _signature(sample))
            self.assertEqual(pretty(out), out)

    def test_references_kept_as_written(self):
        out = pretty("<a t='say \"hi\" &amp; &#60;' u=\"x'y\">&lt;&#x41;&gt;&apos;&quot;</a>")
        self.assertEqual(out, '<a t="say &quot;hi&quot; &amp; &#60;" u="x\'y">'
                              '&lt;&#x41;&gt;&apos;&quot;</a>')

    def test_empty_and_whitespace_elements_kept(self):
        self.assertEqual(pretty("<a><b></b><c/><d>  </d></a>"),
                         "<a>\n  <b></b>\n  <c/>\n  <d>  </d>\n</a>")

    def test_attribute_spacing_normalised(self):
        self.assertEqual(pretty('<a  x = "1"\n\ty=\'2\'  />'), '<a x="1" y="2"/>')

    def test_tab_indent_and_bom(self):
        self.assertEqual(pretty("﻿<a><b/></a>", indent="\t"), "<a>\n\t<b/>\n</a>")

    def test_unicode_names(self):
        self.assertEqual(pretty("<données été='1'><ü/></données>"),
                         '<données été="1">\n  <ü/>\n</données>')

    def test_processing_instruction_inside(self):
        self.assertEqual(pretty("<a><?php echo 1; ?></a>"), "<a>\n  <?php echo 1; ?>\n</a>")


class Cdata(unittest.TestCase):
    def test_cdata_with_markup_and_brackets(self):
        doc = "<a><![CDATA[x < y && ]] ]> ]]]]><![CDATA[>]]></a>"
        self.assertEqual(pretty(doc), doc)
        self.assertEqual(_signature(pretty(doc)), _signature(doc))

    def test_cdata_in_mixed_content_on_own_line(self):
        self.assertEqual(pretty("<a><b/><![CDATA[<x>]]></a>"),
                         "<a>\n  <b/>\n  <![CDATA[<x>]]>\n</a>")

    def test_cdata_end_in_text_refused(self):
        with self.assertRaisesRegex(ValueError, "']]>' in text"):
            pretty("<a>x ]]> y</a>")


class Comments(unittest.TestCase):
    def test_comment_content_verbatim(self):
        self.assertEqual(pretty("<a><!-- - <b> & -x- --><c/></a>"),
                         "<a>\n  <!-- - <b> & -x- -->\n  <c/>\n</a>")
        self.assertEqual(pretty("<!----><a/>"), "<!---->\n<a/>")

    def test_double_hyphen_refused(self):
        for doc in ("<a><!-- a -- b --></a>", "<a><!-- a ---></a>", "<!-- -- --><a/>"):
            with self.assertRaisesRegex(ValueError, ERR):
                pretty(doc)


class Malformed(unittest.TestCase):
    CASES = {
        "mismatched": "<a><b></a></b>",
        "two roots": "<a/><b/>",
        "two roots text between": "<a></a> <b></b>",
        "unterminated start tag": "<a><b x='1'",
        "unterminated end tag": "<a></a",
        "unterminated comment": "<a><!-- never ends</a>",
        "unterminated CDATA": "<a><![CDATA[ never ends</a>",
        "unterminated PI": "<a><?pi never ends</a>",
        "lt in attribute": '<a x="1<2"/>',
        "unquoted attribute": "<a x=1/>",
        "attribute without value": "<a x/>",
        "duplicate attribute": '<a x="1" x="2"/>',
        "no space between attributes": '<a x="1"y="2"/>',
        "unterminated attribute": '<a x="1/>',
        "bad tag name": "<1a/>",
        "space after lt": "< a/>",
        "bare lt in text": "<a>1 < 2</a>",
        "bare ampersand": "<a>fish & chips</a>",
        "ampersand without semicolon": "<a>&amp</a>",
        "unknown entity": "<a>&nbsp;</a>",
        "unknown entity in attribute": '<a x="&copy;"/>',
        "bad char ref": "<a>&#0;</a>",
        "bad hex char ref": "<a>&#xD800;</a>",
        "huge char ref": "<a>&#99999999999999999999;</a>",
        "text before root": "hello<a/>",
        "text after root": "<a/>bye",
        "empty": "",
        "whitespace only": " \n\t ",
        "comment only": "<!-- c -->",
        "unclosed": "<a><b>",
        "end without start": "</a>",
        "cdata outside root": "<![CDATA[x]]><a/>",
        "declaration not first": " <?xml version='1.0'?><a/>",
        "second declaration": "<?xml version='1.0'?><?xml version='1.0'?><a/>",
        "reserved PI target": "<a><?XML x?></a>",
        "declaration without version": "<?xml encoding='utf-8'?><a/>",
        "malformed declaration": "<?xml version='1.0' encoding=[UTF-8'?><a/>",
        "declaration with unknown pseudo-attribute": "<?xml version='1.0' x='1'?><a/>",
        "element declaration": "<!ELEMENT a ANY><a/>",
        "control character": "<a>\x01</a>",
        "nul": "<a>\x00</a>",
        "slash in tag": "<a / x='1'/>",
        "junk": "\x89PNG\r\n\x1a\n",
    }

    def test_refused(self):
        for label, doc in self.CASES.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, ERR):
                    pretty(doc)

    def test_position_reported(self):
        with self.assertRaisesRegex(
                ValueError, r"^not XML: end tag </c> does not match <b> opened at line 2 "
                            r"column 3 at line 3 column 1$"):
            pretty("<a>\n  <b>\n</c></a>")

    def test_not_text(self):
        with self.assertRaisesRegex(ValueError, "not text"):
            pretty(b"<a/>")

    def test_bad_arguments(self):
        for kwargs in ({"indent": "x"}, {"indent": 2}, {"max_depth": 0}, {"max_nodes": -1},
                       {"max_depth": True}, {"max_nodes": 1.5}):
            with self.assertRaises(ValueError):
                pretty("<a/>", **kwargs)


class Dtd(unittest.TestCase):
    def assertRefused(self, doc):
        with self.assertRaises(ValueError) as cm:
            pretty(doc)
        self.assertEqual(str(cm.exception), "XML with a DTD is not pretty-printed")

    APPLE = ('<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
             '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">')

    def test_apple_plist_doctype_kept(self):
        doc = PLIST.replace("\n<plist", "\n" + self.APPLE + "\n<plist", 1)
        expected = PLIST_PRETTY.replace("\n<plist", "\n" + self.APPLE + "\n<plist", 1)
        self.assertEqual(pretty(doc), expected)
        self.assertEqual(pretty(expected), expected)

    def test_plain_doctypes_accepted(self):
        for doctype in ("<!DOCTYPE a>", "<!DOCTYPE a >", "<!DOCTYPE a SYSTEM 'a.dtd'>",
                        '<!DOCTYPE a\n  SYSTEM "x [y] <z>">', "<!DOCTYPE a PUBLIC 'p' \"s\">",
                        "<!DOCTYPE other>"):
            with self.subTest(doctype):
                doc = "<!-- c -->" + doctype + "<?pi x?><a>&amp;</a>"
                self.assertEqual(pretty(doc),
                                 "<!-- c -->\n%s\n<?pi x?>\n<a>&amp;</a>" % doctype)

    def test_doctype_with_subset_or_malformed_refused(self):
        for doctype in ("<!DOCTYPE a []>", "<!DOCTYPE a[]>", "<!DOCTYPE a SYSTEM 'x' [ ]>",
                        "<!DOCTYPE a PUBLIC 'p'>", "<!DOCTYPE a SYSTEM x>", "<!DOCTYPE>",
                        "<!DOCTYPE a PUBLIC '{' 's'>", "<!DOCTYPE a SYSTEM 'x'",
                        "<!doctype a>", "<!DocType a>"):
            with self.subTest(doctype):
                self.assertRefused(doctype + "<a/>")

    def test_doctype_after_10kb_comment(self):
        self.assertRefused("<!--" + "x" * 10240 + "-->\n<!DOCTYPE a [<!ENTITY e 'x'>]><a>&e;</a>")
        self.assertRefused("<!--" + "x" * 10240 + "-->\n<!DOCTYPE a [ ]><a/>")

    def test_doctype_anywhere_else_refused(self):
        for doc in ("<a><!doctype a></a>", "<a><!DOCTYPE a></a>", "<a/><!DOCTYPE a>",
                    "<a/><!DocType a>", "<a>text <!DOCTYPE</a>", "<a><!-- <!DOCTYPE a> --></a>",
                    "<!DOCTYPE a><!DOCTYPE a><a/>", "<!DOCTYPE a><a><!-- <!doctype --></a>",
                    "<!DOCTYPE a><a><![CDATA[<!DOCTYPE]]></a>", "<a><!ENTITY x 'y'></a>",
                    "<!-- <!entity --><a/>"):
            with self.subTest(doc):
                self.assertRefused(doc)

    def test_doctype_does_not_define_entities(self):
        with self.assertRaisesRegex(ValueError, r"^not XML: unknown entity &e;"):
            pretty("<!DOCTYPE a SYSTEM 'a.dtd'><a>&e;</a>")

    def test_billion_laughs(self):
        lines = ['<?xml version="1.0"?>', "<!DOCTYPE lolz [", ' <!ENTITY lol "lol">']
        for k in range(1, 10):
            lines.append(' <!ENTITY lol%d "%s">' % (k, ("&lol%s;" % (k - 1 or "")) * 10))
        lines += ["]>", "<lolz>&lol9;</lolz>"]
        self.assertRefused("\n".join(lines))

    def test_entity_use_without_dtd(self):
        with self.assertRaisesRegex(ValueError, r"^not XML: unknown entity &lol9;"):
            pretty("<lolz>&lol9;</lolz>")


class Limits(unittest.TestCase):
    def test_deep_nesting_fails_fast(self):
        doc = "<a>" * 100000 + "</a>" * 100000
        t = time.perf_counter()
        try:
            pretty(doc)
        except RecursionError:
            self.fail("RecursionError")
        except ValueError as e:
            self.assertIn("depth limit", str(e))
            self.assertIn("256", str(e))
        else:
            self.fail("no error")
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_depth_limit_exact(self):
        self.assertTrue(pretty("<a>" * 5 + "</a>" * 5, max_depth=5))
        self.assertTrue(pretty("<a>" * 4 + "<a/>" + "</a>" * 4, max_depth=5))
        with self.assertRaisesRegex(ValueError, "nested deeper than 5 elements"):
            pretty("<a>" * 6 + "</a>" * 6, max_depth=5)
        with self.assertRaisesRegex(ValueError, "depth limit"):
            pretty("<a>" * 5 + "<a/>" + "</a>" * 5, max_depth=5)

    def test_node_limit(self):
        doc = "<r>" + "<a/>" * 9 + "</r>"            # 10 nodes
        self.assertTrue(pretty(doc, max_nodes=10))
        with self.assertRaisesRegex(ValueError, r"more than 9 nodes \(the node limit\)"):
            pretty(doc, max_nodes=9)
        with self.assertRaisesRegex(ValueError, "1,000,000 nodes"):
            pretty("<r>" + "<a/>" * 1000001 + "</r>")

    def test_five_megabytes(self):
        item = ('<item id="%d" kind=\'x\'><name>Name &amp; value %d</name>'
                '<!-- note --><v><![CDATA[<raw>]]></v><e/></item>\n')
        parts = ["<?xml version='1.0'?>\n<root>\n"]
        size = 0
        k = 0
        while size < 5 * 1024 * 1024:
            s = item % (k, k)
            parts.append(s)
            size += len(s)
            k += 1
        parts.append("</root>")
        doc = "".join(parts)
        t = time.perf_counter()
        out = pretty(doc)
        elapsed = time.perf_counter() - t
        within(self, elapsed, 5.0)
        self.assertTrue(out.startswith("<?xml version='1.0'?>\n<root>\n  <item id=\"0\" "
                                       "kind=\"x\">\n    <name>Name &amp; value 0</name>"))
        self.assertEqual(out.count("<item "), k)

    def test_pathological_inputs_fail_fast(self):
        big = 1000000
        docs = ["<" * big, "<a " + "x" * big, "<a>" + "&#1" * big, "<a>" + "&a" * big,
                "<!--" * big, "<?a" * big, "<a x='" + "'" * big, "<a>" + "]]" * big,
                "<a " + 'b="1" ' * (big // 10) + "b='2'/>", "<a>" + "</" * big,
                "<a>" + "<![CDATA[" * big,
                "<?xml version='1.0'" + " " * big + "x?><a/>",
                "<?xml version='1.0'" + " " * big + "encoding='u'" + " " * big + "x?><a/>",
                "<!DOCTYPE a" + " " * big + "x><a/>",
                "<!DOCTYPE a SYSTEM '" + "x" * big + "<a/>",
                "<!DOCTYPE a PUBLIC '" + "x" * big + "' 'y'" + " " * big + "[]><a/>"]
        t = time.perf_counter()
        for doc in docs:
            with self.assertRaises(ValueError):
                pretty(doc)
        self.assertLess(time.perf_counter() - t, 5.0)

    def test_many_attributes_and_long_text(self):
        doc = "<a " + " ".join('k%d="v"' % k for k in range(100000)) + ">" + "t" * 10 ** 6 + "</a>"
        t = time.perf_counter()
        out = pretty(doc)
        self.assertLess(time.perf_counter() - t, 5.0)
        self.assertTrue(out.endswith("t</a>"))


class Fuzz(unittest.TestCase):
    ALPHABET = "<>/!?-[]&;#x='\" \nabAB:_.0\x00é"

    def test_mutations_only_raise_value_error(self):
        rng = random.Random(20261001)
        accepted = 0
        for sample in SAMPLES:
            for _ in range(1500):
                s = list(sample)
                for _ in range(rng.randint(1, 4)):
                    op = rng.randrange(4)
                    pos = rng.randrange(len(s) + 1)
                    if op == 0:
                        s.insert(pos, rng.choice(self.ALPHABET))
                    elif op == 1 and pos < len(s):
                        del s[pos]
                    elif op == 2 and pos < len(s):
                        s[pos] = rng.choice(self.ALPHABET)
                    else:
                        end = min(len(s), pos + rng.randint(1, 40))
                        s[pos:pos] = s[pos:end]
                doc = "".join(s)
                try:
                    out = pretty(doc)
                except ValueError:
                    continue
                accepted += 1
                self.assertIsInstance(out, str)
                self.assertEqual(pretty(out), out)
        self.assertGreater(accepted, 0)


if __name__ == "__main__":
    unittest.main()
