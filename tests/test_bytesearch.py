"""Byte-level matching: hex patterns, text inside BLOBs as UTF-8 / UTF-16, snippets."""
import re
import unittest

import tests.helpers  # noqa: F401  (puts src/ on sys.path)
from engine.bytesearch import (HexPattern, TextInBytes, ascii_case_run, case_forms, invariant_run,
                               parse_hex, regex_in_bytes, snippet)
from engine.schema import Locator
from engine.search import Matcher, check_term, match_row


def le(s):
    return s.encode("utf-16-le")


def be(s):
    return s.encode("utf-16-be")


class HexParserTest(unittest.TestCase):
    def test_accepted_forms(self):
        for text in ("48 65 6c", "48656C", "48656c", "0x48 0x65 0x6C", "0x48,0x65,0x6c",
                     "0X48656c", "\\x48\\x65\\x6c", "\\X48\\x65 6C", "4865\\x6c", "48:65:6c",
                     "  48, 65 ,6c  ", "48\t65\n6c", "0x48, 65 \\x6C"):
            self.assertEqual(parse_hex(text), [0x48, 0x65, 0x6c], text)

    def test_wildcards(self):
        self.assertEqual(parse_hex("48 ?? 6c"), [0x48, None, 0x6c])
        self.assertEqual(parse_hex("48??6c"), [0x48, None, 0x6c])
        self.assertEqual(parse_hex("\\x48\\x??"), [0x48, None])
        self.assertEqual(parse_hex("0x48??"), [0x48, None])
        self.assertEqual(parse_hex("?? ff"), [None, 0xff])

    def test_errors_say_what_is_wrong(self):
        for text, fragment in (("486", "odd number"), ("48 6", "odd number"), ("4g", "'g'"),
                               ("zz", "'z'"), ("x41", "'x'"), ("48-65", "'-'"),
                               ("4?", "half-byte"), ("?", "half-byte"), ("48 ?", "half-byte"),
                               ("", "enter hex"), ("  ,  ", "enter hex"), ("0x", "enter hex"),
                               ("??", "known byte"), ("?? ??", "known byte"),
                               ("\\x4", "escape"), ("\\y41", "escape"), ("41\\", "escape")):
            with self.assertRaises(ValueError) as cm:
                parse_hex(text)
            self.assertIn(fragment, str(cm.exception), text)

    def test_check_term(self):
        check_term("de ad be ef", "hex")
        check_term("anything at all", "ci")
        with self.assertRaises(ValueError):
            check_term("dead bee", "hex")
        with self.assertRaises(ValueError):
            check_term("(unclosed", "rx")


class HexPatternTest(unittest.TestCase):
    def test_matches_whole_bytes_only(self):
        data = bytes.fromhex("148656c0")        # its hex digits contain 4865 and 656c at odd nibbles
        self.assertIsNone(HexPattern("48 65").find(data))
        self.assertIsNone(HexPattern("65 6c").find(data))
        self.assertEqual(HexPattern("86 56").find(data), 1)
        self.assertEqual(HexPattern("48 65").find(b"\x00\x48\x65"), 1)

    def test_wildcards_are_one_whole_byte(self):
        p = HexPattern("48 ?? 6c")
        self.assertEqual(p.literal, b"\x48")
        self.assertEqual(p.find(b"xx\x48\x00\x6c"), 2)
        self.assertEqual(p.find(b"\x48\x0a\x6c"), 0)        # '.' matches a newline byte too
        self.assertIsNone(p.find(b"\x48\x6c"))
        self.assertIsNone(p.find(b"\x48\x00\x00\x6c"))
        self.assertIsNone(HexPattern("?? 65").find(bytes.fromhex("148656c0")))

    def test_old_nibble_match_is_gone(self):
        # "48 65" is in hexlify(b"\x14\x86\x5c") = "14865c" but at a half-byte offset
        m = Matcher("48 65", "blob", True)
        self.assertIsNone(m.cell_hit(b"\x14\x86\x5c"))
        self.assertIsNone(Matcher("48 65", "hex", False).cell_hit(b"\x14\x86\x5c"))
        self.assertEqual(Matcher("48 65", "hex", False).cell_hit(b"\x01He")[1:], ("blob_hex", "hex", 1))


