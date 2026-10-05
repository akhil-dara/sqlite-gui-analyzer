"""Hex viewer widget for BLOBs of any size.

Only the lines that fit in the window are rendered, so a 64 MB BLOB scrolls as smoothly as a
64-byte one. A byte range can be highlighted (the bytes behind a decoded value), and clicking
a byte reports its offset, which lets an inspector select the value that byte belongs to.

Line layout (16 bytes per line):
    00000010  6e 67 00 5f 10 0f 4e 53  2e 6f 62 6a 65 63 74 73  |ng._..NS.objects|
"""

import tkinter as tk
from tkinter import ttk

from tokens import COLOR as K, FONT as F

BYTES_PER_LINE = 16
HEX_START = 10                      # after the 8-digit offset and two spaces
ASCII_START = HEX_START + BYTES_PER_LINE * 3 + 2      # hex digits, the mid gap, two spaces


def offset_width(size):
    """Hex digits used for offsets: 8, or more for buffers of 4 GiB and over."""
    return max(8, len("%x" % max(size - 1, 0)))


def hex_col(i):
    """Text column of the first hex digit of byte i (0-15) of a line."""
    return HEX_START + i * 3 + (1 if i >= 8 else 0)


def ascii_col(i):
    return ASCII_START + 1 + i                            # after the opening '|'


def printable(b):
    return chr(b) if 32 <= b < 127 else "."


def format_line(data, line):
    """The text of line `line` of `data` (offset, hex, ASCII)."""
    start = line * BYTES_PER_LINE
    chunk = data[start:start + BYTES_PER_LINE]
    cells = []
    for i in range(BYTES_PER_LINE):
        cells.append("%02x" % chunk[i] if i < len(chunk) else "  ")
    hex_part = " ".join(cells[:8]) + "  " + " ".join(cells[8:])
    ascii_part = "".join(printable(b) for b in chunk)
    return "%08x  %s  |%s|" % (start, hex_part, ascii_part.ljust(BYTES_PER_LINE))


def offset_at(data_len, line, col):
    """Byte offset under text column `col` of line `line`, or None (offset column, gaps)."""
    base = line * BYTES_PER_LINE
    for i in range(BYTES_PER_LINE):
        if hex_col(i) <= col <= hex_col(i) + 1 or col == ascii_col(i):
            off = base + i
            return off if off < data_len else None
    return None


