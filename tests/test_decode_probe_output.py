"""tools/decode_probe.py writes its report on any console: characters the console's encoding
cannot show become backslash escapes instead of stopping the report at the end of the run."""

import io
import os
import sys
import unittest

from tests.helpers import ROOT


class SafeOutputTest(unittest.TestCase):
    def test_cp1252_console(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        self.addCleanup(sys.path.remove, os.path.join(ROOT, "tools"))
        import decode_probe
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        out = decode_probe.safe_output(stream)
        out.write("table 字典 \U0001F600 café\n")
        out.flush()
        text = raw.getvalue().decode("cp1252")
        self.assertIn("café", text)
        self.assertIn("\\u5b57", text)
        self.assertIn("\\U0001f600", text)