class TextInBytesTest(unittest.TestCase):
    def test_utf16_le_and_be_with_offsets(self):
        self.assertEqual(TextInBytes("hello", True).search(b"\x00\x00" + le("hello")),
                         (2, 12, "utf-16le"))
        self.assertEqual(TextInBytes("hello", True).search(b"\x01" + le("Hello") + b"\x00"),
                         (1, 11, "utf-16le"))                          # odd offset
        self.assertEqual(TextInBytes("HELLO", True).search(b"xx" + be("Hello")),
                         (2, 12, "utf-16be"))
        self.assertEqual(TextInBytes("hello", False).search(be("hello")), (0, 10, "utf-16be"))
        self.assertIsNone(TextInBytes("Hello", False).search(le("hello")))

    def test_ascii_case_insensitive_and_nul_prefixed(self):
        self.assertEqual(TextInBytes("HeLLo", True).search(b"\x00\x00say hello!"),
                         (6, 11, "utf-8"))
        self.assertEqual(TextInBytes("secret", True).search(b"\x00SECRET\x00"), (1, 7, "utf-8"))
        self.assertIsNone(TextInBytes("secret", True).search(b"\x00SECRE\x00T"))

    def test_earliest_match_wins_and_utf8_on_a_tie(self):
        self.assertEqual(TextInBytes("a", True).search(b"a\x00"), (0, 1, "utf-8"))
        self.assertEqual(TextInBytes("ab", True).search(le("ab") + b"ab"), (0, 4, "utf-16le"))

    def test_the_other_utf16_byte_order_one_byte_off_is_not_reported(self):
        # LE text also reads as BE one byte earlier: '20 00 68 00 69 00' holds BE 'hi' at 1
        say = TextInBytes("hi", True)
        self.assertEqual(say.search(le("say hi")), (8, 12, "utf-16le"))
        self.assertEqual(say.search(b"\x07" + le("say hi")), (9, 13, "utf-16le"))   # odd offset
        self.assertEqual(say.search(b"\x00\x00" + le("say hi") + b"\x00\x00"), (10, 14, "utf-16le"))
        self.assertEqual(say.search(be("say hi") + b"\x00\x00"), (8, 12, "utf-16be"))
        self.assertEqual(say.search(b"\x07" + be("say hi")), (9, 13, "utf-16be"))

    def test_non_ascii_letters_match_their_other_case(self):
        for data, want in (("xx über".encode("utf-8"), (3, 8, "utf-8")),
                           (le("xx ÜBER"), (6, 14, "utf-16le")),
                           (be("Über"), (0, 8, "utf-16be"))):
            self.assertEqual(TextInBytes("üBeR", True).search(data), want)
        self.assertIsNone(TextInBytes("über", False).search("ÜBER".encode("utf-8")))
        self.assertEqual(case_forms("ß"), set("ß"))           # 'SS' is not one character

    def test_anchors(self):
        sw, ew, ex = TextInBytes("abc", True, "start"), TextInBytes("DEF", True, "end"), \
            TextInBytes("abc", False, "whole")
        self.assertEqual(sw.search(b"\xef\xbb\xbfABCdef"), (3, 6, "utf-8"))      # after a BOM
        self.assertEqual(sw.search(b"\xff\xfe" + le("abcdef")), (2, 8, "utf-16le"))
        self.assertIsNone(sw.search(b"xabc"))
        self.assertEqual(ew.search(b"abcdef\x00"), (3, 6, "utf-8"))              # before a NUL
        self.assertEqual(ew.search(b"\x00" * 5000 + be("def")), (5000, 5006, "utf-16be"))
        self.assertIsNone(ew.search(b"defx"))
        self.assertEqual(ex.search(le("abc") + b"\x00\x00"), (0, 6, "utf-16le"))
        self.assertEqual(ex.search(b"abc"), (0, 3, "utf-8"))
        self.assertIsNone(ex.search(b"abcd"))
        self.assertIsNone(ex.search(b"ABC"))

    def test_sql_needles_are_in_every_match(self):
        self.assertEqual(TextInBytes("Hi", False).sql_needles(), [b"Hi", le("Hi"), be("Hi")])
        self.assertEqual(invariant_run("user-2024-07 x"), "-2024-07 ")
        self.assertEqual(TextInBytes("id 2024-07", True).sql_needles()[0], b" 2024-07")
        self.assertEqual(TextInBytes("hello", True).sql_needles(), [])
        self.assertEqual(invariant_run("ı123", strict=True), "123")

    def test_ascii_case_run_leaves_out_letters_with_non_ascii_case_partners(self):
        self.assertEqual(ascii_case_run("Kiss me, Sam"), " me, ")
        self.assertEqual(ascii_case_run("abcé 12"), "abc")
        self.assertEqual(ascii_case_run("ski"), "")
        for letter, partner in (("i", chr(0x130)), ("i", chr(0x131)), ("s", chr(0x17f)),
                                ("k", chr(0x212a))):
            self.assertTrue(re.fullmatch("(?i)" + letter, partner))       # why they are left out


class RegexInBytesTest(unittest.TestCase):
    def test_utf8_and_both_utf16_alignments(self):
        rx = re.compile(r"\d{3}-\d{4}")
        self.assertEqual(regex_in_bytes(rx, b"\xff\xfeab 123-4567"), (5, 13, "utf-8"))
        self.assertEqual(regex_in_bytes(rx, b"\x00\x01" + le("call 555-1234")), (12, 28, "utf-16le"))
        self.assertEqual(regex_in_bytes(rx, b"\x01" + le("call 555-1234")), (11, 27, "utf-16le"))
        self.assertEqual(regex_in_bytes(rx, b"\x01" + be("555-1234")), (1, 17, "utf-16be"))
        self.assertIsNone(regex_in_bytes(rx, b"\x00" * 40))


