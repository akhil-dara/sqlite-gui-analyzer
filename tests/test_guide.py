"""The illustrated guide: one self-contained HTML page with every section, nothing loaded
from anywhere, escaped, written to the data folder only when it changed."""
import os
import re
import tempfile
import unittest

from tests.helpers import TempDirTest


class GuideTest(TempDirTest):
    def test_sections_and_self_containment(self):
        from guide import SECTIONS, guide_html
        page = guide_html("9.9.9")
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        for sid, title in SECTIONS:
            self.assertIn('id="%s"' % sid, page)
            self.assertIn(">%s<" % title, page)
        self.assertIn("9.9.9", page)
        # nothing loaded from anywhere: no src= or href= to a URL, a CSP that forbids it
        self.assertNotRegex(page, r'(src|href)="(https?:)?//')
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("<script", page)
        # the diagrams are inline SVG with titles for screen readers
        self.assertGreaterEqual(page.count("<svg"), 2)
        self.assertIn('aria-labelledby="flowt"', page)
        # states, confidence, the number display and derived columns are explained
        for word in ("older version", "inserted again", "÷100", "ƒ columns",
                     "byte offset", "secure_delete", "Content-Security-Policy"):
            self.assertIn(word, page)
        # the dark palette rides along
        self.assertIn("prefers-color-scheme:dark", page)

    def test_written_once_in_the_data_folder(self):
        from guide import guide_path, write_guide
        d = tempfile.mkdtemp(dir=self.tmp)
        p = write_guide(d)
        self.assertEqual(p, guide_path(d))
        self.assertTrue(os.path.exists(p))
        m1 = os.stat(p).st_mtime_ns
        self.assertEqual(write_guide(d), p)
        self.assertEqual(os.stat(p).st_mtime_ns, m1)      # unchanged content: not rewritten
        self.assertTrue(re.match(r"guide-\d+\.\d+\.\d+\.html$", os.path.basename(p)))


if __name__ == "__main__":
    unittest.main()
