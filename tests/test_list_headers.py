"""Every list's header text sits over its values: left-aligned, or right-aligned over a
right-aligned (numeric) column (widgets._align_headings, app-wide)."""
import unittest
from tkinter import ttk

from tests.helpers import tk_root
import widgets  # noqa: F401 - installs the alignment


class HeaderAlignmentTest(unittest.TestCase):
    def test_headers_follow_their_columns(self):
        root = tk_root(self)
        t = ttk.Treeview(root, columns=("name", "rows", "size"), show="tree headings")
        try:
            t.column("rows", width=60, anchor="e")
            t.column("size", width=60, anchor="ne")
            t.heading("name", text="Name")           # a later heading() keeps the anchor
            self.assertEqual(str(t.heading("#0", "anchor")), "w")
            self.assertEqual(str(t.heading("name", "anchor")), "w")
            self.assertEqual(str(t.heading("rows", "anchor")), "e")
            self.assertEqual(str(t.heading("size", "anchor")), "e")
            t.column("rows", anchor="w")               # back to the left: the header follows
            self.assertEqual(str(t.heading("rows", "anchor")), "w")
        finally:
            t.destroy()


if __name__ == "__main__":
    unittest.main()
