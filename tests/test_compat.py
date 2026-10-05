"""The tool must run on any stock Python from 3.8 to 3.14 with only the standard library."""
import ast
import os
import re
import sys
import unittest

from tests.helpers import ROOT, SRC

BANNED = [
    (r"\.removeprefix\(|\.removesuffix\(", "str.removeprefix/removesuffix (3.9)"),
    (r"functools\.cache\b|from functools import cache\b", "functools.cache (3.9)"),
    (r"^\s*(import|from)\s+(zoneinfo|graphlib|tomllib)\b", "3.9+ stdlib module"),
    (r"^\s*match\s+[^=]+:\s*$", "match statement (3.10)"),
    (r":\s*(list|dict|tuple|set)\[", "builtin generic annotation (3.9)"),
    (r"->\s*(list|dict|tuple|set)\[", "builtin generic annotation (3.9)"),
    (r"\bzip\([^)]*strict=", "zip(strict=) (3.10)"),
    (r"^\s*(import|from)\s+(PySide|PyQt|numpy|pandas|PIL)\b", "third-party import at module level"),
]
ALLOWED_OPTIONAL = {"constants.py"}   # guarded 'try: from PIL import ...' lives here


def sources():
    files = [os.path.join(ROOT, "sqlite_gui_analyzer.py")]
    for d, _, names in os.walk(SRC):
        files += [os.path.join(d, n) for n in names if n.endswith(".py")]
    return files


class CompatTest(unittest.TestCase):
    def test_parses_as_python_38(self):
        for path in sources():
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), path, feature_version=(3, 8))

    def test_no_newer_or_third_party_apis(self):
        problems = []
        for path in sources():
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    for pattern, why in BANNED:
                        if re.search(pattern, line):
                            if "PIL" in line and os.path.basename(path) in ALLOWED_OPTIONAL:
                                continue
                            problems.append("%s:%d %s" % (os.path.relpath(path, ROOT), n, why))
        self.assertEqual(problems, [])

    def test_engine_has_no_tkinter(self):
        for d, _, names in os.walk(os.path.join(SRC, "engine")):
            for n in names:
                if n.endswith(".py"):
                    with open(os.path.join(d, n), encoding="utf-8") as f:
                        self.assertIsNone(re.search(r"^\s*(import|from)\s+tkinter", f.read(), re.M), n)


if __name__ == "__main__":
    unittest.main()
