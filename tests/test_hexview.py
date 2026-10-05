import unittest

from tests import helpers  # noqa: F401  (sets sys.path)
from hexview import (BYTES_PER_LINE, HexView, ascii_col, format_line, hex_col, line_count,
                     offset_at, parse_offset)


class LayoutTest(unittest.TestCase):
    def test_line_text(self):
        data = bytes(range(0x41, 0x41 + 20))
        self.assertEqual(format_line(data, 0),
                         "00000000  41 42 43 44 45 46 47 48  49 4a 4b 4c 4d 4e 4f 50  |ABCDEFGHIJKLMNOP|")
        self.assertEqual(format_line(data, 1),
                         "00000010  51 52 53 54" + " " * 37 + "  |QRST            |")
        self.assertEqual(format_line(b"\x00\x7f\xff", 0)[-18:], "|...             |")

    def test_columns_map_back_to_offsets(self):
        data = bytes(40)
        line = format_line(bytes(range(16)), 0)
        for i in range(BYTES_PER_LINE):
            self.assertEqual(line[hex_col(i):hex_col(i) + 2], "%02x" % i)
            for col in (hex_col(i), hex_col(i) + 1, ascii_col(i)):
                self.assertEqual(offset_at(len(data), 1, col), 16 + i)
        self.assertIsNone(offset_at(len(data), 0, 3))               # the offset column
        self.assertIsNone(offset_at(len(data), 2, hex_col(15)))     # past the end (40 bytes)
        self.assertEqual(offset_at(len(data), 2, hex_col(7)), 39)

    def test_parse_offset(self):
        for text, want in (("0x1f", 31), ("1fh", 31), ("31", 31), ("ff", 255), ("", None),
                           ("zz", None), ("0x", None), (" 0X10 ", 16)):
            self.assertEqual(parse_offset(text), want, text)

    def test_line_count(self):
        self.assertEqual([line_count(n) for n in (0, 1, 16, 17)], [1, 1, 1, 2])


class WidgetTest(unittest.TestCase):
    def setUp(self):
        self.root = helpers.tk_root(self)
        self.root.deiconify()
        self.root.geometry("+-4000+0")

    def view(self, data):
        hv = HexView(self.root, data)
        hv.pack(fill="both", expand=True)
        self.root.update()
        return hv

    def rendered_lines(self, hv):
        return [l for l in hv.text.get("1.0", "end-1c").split("\n") if l]

    def test_renders_only_visible_lines_of_a_large_blob(self):
        data = bytes(i % 251 for i in range(8 * 1024 * 1024))
        hv = self.view(data)
        lines = self.rendered_lines(hv)
        first, last = hv.visible_lines()
        self.assertEqual(len(lines), last - first + 1)
        self.assertLess(len(lines), 200)
        hv.goto(5 * 1024 * 1024 + 3)
        first, last = hv.visible_lines()
        self.assertTrue(first <= (5 * 1024 * 1024 + 3) // 16 <= last)
        self.assertIn("%08x" % (5 * 1024 * 1024), hv.text.get("1.0", "end"))

    def test_highlight_marks_hex_and_ascii_of_the_range(self):
        data = b"xxxxHELLOxxxxxxxxxxxxxxxxxx"
        hv = self.view(data)
        hv.highlight(4, 5)
        ranges = hv.text.tag_ranges("hl")
        texts = [hv.text.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)]
        self.assertEqual(texts, ["48 45 4c 4c  4f", "HELLO"])   # spans the mid-line gap
        hv.highlight(None, 0)
        self.assertEqual(hv.text.tag_ranges("hl"), ())

    def test_highlight_across_lines_and_find(self):
        data = b"a" * 14 + b"NEEDLE" + b"b" * 30
        hv = self.view(data)
        self.assertEqual(hv.find(b"NEEDLE"), 14)
        ranges = hv.text.tag_ranges("hl")
        joined = "".join(hv.text.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2))
        self.assertIn("4e 45", joined)
        self.assertIn("NE", joined)
        self.assertIn("EDLE", joined)
        self.assertIsNone(hv.find(b"absent"))

    def test_click_reports_the_byte_offset(self):
        got = []
        hv = self.view(bytes(range(64)))
        hv.on_select = got.append
        for idx, want in (("2.%d" % hex_col(3), 19), ("3.%d" % ascii_col(15), 47)):
            x, y, _w, _h = hv.text.bbox(idx)
            hv._on_click(type("E", (), {"x": x + 1, "y": y + 1})())
            self.assertEqual(got[-1], want)
        self.assertEqual(hv.text.tag_ranges("cursor") != (), True)

    def test_empty_and_replaced_data(self):
        hv = self.view(b"")
        self.assertEqual(hv.text.get("1.0", "end-1c"), "(empty)")
        hv.set_data(b"abc")
        self.assertEqual(len(self.rendered_lines(hv)), 1)


if __name__ == "__main__":
    unittest.main()
