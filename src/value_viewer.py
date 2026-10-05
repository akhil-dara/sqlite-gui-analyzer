"""ValueViewer: the whole of one (possibly very large) text value, read-only.

Word-wrapped text with optional line numbers, find (Ctrl+F; Enter / Shift+Enter or F3 /
Shift+F3 for next / previous, with a match count), a wrap toggle, the size in characters,
lines and UTF-8 bytes, pretty-printing of JSON and XML that parses, and copy. Multi-megabyte
text is put into the widget in chunks from after() callbacks, so the window stays responsive
while it fills; everything else (counting, finding, pretty-printing) works on the Python string.
At most limits 'value_view_chars' characters are shown, and the window says when it cut.
"""

import bisect
import json
from array import array
import re
import threading
import tkinter as tk
from tkinter import ttk

from constants import C
from tokens import FONT as F
from engine import limits
from engine.fileformat.record import InvalidText

CHUNK = 200000              # characters put into the Text widget per step
FIND_MAX = 1000000          # matches located at most (the count says when there are more)


def value_text(v):
    """The text View value shows for a value (bytes: None, they have the BLOB Inspector)."""
    if v is None:
        return "NULL"
    if isinstance(v, InvalidText):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, (bytes, bytearray)):
        return None
    return v if isinstance(v, str) else str(v)


def looks_structured(text):
    """'json', 'xml' or None: what the text might be pretty-printed as."""
    s = text[:200].lstrip()
    if s[:1] in ("{", "["):
        return "json"
    if s[:1] == "<":
        return "xml"
    return None


def pretty(text):
    """(kind, pretty text) of JSON or XML that parses; raises ValueError otherwise. XML with a
    DTD is not parsed (its entities could expand without bound)."""
    kind = looks_structured(text)
    if kind == "json":
        try:
            return kind, json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except (ValueError, RecursionError) as e:
            raise ValueError("not JSON: %s" % e)
    if kind == "xml":
        # the tool's own XML reader: no DTD (internal subset) and no entity is ever expanded,
        # nesting and size are bounded, and it never reads anything but this text
        from engine.xmlpretty import pretty_xml
        return kind, pretty_xml(text, indent="  ")
    raise ValueError("neither JSON nor XML")


def counts_text(text):
    lines = text.count("\n") + 1 if text else 0
    return "%s chars · %s lines · %s bytes UTF-8" % (
        format(len(text), ","), format(lines, ","),
        format(len(text.encode("utf-8", "surrogatepass")), ","))