class SnippetTest(unittest.TestCase):
    def test_utf16_snippet_reads_as_text(self):
        data = b"\x00" * 30 + le("hello world") + b"\x00" * 30
        s = snippet(data, 30, 40, "utf-16le")
        self.assertTrue(s.startswith("..."))
        self.assertIn("hello world", s)
        self.assertTrue(s.endswith("[utf-16le @30: 68 00 65 00 6c 00 6c 00 6f 00]"))

    def test_hex_snippet_shows_printable_ascii(self):
        self.assertEqual(snippet(b"\x00ABC\xff", 1, 3, "hex"), ".ABC.  [hex @1: 41 42]")
        long = snippet(b"x" * 100, 0, 40, "hex")      # 40 matched bytes: the first 32 shown
        self.assertTrue(long.endswith("[hex @0: " + " ".join(["78"] * 32) + " ...]"))


class MatcherTest(unittest.TestCase):
    def hits(self, matcher, decl, value):
        out = []
        match_row("t", ["c"], [decl], Locator("rowid", 1), [value], matcher, out.append)
        return out

    def test_blob_in_a_text_column_and_text_in_a_blob_column(self):
        blob = b"\x00\x01" + le("Secret plan")
        self.assertEqual(self.hits(Matcher("secret", "ci", False), "TEXT", blob), [])
        for m in (Matcher("secret", "ci", True), Matcher("secret", "blob", False)):
            h = self.hits(m, "TEXT", blob)[0]
            self.assertEqual((h["encoding"], h["offset"], h["type"]), ("utf-16le", 2, "BLOB"))
            self.assertIn("Secret plan", h["value"])
        # text in a column declared BLOB: skipped by the plain text modes, as before
        self.assertEqual(self.hits(Matcher("secret", "ci", False), "BLOB", "a secret"), [])
        for m in (Matcher("secret", "ci", True), Matcher("secret", "blob", False)):
            h = self.hits(m, "BLOB", "a secret")[0]
            self.assertEqual((h["value"], h["encoding"], h["offset"], h["type"]),
                             ("a secret", "text", None, "TEXT"))

    def test_text_hits_are_unchanged(self):
        h = self.hits(Matcher("ell", "ci", False), "TEXT", "Hello")[0]
        self.assertEqual((h["value"], h["type"], h["encoding"], h["offset"]),
                         ("Hello", "TEXT", "text", None))
        self.assertEqual(self.hits(Matcher("5", "ci", False), "INTEGER", 15)[0]["value"], "15")

    def test_hex_matches_stored_bytes_of_text(self):
        m = Matcher("c3 bc", "hex", False)                     # 'ü' in UTF-8
        self.assertEqual(self.hits(m, "TEXT", "über")[0]["offset"], 0)
        m16 = Matcher("fc 00", "hex", False, encoding="utf-16-le")
        self.assertEqual(self.hits(m16, "TEXT", "xü")[0]["offset"], 2)
        self.assertEqual(self.hits(Matcher("31", "hex", False), "INTEGER", 1), [])

    def test_blob_mode_tries_hex_only_with_deep_blob_and_a_hex_term(self):
        data = b"\x00\xca\xfe"
        self.assertEqual(self.hits(Matcher("cafe", "blob", False), "BLOB", data), [])
        h = self.hits(Matcher("cafe", "blob", True), "BLOB", data)[0]
        self.assertEqual((h["type"], h["encoding"], h["offset"]), ("blob_hex", "hex", 1))
        self.assertEqual(self.hits(Matcher("coffee", "blob", True), "BLOB", data), [])

    def test_regex_on_blobs_needs_deep_blob(self):
        data = b"\x07" + le("id=4711")
        self.assertEqual(self.hits(Matcher(r"id=\d+", "rx", False), "TEXT", data), [])
        h = self.hits(Matcher(r"id=\d+", "rx", True), "TEXT", data)[0]
        self.assertEqual((h["encoding"], h["offset"]), ("utf-16le", 1))

    def test_anchored_modes_on_blobs(self):
        self.assertTrue(self.hits(Matcher("abc", "ex", True), "BLOB", b"abc\x00"))
        self.assertFalse(self.hits(Matcher("abc", "ex", True), "BLOB", b"abcd"))
        self.assertTrue(self.hits(Matcher("ABC", "sw", True), "BLOB", b"\xff\xfe" + le("abcd")))
        self.assertTrue(self.hits(Matcher("CD", "ew", True), "BLOB", be("abcd")))
        self.assertFalse(self.hits(Matcher("ab", "ew", True), "BLOB", be("abcd")))


if __name__ == "__main__":
    unittest.main()
