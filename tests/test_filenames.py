"""File names derived from data: one sanitizer (engine.filenames) behind tags.safe_name,
utils.safe_filename and datamap.safe_name; never a device name, a bidi trick, '.' or '..'."""
import unittest

import tests.helpers  # noqa: F401  (puts src/ on sys.path)
from engine import datamap as dm
from engine import tags as T
from engine.filenames import is_reserved_name, safe_file_name
from utils import safe_filename

SANITIZERS = (("tags.safe_name", T.safe_name), ("utils.safe_filename", safe_filename),
              ("datamap.safe_name", dm.safe_name))
DEVICES = ["CON", "con", "Prn", "AUX", "NUL", "nul", "COM1", "com9", "LPT1", "LPT9", "COM0",
           "COM¹", "LPT²", "COM³", "CONIN$", "conout$"]


class ReservedNamesTest(unittest.TestCase):
    def test_reserved_detection(self):
        for name in DEVICES:
            for variant in (name, name + ".csv", name + ".tar.gz", name + " .txt"):
                self.assertTrue(is_reserved_name(variant), variant)
        for name in ("CONSOLE", "COM10", "NULL.csv", "LPT", "xNUL", "_NUL.csv", "AUXI"):
            self.assertFalse(is_reserved_name(name), name)

    def test_no_sanitizer_returns_a_device_name(self):
        for label, fn in SANITIZERS:
            for name in DEVICES:
                for variant in (name, name + ".csv", name + "..", " " + name + " "):
                    out = fn(variant)
                    self.assertFalse(is_reserved_name(out), "%s(%r) -> %r" % (label, variant, out))
                    self.assertFalse(is_reserved_name(out + ".csv"), "%s(%r)" % (label, variant))

    def test_dot_names_and_empty(self):
        for label, fn in SANITIZERS:
            for text in ("", ".", "..", "...", " ", " . ", "_", "‮", "\x00"):
                out = fn(text)
                self.assertNotIn(out, ("", ".", ".."), "%s(%r)" % (label, text))
                self.assertFalse(out.endswith((".", " ")), "%s(%r) -> %r" % (label, text, out))

    def test_format_characters_and_separators_are_removed(self):
        for label, fn in SANITIZERS:
            for text in ("evil‮gpj.exe", "a​b", "x⁦y⁩", "﻿name",
                         "..\\..\\evil", "../../evil", "C:evil", "a:b/c\\d", "x\ny\tz"):
                out = fn(text)
                for ch in out:
                    self.assertNotIn(ch, '\\/:*?"<>|‮​⁦⁩﻿\n\t',
                                     "%s(%r) -> %r" % (label, text, out))
                self.assertNotIn(out, ("", ".", ".."))
            self.assertNotIn("‮", fn("photo‮gnp.exe"))

    def test_trailing_dots_and_spaces(self):
        for label, fn in SANITIZERS:
            for text in ("report. . .", "name   ", "a_.", "x.. "):
                out = fn(text)
                self.assertFalse(out.endswith((".", " ")), "%s(%r) -> %r" % (label, text, out))

    def test_limits_hold(self):
        self.assertLessEqual(len(T.safe_name("NUL" + "x" * 0, 3)), 3)
        self.assertEqual(len(safe_file_name("a" * 500, 80)), 80)
        out = safe_file_name("CON", 3)
        self.assertFalse(is_reserved_name(out))
        self.assertLessEqual(len(out), 3)

    def test_ordinary_names_unchanged(self):
        self.assertEqual(T.safe_name("messages"), "messages")
        self.assertEqual(safe_filename("chat_list"), "chat_list")
        self.assertEqual(dm.safe_name("msgstore.db"), "msgstore.db")
        self.assertEqual(dm.safe_name("My Case (1)"), "My Case (1)")
        self.assertEqual(dm.map_file_name("NUL", "html"), "_NUL - Database Map.html")


class TaggedCsvFileNameTest(unittest.TestCase):
    def test_table_named_like_a_device_gets_a_safe_file(self):
        for table in ("NUL", "COM1", "con", "AUX"):
            name = T.safe_name(table) + ".csv"
            self.assertFalse(is_reserved_name(name), name)


if __name__ == "__main__":
    unittest.main()
