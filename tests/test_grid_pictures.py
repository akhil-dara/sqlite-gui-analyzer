"""Picture mode of the grid: image BLOBs drawn as thumbnails (taller rows), other values as
before, a too-large image refused by its declared size."""
import os
import struct
import sys
import time
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tests.helpers import tk_root  # noqa: E402


def png(w, h, rgb=(200, 30, 30)):
    """A w x h PNG of one colour, built with the standard library."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class PicturesTest(unittest.TestCase):
    def setUp(self):
        from browse_sources import ListSource
        from grid import DataGrid
        self.root = tk_root(self)
        self.root.geometry("800x400+-4000+0")
        self.root.deiconify()           # drawn (off the screen): a withdrawn grid draws nothing
        self.g = DataGrid(self.root, frozen=0)
        self.g.pack(fill="both", expand=True)
        rows = [([1, png(120, 90)], ()), ([2, b"not an image"], ()), ([3, None], ()),
                ([4, png(20, 20, (0, 90, 200))], ())]
        self.g.set_source(ListSource(["id", "data"], rows))
        self.settle()

    def settle(self, t=0.3):
        end = time.time() + t
        while time.time() < end:
            self.root.update()
            time.sleep(0.01)

    def test_thumbnails_and_taller_rows(self):
        g = self.g
        rh = g._rh
        g.set_pictures(True)
        self.settle()
        self.assertGreaterEqual(g._rh, 48 + 8)
        self.assertGreater(g._rh, rh)
        self.assertEqual(len(g._cv.find_withtag("pic")), 2)     # the two images, not the rest
        img = g._picture(png(120, 90))
        self.assertLessEqual(img.width(), 64)
        self.assertLessEqual(img.height(), 48)
        self.assertIsNone(g._picture(b"not an image"))
        text, _fill, _fi = g._cell(png(20, 20), 1)
        self.assertTrue(text.startswith(" "))              # room for the thumbnail
        self.assertIn("PNG", text)
        g.set_pictures(False)
        self.settle()
        self.assertEqual(g._rh, rh)
        self.assertEqual(g._cv.find_withtag("pic"), ())

    def test_an_image_too_large_for_preview_pixels_is_not_decoded(self):
        from engine import limits
        old = limits.current()
        limits.load({"limits": dict(old, preview_pixels=10000)})
        self.addCleanup(limits.load, {"limits": old})
        self.g.set_pictures(True)
        self.assertIsNone(self.g._picture(png(120, 90)))       # 10,800 pixels > 10,000
        self.assertIsNotNone(self.g._picture(png(90, 90)))     # 8,100: drawn

    def test_without_pillow_tk_reads_png(self):
        import constants
        from unittest import mock
        with mock.patch.object(constants, "HAS_PIL", False):
            self.g.set_pictures(True)
            self.g._pics.clear()
            img = self.g._picture(png(120, 90))
            self.assertIsNotNone(img)
            self.assertLessEqual(img.width(), 64)       # reduced by a whole factor (2)
            self.assertLessEqual(img.height(), 48)
            jpeg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes(200)
            self.assertIsNone(self.g._picture(jpeg))    # JPEG needs Pillow: no thumbnail


if __name__ == "__main__":
    unittest.main()