def line_count(size):
    return max(1, (size + BYTES_PER_LINE - 1) // BYTES_PER_LINE)


def parse_offset(text):
    """'0x1f', '1f h', '31' (decimal) -> int, or None. A bare hex word like 'ff' is hex."""
    s = text.strip().lower().replace("_", "")
    if not s:
        return None
    try:
        if s.startswith("0x"):
            return int(s[2:], 16)
        if s.endswith("h"):
            return int(s[:-1].strip(), 16)
        if s.isdigit():
            return int(s)
        return int(s, 16)
    except ValueError:
        return None


class HexView(ttk.Frame):
    """Virtual hex dump. on_select(offset) is called when the user clicks a byte."""

    def __init__(self, parent, data=b"", on_select=None, font=None, **kw):
        ttk.Frame.__init__(self, parent, **kw)
        font = font if font is not None else F["mono"]
        self._data = b""
        self._top = 0                   # first rendered line
        self._rows = 1                  # lines that fit
        self._hl = None                 # (offset, length) highlighted range
        self._cursor = None             # the clicked byte
        self.on_select = on_select
        self.text = tk.Text(self, font=font, wrap="none", height=20, width=78,
                            bg=K["card"], fg=K["text"], insertbackground=K["text"],
                            selectbackground=K["selection"], selectforeground=K["text"],
                            cursor="arrow", borderwidth=0, highlightthickness=0,
                            padx=4, pady=2)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self._on_scrollbar)
        self.scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("off", foreground=K["placeholder"])
        self.text.tag_configure("hl", background=K["highlight"])
        self.text.tag_configure("cursor", background=K["selection"])
        self.text.bind("<Configure>", lambda e: self._render())
        self.text.bind("<MouseWheel>", self._on_wheel)
        self.text.bind("<Button-4>", lambda e: self.scroll_lines(-3))
        self.text.bind("<Button-5>", lambda e: self.scroll_lines(3))
        self.text.bind("<Button-1>", self._on_click)
        for key, delta in (("<Up>", -1), ("<Down>", 1), ("<Prior>", "-page"), ("<Next>", "page")):
            self.text.bind(key, lambda e, d=delta: self._on_key(d))
        self.text.bind("<Home>", lambda e: (self.goto_line(0), "break")[1])
        self.text.bind("<End>", lambda e: (self.goto_line(line_count(len(self._data))), "break")[1])
        self.set_data(data)

    # -- data and view ------------------------------------------------------
    def set_data(self, data):
        self._data = bytes(data or b"")
        self._top = 0
        self._hl = self._cursor = None
        self._render()

    @property
    def data(self):
        return self._data

    def visible_lines(self):
        """(first, last) line numbers currently rendered."""
        return self._top, min(self._top + self._rows, line_count(len(self._data))) - 1

    def _fit_rows(self):
        try:
            import tkinter.font as tkfont
            line_px = tkfont.Font(font=self.text.cget("font")).metrics("linespace")
        except tk.TclError:
            line_px = 15
        height = self.text.winfo_height()
        if height <= 1:
            height = int(self.text.cget("height")) * line_px
        return max(1, height // max(line_px, 1))

    def _render(self):
        self._rows = self._fit_rows()
        total = line_count(len(self._data))
        self._top = max(0, min(self._top, total - self._rows))
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        last = min(self._top + self._rows, total)
        width = offset_width(len(self._data))
        lines = []
        for n in range(self._top, last):
            line = format_line(self._data, n)
            if width > 8:          # widen the offset column for very large buffers
                line = ("%0*x" % (width, n * BYTES_PER_LINE)) + line[8:]
            lines.append(line)
        t.insert("1.0", "\n".join(lines) if self._data else "(empty)")
        extra = width - 8
        for row in range(1, last - self._top + 1):
            t.tag_add("off", "%d.0" % row, "%d.%d" % (row, 8 + extra))
        self._paint_range(self._hl, "hl", extra)
        if self._cursor is not None:
            self._paint_range((self._cursor, 1), "cursor", extra)
        t.configure(state="disabled")
        if total <= self._rows:
            self.scroll.set(0.0, 1.0)
        else:
            self.scroll.set(self._top / float(total), last / float(total))

    def _paint_range(self, rng, tag, extra):
        if not rng or rng[1] <= 0:
            return
        start, length = rng
        end = start + length                                   # exclusive
        first = max(start // BYTES_PER_LINE, self._top)
        last = min((end - 1) // BYTES_PER_LINE, self._top + self._rows - 1)
        for n in range(first, last + 1):
            row = n - self._top + 1
            a = max(start, n * BYTES_PER_LINE) - n * BYTES_PER_LINE
            b = min(end, (n + 1) * BYTES_PER_LINE) - n * BYTES_PER_LINE - 1
            if a > b:
                continue
            self.text.tag_add(tag, "%d.%d" % (row, hex_col(a) + extra),
                              "%d.%d" % (row, hex_col(b) + 2 + extra))
            self.text.tag_add(tag, "%d.%d" % (row, ascii_col(a) + extra),
                              "%d.%d" % (row, ascii_col(b) + 1 + extra))

    # -- navigation ------------------------------------------------------------
    def goto_line(self, line):
        self._top = max(0, int(line))
        self._render()

    def scroll_lines(self, n):
        self.goto_line(self._top + n)

    def ensure_visible(self, offset):
        line = offset // BYTES_PER_LINE
        if not self._top <= line < self._top + self._rows:
            self.goto_line(max(0, line - self._rows // 3))

    def goto(self, offset):
        """Show and mark the byte at `offset` (clamped to the data)."""
        if not self._data:
            return
        offset = max(0, min(int(offset), len(self._data) - 1))
        self._cursor = offset
        self.ensure_visible(offset)
        self._render()

    def highlight(self, offset, length):
        """Highlight `length` bytes from `offset` and scroll them into view (None clears)."""
        if offset is None or not length:
            self._hl = None
        else:
            self._hl = (max(0, int(offset)), int(length))
            self.ensure_visible(self._hl[0])
        self._render()

    def find(self, needle, start=0):
        """Offset of the next occurrence of bytes `needle` from `start` (wrapping), or None.
        The match is highlighted."""
        if not needle or not self._data:
            return None
        pos = self._data.find(needle, start)
        if pos < 0:
            pos = self._data.find(needle, 0)
        if pos < 0:
            return None
        self.highlight(pos, len(needle))
        return pos

    # -- events --------------------------------------------------------------
    def _on_scrollbar(self, *args):
        total = line_count(len(self._data))
        if args[0] == "moveto":
            self.goto_line(int(float(args[1]) * total))
        elif args[0] == "scroll":
            step = int(args[1]) * (self._rows if args[2] == "pages" else 1)
            self.scroll_lines(step)

    def _on_wheel(self, event):
        self.scroll_lines(-3 * int(event.delta / 120) if event.delta else 0)
        return "break"

    def _on_key(self, delta):
        if delta == "page":
            delta = self._rows
        elif delta == "-page":
            delta = -self._rows
        self.scroll_lines(delta)
        return "break"

    def _on_click(self, event):
        self.text.focus_set()
        idx = self.text.index("@%d,%d" % (event.x, event.y))
        row, col = (int(p) for p in idx.split("."))
        extra = offset_width(len(self._data)) - 8
        off = offset_at(len(self._data), self._top + row - 1, col - extra)
        if off is None:
            return None
        self._cursor = off
        self._render()
        if self.on_select is not None:
            self.on_select(off)
        return None