def layout_display(text, width):
    """(display text, starts, cont) for showing `text` with no line longer than `width`
    characters: long lines are broken into pieces (after a space near the end of a piece when
    there is one). starts[i] is the offset in `text` where display line i begins; cont is None
    when nothing was broken, else a bytearray with 1 for the lines that continue a piece.
    Without long lines the text is shown as it is and starts are its line starts."""
    if re.search("[^\n]{%d}" % (width + 1), text) is None:
        starts = array("q", [0])
        starts.extend(m.end() for m in re.finditer("\n", text))
        return text, starts, None
    out, starts, cont = [], array("q"), bytearray()
    pos = 0
    for line in text.split("\n"):
        i, n = 0, len(line)
        first = True
        while True:
            starts.append(pos + i)
            cont.append(0 if first else 1)
            first = False
            if n - i <= width:
                out.append(line[i:])
                break
            cut = line.rfind(" ", i + width // 2, i + width)
            cut = cut + 1 if cut > 0 else i + width
            out.append(line[i:cut])
            i = cut
        pos += n + 1
    return "\n".join(out), starts, cont


class ValueViewer(tk.Toplevel):
    def __init__(self, master, value, title="Value"):
        tk.Toplevel.__init__(self, master)
        # a preview of its parent window: minimize and restore with it, never left behind
        self._owner = master.winfo_toplevel()
        try:
            self.transient(self._owner)
        except tk.TclError:
            pass
        self._hidden_with_owner = False
        self._watch_after = None
        self.title(title)
        from widgets import fit_geometry
        fit_geometry(self, 820, 560)
        self.configure(bg=C["bg"])
        text = value_text(value)
        if text is None:
            text = ""
        self.total_chars = len(text)
        cap = limits.get("value_view_chars")
        self.cut_note = ""
        if len(text) > cap:
            text = text[:cap]
            self.cut_note = ("showing the first %s of %s characters (raise %s)"
                             % (format(cap, ","), format(self.total_chars, ","),
                                limits.hint("value_view_chars")))
        self._raw = text
        self._shown = text          # the raw text, or its pretty-printed form
        self._real_lines = None
        self._matches = []
        self._match_i = -1
        self._needle = None
        self._load_after = None
        self._loaded = 0
        self._pretty_job = self._pretty_box = None
        self._alive = True
        self._pretty_cache = {}
        self.pretty_error = ""

        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=8, pady=(6, 2))
        self.info = tk.Label(bar, text="", bg=C["bg"], fg=C["text2"], anchor="w")
        self.info.pack(side="left", fill="x", expand=True)
        self.wrap_var = tk.BooleanVar(value=True)
        self.lines_var = tk.BooleanVar(value=False)
        self.pretty_var = tk.BooleanVar(value=False)
        ttk.Button(bar, text="Copy", command=self.copy).pack(side="right", padx=2)
        self.pretty_cb = ttk.Checkbutton(bar, text="Pretty-print", variable=self.pretty_var,
                                         command=self._pretty_toggled)
        self.pretty_cb.pack(side="right", padx=4)
        if looks_structured(text) is None:
            self.pretty_cb.state(["disabled"])
        ttk.Checkbutton(bar, text="Line numbers", variable=self.lines_var,
                        command=self._lines_toggled).pack(side="right", padx=4)
        ttk.Checkbutton(bar, text="Wrap", variable=self.wrap_var,
                        command=self._wrap_toggled).pack(side="right", padx=4)

        fb = self.findbar = tk.Frame(self, bg=C["bg"])
        fb.pack(fill="x", padx=8, pady=2)
        tk.Label(fb, text="Find:", bg=C["bg"], fg=C["text"]).pack(side="left")
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Entry(fb, textvariable=self.find_var, width=36)
        self.find_entry.pack(side="left", padx=4)
        ttk.Button(fb, text="Next", command=self.find_next).pack(side="left", padx=2)
        ttk.Button(fb, text="Previous", command=self.find_prev).pack(side="left", padx=2)
        self.find_info = tk.Label(fb, text="", bg=C["bg"], fg=C["text2"])
        self.find_info.pack(side="left", padx=6)

        body = tk.Frame(self, bg=C["border"])
        body.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.gutter = tk.Canvas(body, width=56, bg=C["bg3"], highlightthickness=0, bd=0)
        self.text = tk.Text(body, wrap="word", undo=False, bd=0, highlightthickness=0,
                            font=F["mono_large"], bg=C["bg"], fg=C["text"],
                            insertwidth=0)
        ysb = ttk.Scrollbar(body, orient="vertical", command=self._yview)
        self.xsb = ttk.Scrollbar(body, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=lambda a, b: (ysb.set(a, b), self._draw_gutter()),
                            xscrollcommand=self.xsb.set)
        ysb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("cur", background=C["hl"], foreground=C["text"])
        self.text.bind("<Configure>", lambda e: self._draw_gutter())
        for w in (self, self.text, self.find_entry):
            w.bind("<Control-f>", lambda e: (self.focus_find(), "break")[1])
            w.bind("<F3>", lambda e: (self.find_next(), "break")[1])
            w.bind("<Shift-F3>", lambda e: (self.find_prev(), "break")[1])
            w.bind("<Escape>", lambda e: self.destroy())
        self.find_entry.bind("<Return>", lambda e: (self.find_next(), "break")[1])
        self.find_entry.bind("<Shift-Return>", lambda e: (self.find_prev(), "break")[1])
        self.text.bind("<Control-a>", lambda e: (self.text.tag_add("sel", "1.0", "end"),
                                                 "break")[1])
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._fill(self._shown)
        self._owner.bind("<Unmap>", self._on_owner_unmap, add="+")
        self._owner.bind("<Map>", self._on_owner_map, add="+")
        self._watch_min()

    # -- minimizing with the parent window ---------------------------------------------
    def _on_owner_unmap(self, _e):
        """The parent window was minimized (or withdrawn): hide with it."""
        try:
            if self._alive and self.winfo_viewable():
                self.withdraw()
                self._hidden_with_owner = True
        except tk.TclError:
            pass

    def _on_owner_map(self, _e):
        """The parent window is back: re-show a viewer hidden with it."""
        try:
            if self._hidden_with_owner and self._alive:
                self.deiconify()
        except tk.TclError:
            pass
        finally:
            self._hidden_with_owner = False

    def _watch_min(self):
        """Poll the owner twice a second: a fallback for platforms where <Unmap> does not
        fire on the parent window."""
        try:
            if not self._alive:
                return
            mapped = self._owner.winfo_ismapped()
        except tk.TclError:
            return
        if not mapped:
            self._on_owner_unmap(None)
        else:
            self._on_owner_map(None)
        if self._alive:
            self._watch_after = self.after(500, self._watch_min)

    def _stop_watch(self):
        if self._watch_after is not None:
            try:
                self.after_cancel(self._watch_after)
            except tk.TclError:
                pass
            self._watch_after = None

    # -- filling ------------------------------------------------------------------------
    def _fill(self, text):
        """Put `text` into the widget, CHUNK characters per event-loop turn. Lines longer than
        limits 'value_view_line_chars' are shown in pieces (Tk slows to a crawl on very long
        lines); copy and find still work on the text itself."""
        if self._load_after is not None:
            self.after_cancel(self._load_after)
            self._load_after = None
        self._shown = text
        self._counts = counts_text(text)
        self._real_lines = None
        self._display, self._starts, self._cont = layout_display(
            text, limits.get("value_view_line_chars"))
        self._matches, self._match_i, self._needle = [], -1, None
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._loaded = 0
        self._show_info()
        self._load_step()

    def _load_step(self):
        self._load_after = None
        if not self._alive:
            return
        text = self._display
        if self._loaded < len(text):
            end = min(len(text), self._loaded + CHUNK)
            self.text.configure(state="normal")
            self.text.insert("end-1c", text[self._loaded:end])
            self.text.configure(state="disabled")
            self._loaded = end
            self._show_info()
            if self._loaded < len(text):
                self._load_after = self.after(1, self._load_step)
                return
        self._show_info()
        self._draw_gutter()
        if self.find_var.get():
            self._refind(keep=True)

    def loading(self):
        return self._loaded < len(self._display) or self._pretty_job is not None

    def _show_info(self):
        parts = [self._counts]
        if self._shown is not self._raw:
            parts.append("pretty-printed (%s raw chars)" % format(len(self._raw), ","))
        if self._cont is not None:
            parts.append("lines over %s characters shown in pieces (↪)"
                         % format(limits.get("value_view_line_chars"), ","))
        if self._loaded < len(self._display):
            parts.append("loading %d%%" % (100 * self._loaded // max(1, len(self._display))))
        if self._pretty_job is not None:
            parts.append("pretty-printing…")
        if self.pretty_error:
            parts.append(self.pretty_error)
        if self.cut_note:
            parts.append(self.cut_note)
        self.info.configure(text="  |  ".join(parts))

    def info_text(self):
        return self.info.cget("text")

    def shown_text(self):
        return self._shown

    # -- toggles ---------------------------------------------------------------------------
    def _wrap_toggled(self):
        wrap = self.wrap_var.get()
        self.text.configure(wrap="word" if wrap else "none")
        if wrap:
            self.xsb.pack_forget()
        else:
            self.xsb.pack(side="bottom", fill="x", before=self.text)
        self._draw_gutter()

    def _lines_toggled(self):
        if self.lines_var.get():
            self.gutter.pack(side="left", fill="y", before=self.text)
        else:
            self.gutter.pack_forget()
        self._draw_gutter()

    def _pretty_toggled(self):
        self.pretty_error = ""
        if not self.pretty_var.get():
            self._fill(self._raw)
            return
        hit = self._pretty_cache.get("text")
        if hit is not None:
            self._fill(hit)
            return
        raw = self._raw
        box = {}

        def work():
            try:
                box["out"] = pretty(raw)[1]
            except Exception as e:      # noqa: BLE001 - any parse failure is only reported
                box["error"] = str(e)
        th = threading.Thread(target=work, name="value-pretty")
        th.daemon = True
        self._pretty_job, self._pretty_box = th, box
        th.start()
        self._show_info()
        self._poll_pretty()

    def _poll_pretty(self):
        """Wait (via after()) for the pretty-printing thread; it never touches Tk."""
        th = self._pretty_job
        if th is None or not self._alive:
            return
        if th.is_alive():
            self.after(20, self._poll_pretty)
            return
        self._pretty_job = None
        box = self._pretty_box
        if not self.pretty_var.get():
            self._show_info()           # switched back to raw meanwhile
            return
        if "out" in box:
            self._pretty_cache["text"] = box["out"]
            self._fill(box["out"])
        else:
            self.pretty_error = "not pretty-printed: %s" % box.get("error", "")
            self.pretty_var.set(False)
            self._show_info()

    def _on_destroy(self, e):
        if e.widget is self:
            self._alive = False
            self._stop_watch()
        if e.widget is self and self._load_after is not None:
            try:
                self.after_cancel(self._load_after)
            except tk.TclError:
                pass
            self._load_after = None

    # -- line numbers ------------------------------------------------------------------------
    def _yview(self, *args):
        self.text.yview(*args)
        self._draw_gutter()

    def _draw_gutter(self):
        if not self.lines_var.get():
            return
        g = self.gutter
        g.delete("all")
        idx = self.text.index("@0,0")
        last = None
        while True:
            info = self.text.dlineinfo(idx)
            if info is None:
                break
            line = idx.split(".")[0]
            if line != last:
                g.create_text(50, info[1] + 1, anchor="ne", text=self._line_label(int(line)),
                              fill=C["text2"], font=F["mono"])
                last = line
            nxt = self.text.index("%s+1display lines" % idx)
            if nxt == idx:
                break
            idx = nxt

    def _line_label(self, display_line):
        """The line number shown for a display line ('↪' for a piece of a long line)."""
        cont = self._cont
        if cont is None:
            return str(display_line)
        i = display_line - 1
        if 0 <= i < len(cont) and cont[i]:
            return "↪"
        if self._real_lines is None or len(self._real_lines) != len(cont):
            nums, n = array("l"), 0
            for c in cont:
                n += 0 if c else 1
                nums.append(n)
            self._real_lines = nums         # the text's own line number of each display line
        return str(self._real_lines[i]) if 0 <= i < len(cont) else ""

    # -- find ------------------------------------------------------------------------------------
    def focus_find(self):
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")

    def _offset_index(self, off):
        """Tk text index of a character offset into the shown text."""
        line = max(0, bisect.bisect_right(self._starts, off) - 1)
        return "%d.%d" % (line + 1, off - self._starts[line])

    def _index_offset(self, index):
        """Character offset into the shown text of a Tk text index."""
        line, col = (int(x) for x in self.text.index(index).split("."))
        line = min(max(1, line), len(self._starts))
        return self._starts[line - 1] + col

    def _refind(self, keep=False):
        needle = self.find_var.get()
        if not needle:
            self._matches, self._match_i, self._needle = [], -1, None
            self.find_info.configure(text="")
            return
        hay = self._shown.lower()
        low = needle.lower()
        found, pos = [], hay.find(low)
        while pos >= 0 and len(found) < FIND_MAX:
            found.append(pos)
            pos = hay.find(low, pos + max(1, len(low)))
        self._matches = found
        self._needle = needle
        if not keep:
            self._match_i = -1

    def _go(self, step):
        if self.find_var.get() != self._needle:
            self._refind()
        n = len(self._matches)
        if not n:
            self.find_info.configure(text="no matches" if self._needle else "")
            return None
        self._match_i = (self._match_i + step) % n if self._match_i >= 0 else \
            (0 if step > 0 else n - 1)
        off = self._matches[self._match_i]
        more = "+" if n >= FIND_MAX else ""
        self.find_info.configure(text="%s of %s%s" % (format(self._match_i + 1, ","),
                                                      format(n, ","), more))
        if self._loaded < len(self._display):
            end_line = bisect.bisect_right(self._starts, off + len(self._needle))
            if end_line > int(self.text.index("end-1c").split(".")[0]):
                self.find_info.configure(text=self.find_info.cget("text") + " (still loading)")
                return off
        a = self._offset_index(off)
        b = self._offset_index(off + len(self._needle))
        self.text.tag_remove("cur", "1.0", "end")
        self.text.tag_add("cur", a, b)
        self.text.see(a)
        self._draw_gutter()
        return off

    def find_next(self):
        return self._go(1)

    def find_prev(self):
        return self._go(-1)

    def match_count(self):
        return len(self._matches)

    # -- copy --------------------------------------------------------------------------------------
    def copy(self):
        """Copy the selection, or the whole text shown."""
        try:
            a, b = self._index_offset("sel.first"), self._index_offset("sel.last")
            text = self._shown[a:b]         # the text itself, without the display breaks
        except tk.TclError:
            text = self._shown
        self.clipboard_clear()
        self.clipboard_append(text)
        return text
