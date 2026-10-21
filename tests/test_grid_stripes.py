"""The grid's plain rows are one striped image (zebra and grid lines), only special rows get
their own rectangle; the stripes stop at the table's last row and follow the theme."""
import time
import unittest

import tests.helpers  # noqa: F401 - the src path
from tests.helpers import tk_root


class Rows(object):
    threaded = False

    def __init__(self, n):
        self.n = n

    def columns(self):
        return ["#", "a", "b"]

    def row_count(self):
        return self.n

    def rows(self, start, count):
        return [((i, "a%d" % i, "b%d" % i), ()) for i in range(start, min(self.n, start + count))]


class StripesTest(unittest.TestCase):
    def setUp(self):
        self.root = tk_root(self)
        self.root.geometry("900x500+-4000+0")
        self.root.deiconify()
        from grid import DataGrid
        self.g = DataGrid(self.root)
        self.g.pack(fill="both", expand=True)

    def pump(self, t=0.3):
        end = time.time() + t
        while time.time() < end:
            self.root.update()
            time.sleep(0.01)

    def test_plain_rows_are_the_image(self):
        from constants import C
        g = self.g
        g.set_source(Rows(500))
        self.pump()
        surf = g._surf[g._cv]
        self.assertIsNotNone(surf.stripe_item)
        self.assertEqual(g._cv.itemcget(surf.stripe_item, "state"), "normal")
        shown = [k for k, v in surf.rowbg.items.items() if v is not None]
        self.assertEqual(shown, [])                     # no rectangle for a plain row
        self.assertEqual(g.row_background(1), C["alt"])
        self.assertEqual(g.row_background(2), C["bg"])
        key = surf.stripe_key
        g.scroll_rows(3)
        self.pump(0.2)
        g.scroll_rows(3)
        self.pump(0.2)
        self.assertEqual(surf.stripe_key, key)          # moved, not rebuilt
        g.set_current_cell(4, 1)
        self.pump(0.2)
        self.assertEqual(g.row_background(4), C["tsel"])
        self.assertIn(4, [k for k, v in surf.rowbg.items.items() if v is not None])

    def test_stripes_stop_at_the_last_row(self):
        g = self.g
        g.set_source(Rows(3))
        self.pump()
        surf = g._surf[g._cv]
        self.assertEqual(surf.stripe_key[1], 3)         # three rows, not the view's height
        g.set_source(Rows(0))
        self.pump()
        self.assertEqual(g._cv.itemcget(surf.stripe_item, "state"), "hidden")


if __name__ == "__main__":
    unittest.main()
