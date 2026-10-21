"""FastTree: a list of rows in columns, with expandable child rows, that scrolls fast.

A ttk.Treeview repaints every visible cell on each scroll step: on some machines ~0.6 ms a
cell, 100-400 ms a wheel step for a full window of rows. FastTree draws its rows into a Tk
Text widget, which scrolls by copying what is on screen and drawing only the lines that come
into view (measured: 18 ms a wheel step where the Treeview took 200). It offers the part of
the ttk.Treeview interface the tool's lists use, with the same meaning:

    insert(parent, index, iid=None, text="", values=(), tags=(), open=False) -> iid
    delete(*iids)   get_children(item="")   exists(iid)   parent(iid)   index(iid)
    item(iid[, option][, values=, tags=, open=, text=])   set(iid, column[, value])
    heading(column[, option][, text=, command=])   column(column[, option][, width=,
    minwidth=, stretch=, anchor=])   tag_configure(tag, background=, foreground=, font=)
    selection()   selection_set(iids)   selection_add / selection_remove   focus([iid])
    see(iid)   identify_row(y)   identify_column(x)   bbox(iid[, column])
    yview(...)   xview(...)   configure(xscrollcommand=, yscrollcommand=)   cget()
    bind(sequence, func, add)   event_generate   focus_set

Rows are kept in a model; the text is rebuilt on idle after changes (a burst of inserts is
one rebuild), or at once when something needs the drawn lines (see, bbox, identify_*).
Column "#0" holds the open / closed mark of rows with children; a click on it toggles the
row, Right / Left open and close it, Up / Down / Page / Home / End move the selection,
Shift and Ctrl extend it. <<TreeviewSelect>> is sent when the selection changes, and
<<TreeviewOpen>> / <<TreeviewClose>> when a row opens or closes, as a Treeview does. A cell
too long for its column is cut with an ellipsis; the whole value stays in item(..., values).
"""

import tkinter as tk
import tkinter.font as tkfont

from constants import C
from tokens import COLOR as K

INDENT = 18          # px a child row's first cell is indented by


class _Row(object):
    __slots__ = ("iid", "parent", "children", "text", "values", "tags", "open")

    def __init__(self, iid, parent, text, values, tags, open_):
        self.iid, self.parent, self.children = iid, parent, []
        self.text, self.values, self.tags, self.open = text, tuple(values), tuple(tags), open_


class FastTree(tk.Frame):
    def __init__(self, master, columns=(), show="tree headings", selectmode="extended",
                 height=None, **kw):
        tk.Frame.__init__(self, master, bg=C["bg"], bd=0, highlightthickness=0)
        self._columns = tuple(columns)
        self._show_tree = "tree" in str(show)
        self._show_head = "headings" in str(show)
        self._selectmode = selectmode
        self._font = tkfont.nametofont("TkDefaultFont")
        self._head_font = tkfont.nametofont("TkHeadingFont")
        self._rows = {"": _Row("", None, "", (), (), True)}
        self._next = 0
        self._sel = []
        self._focus = None
        self._anchor = None
        self._lines = []                 # iids in display order (line n+1 of the text)
        self._line_of = {}               # iid -> its line (0-based), rows shown only
        self._dirty = False
        self._after = None
        self._tags = {}                  # tag -> options given
        cols = ["#0"] + list(self._columns)
        self._cols = cols
        self._width = dict((c, 24 if c == "#0" else 120) for c in cols)
        self._minwidth = dict((c, 20) for c in cols)
        self._stretch = dict((c, c != "#0") for c in cols)
        self._heading = dict((c, {"text": "" if c == "#0" else c, "command": None})
                             for c in cols)
        self._xcmd = self._ycmd = None
        self._yfirst = 0.0               # the first fraction shown (scroll callback)
        self._xfirst = 0.0
        lh = self._font.metrics("linespace") + 5
        self._header = tk.Canvas(self, height=self._head_font.metrics("linespace") + 8,
                                 bg=K["muted"],
                                 highlightthickness=0, bd=0, xscrollincrement=1)
        if self._show_head:
            self._header.pack(fill="x", side="top")
        self._text = tk.Text(self, wrap="none", cursor="arrow", bd=0, highlightthickness=0,
                             relief="flat", font=self._font, bg=C["bg"], fg=C["text"],
                             spacing1=2, spacing3=3, padx=0, pady=0, takefocus=1,
                             insertwidth=0, exportselection=False,
                             height=height or 10)
        self._text.pack(fill="both", expand=True)
        self._text.configure(yscrollcommand=self._on_y, xscrollcommand=self._on_x)
        self._text.tag_configure("fl_sel", background=K["selection"], foreground=K["text"])
        self._line_h = lh
        # the Text's own class bindings (editing, text selection) are left out; the list's
        # own handlers come first, then whatever the caller binds
        self._text.bindtags((str(self._text), "FastTree%d" % id(self),
                             str(self.winfo_toplevel()), "all"))
        own = "FastTree%d" % id(self)
        for seq, fn in (("<Button-1>", self._on_click), ("<Shift-Button-1>",
                        lambda e: self._on_click(e, "shift")),
                        ("<Control-Button-1>", lambda e: self._on_click(e, "ctrl")),
                        ("<Button-3>", self._on_right_press),
                        ("<Up>", lambda e: self._move(-1, e)), ("<Down>", lambda e: self._move(1, e)),
                        ("<Shift-Up>", lambda e: self._move(-1, e, True)),
                        ("<Shift-Down>", lambda e: self._move(1, e, True)),
                        ("<Prior>", lambda e: self._move(-self._page(), e)),
                        ("<Next>", lambda e: self._move(self._page(), e)),
                        ("<Home>", lambda e: self._move(-10 ** 9, e)),
                        ("<End>", lambda e: self._move(10 ** 9, e)),
                        ("<Right>", lambda e: self._open_focus(True)),
                        ("<Left>", lambda e: self._open_focus(False)),
                        ("<MouseWheel>", self._on_wheel),
                        ("<Shift-MouseWheel>", lambda e: self._on_wheel(e, True)),
                        ("<Button-4>", lambda e: self.yview_scroll(-3, "units")),
                        ("<Button-5>", lambda e: self.yview_scroll(3, "units")),
                        ("<Control-a>", self._select_all)):
            self._text.bind_class(own, seq, fn)
        self._header.bind("<Button-1>", self._on_head_click)
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Map>", self._on_map, add="+")

    # -- model ------------------------------------------------------------------------------
    def insert(self, parent, index, iid=None, text="", values=(), tags=(), open=False,
               **_kw):
        parent = parent or ""
        if parent not in self._rows:
            raise tk.TclError("Item %s not found" % parent)
        if iid is None:
            self._next += 1
            iid = "I%03d" % self._next
            while iid in self._rows:
                self._next += 1
                iid = "I%03d" % self._next
        elif iid in self._rows:
            raise tk.TclError("Item %s already exists" % iid)
        if isinstance(tags, str):
            tags = (tags,)
        row = _Row(iid, parent, text, values, tags, bool(open))
        self._rows[iid] = row
        kids = self._rows[parent].children
        if index == "end" or index is None or int(index) >= len(kids):
            kids.append(iid)
        else:
            kids.insert(max(0, int(index)), iid)
        self._changed()
        return iid

    def delete(self, *iids):
        gone = set()
        for iid in iids:
            if iid not in self._rows or iid == "":
                continue
            stack = [iid]
            while stack:
                x = stack.pop()
                gone.add(x)
                stack.extend(self._rows[x].children)
            row = self._rows[iid]
            parent = self._rows.get(row.parent)
            if parent is not None and iid in parent.children:
                parent.children.remove(iid)
        for x in gone:
            self._rows.pop(x, None)
        if gone:
            before = list(self._sel)
            self._sel = [s for s in self._sel if s not in gone]
            if self._focus in gone:
                self._focus = None
            if self._anchor in gone:
                self._anchor = None
            self._changed()
            if before != self._sel:
                self._select_event()

    def get_children(self, item=""):
        row = self._rows.get(item or "")
        return tuple(row.children) if row is not None else ()

    def exists(self, iid):
        return iid in self._rows and iid != ""

    def parent(self, iid):
        row = self._rows.get(iid)
        return row.parent if row is not None and row.parent is not None else ""

    def index(self, iid):
        row = self._rows[iid]
        return self._rows[row.parent].children.index(iid)

    def item(self, iid, option=None, **kw):
        if iid not in self._rows:
            raise tk.TclError("Item %s not found" % iid)
        row = self._rows[iid]
        if kw:
            changed = False
            if "values" in kw:
                row.values, changed = tuple(kw["values"]), True
            if "tags" in kw:
                t = kw["tags"]
                row.tags, changed = ((t,) if isinstance(t, str) else tuple(t or ())), True
            if "text" in kw:
                row.text, changed = kw["text"], True
            if "open" in kw and bool(kw["open"]) != row.open:
                row.open, changed = bool(kw["open"]), True
            if changed:
                self._changed()
            return None
        d = {"text": row.text, "values": row.values, "tags": row.tags, "open": row.open,
             "image": ""}
        if option is not None:
            return d[option.lstrip("-")]
        return d

    def set(self, iid, column, value=None):
        row = self._rows[iid]
        k = self._columns.index(column) if column in self._columns else int(str(column)
                                                                            .lstrip("#")) - 1
        if value is None:
            return row.values[k] if k < len(row.values) else ""
        vals = list(row.values) + [""] * max(0, k + 1 - len(row.values))
        vals[k] = value
        row.values = tuple(vals)
        self._changed()

    # -- columns and headings ------------------------------------------------------------------
    def _col(self, column):
        if isinstance(column, int):
            return self._cols[column + 1]
        c = str(column)
        if c in self._cols:
            return c
        if c.startswith("#") and c[1:].isdigit():
            return self._cols[int(c[1:])]
        raise tk.TclError("Invalid column index %s" % column)

    def column(self, column, option=None, **kw):
        c = self._col(column)
        if kw:
            if "width" in kw:
                self._width[c] = max(1, int(kw["width"]))
            if "minwidth" in kw:
                self._minwidth[c] = int(kw["minwidth"])
            if "stretch" in kw:
                self._stretch[c] = bool(kw["stretch"])
            self._layout_columns()
            self._changed()
            return None
        d = {"width": self._width[c], "minwidth": self._minwidth[c],
             "stretch": self._stretch[c], "anchor": "w", "id": c}
        return d[option.lstrip("-")] if option is not None else d

    def heading(self, column, option=None, **kw):
        c = self._col(column)
        if kw:
            if "text" in kw:
                self._heading[c]["text"] = kw["text"]
            if "command" in kw:
                self._heading[c]["command"] = kw["command"]
            self._draw_header()
            return None
        d = dict(self._heading[c])
        return d[option.lstrip("-")] if option is not None else d

    def tag_configure(self, tag, **kw):
        opts = self._tags.setdefault(tag, {})
        if not kw:
            return dict(opts)
        opts.update(kw)
        conf = {}
        if "background" in kw:
            conf["background"] = kw["background"]
        if "foreground" in kw:
            conf["foreground"] = kw["foreground"]
        if "font" in kw:
            conf["font"] = kw["font"]
        if conf:
            self._text.tag_configure("t_" + tag, **conf)
            self._text.tag_raise("fl_sel")
        return None

    def cget(self, key):
        if key in ("columns", "-columns"):
            return self._columns
        if key in ("show", "-show"):
            return ("tree " if self._show_tree else "") + ("headings" if self._show_head else "")
        if key in ("selectmode", "-selectmode"):
            return self._selectmode
        return tk.Frame.cget(self, key)

    def configure(self, cnf=None, **kw):
        if "yscrollcommand" in kw:
            self._ycmd = kw.pop("yscrollcommand")
        if "xscrollcommand" in kw:
            self._xcmd = kw.pop("xscrollcommand")
        if "selectmode" in kw:
            self._selectmode = kw.pop("selectmode")
        if "height" in kw:
            self._text.configure(height=kw.pop("height"))
        if cnf or kw:
            return tk.Frame.configure(self, cnf, **kw)
        return None

    config = configure

    # -- selection ---------------------------------------------------------------------------
    def selection(self):
        return tuple(self._sel)

    def selection_set(self, *items):
        items = self._flat(items)
        self._sel = [i for i in items if i in self._rows and i != ""]
        if self._sel:
            self._focus = self._anchor = self._sel[-1]
        self._paint_selection()
        self._select_event()

    def selection_add(self, *items):
        for i in self._flat(items):
            if i in self._rows and i not in self._sel:
                self._sel.append(i)
        self._paint_selection()
        self._select_event()

    def selection_remove(self, *items):
        drop = set(self._flat(items))
        self._sel = [i for i in self._sel if i not in drop]
        self._paint_selection()
        self._select_event()

    @staticmethod
    def _flat(items):
        out = []
        for i in items:
            if isinstance(i, (list, tuple)):
                out.extend(i)
            elif i is not None and i != "":
                out.append(i)
        return out

    def focus(self, iid=None):
        if iid is None:
            return self._focus or ""
        self._focus = iid if iid in self._rows else None
        return None

    def focus_set(self):
        self._text.focus_set()

    def see(self, iid):
        self._flush()
        if iid in self._rows:
            p = self._rows[iid].parent
            opened = False
            while p:
                if not self._rows[p].open:
                    self._rows[p].open, opened = True, True
                p = self._rows[p].parent
            if opened:
                self._rebuild()
        if iid in self._line_of:
            self._text.see("%d.0" % (self._line_of[iid] + 1))

    # -- geometry --------------------------------------------------------------------------
    def identify_row(self, y):
        self._flush()
        if not self._lines:
            return ""
        idx = self._text.index("@0,%d" % int(y))
        line = int(idx.split(".")[0]) - 1
        info = self._text.dlineinfo("%d.0" % (line + 1))
        if info is None or not (info[1] <= int(y) < info[1] + info[3]):
            return ""
        return self._lines[line] if 0 <= line < len(self._lines) else ""

    def identify_column(self, x):
        xoff = self._xoffset()
        px = int(x) + xoff
        acc = 0
        for k, c in enumerate(self._shown_cols()):
            acc += self._width[c]
            if px < acc:
                return "#%d" % self._cols.index(c)
        return ""

    def identify_region(self, x, y):
        if self._show_head and y < 0:
            return "heading"
        return "cell" if self.identify_row(y) else "nothing"

    def bbox(self, iid, column=None):
        self._flush()
        if iid not in self._line_of:
            return ""
        info = self._text.dlineinfo("%d.0" % (self._line_of[iid] + 1))
        if info is None:
            return ""
        x, w = 0, sum(self._width[c] for c in self._shown_cols())
        if column is not None:
            c = self._col(column)
            x = sum(self._width[k] for k in self._shown_cols()[:self._shown_cols().index(c)])
            w = self._width[c]
        return (x - self._xoffset(), info[1], w, info[3])

    def _xoffset(self):
        # from the scroll callback: asking xview() makes the Text measure every line
        return int(round(self._xfirst * max(1, self._total_width())))

    def yview(self, *args):
        if not args:
            return self._text.yview()
        return self._text.yview(*args)

    def xview(self, *args):
        if not args:
            return self._text.xview()
        r = self._text.xview(*args)
        self._sync_header()
        return r

    def yview_scroll(self, n, what):
        self._text.yview_scroll(n, what)

    def yview_moveto(self, f):
        self._text.yview_moveto(f)

    def bind(self, sequence=None, func=None, add=None):
        return self._text.bind(sequence, func, add)

    def unbind(self, sequence, funcid=None):
        return self._text.unbind(sequence, funcid)

    def event_generate(self, sequence, **kw):
        return self._text.event_generate(sequence, **kw)

    # -- drawing ---------------------------------------------------------------------------
    def _changed(self):
        self._dirty = True
        try:
            shown = self.winfo_ismapped()
        except tk.TclError:
            return
        if not shown:
            return                      # drawn when it is shown (<Map>), or when asked
        if self._after is None:
            try:
                self._after = self.after_idle(self._rebuild)
            except tk.TclError:
                self._after = None

    def _on_map(self, _e=None):
        if self._dirty and self._after is None:
            try:
                self._after = self.after_idle(self._rebuild)
            except tk.TclError:
                self._after = None

    def _flush(self):
        if self._dirty:
            self._rebuild()

    def _shown_cols(self):
        return self._cols if self._show_tree else self._cols[1:]

    def _total_width(self):
        return sum(self._width[c] for c in self._shown_cols())

    def _layout_columns(self):
        """Stretch columns share the room the window has beyond the columns' widths."""
        try:
            avail = self._text.winfo_width()
        except tk.TclError:
            return
        if avail <= 1:
            return
        cols = self._shown_cols()
        fixed = sum(self._width[c] for c in cols)
        extra = avail - fixed
        stretch = [c for c in cols if self._stretch[c]]
        if extra != 0 and stretch:
            base = getattr(self, "_base_width", None)
            if base is None or set(base) != set(cols):
                base = self._base_width = dict(self._width)
            base_total = sum(base[c] for c in cols)
            room = avail - base_total
            share = room / float(len(stretch))
            for c in cols:
                w = base[c] + (share if c in stretch else 0)
                self._width[c] = max(self._minwidth[c], int(w))

    def _tabs(self):
        out, acc = [], 0
        for c in self._shown_cols():
            acc += self._width[c]
            out.append(acc)
        return tuple(out)

    COMMON = 0x2FFF      # characters above this get an estimated width (see _width_of)

    def _char_widths(self, font):
        cache = getattr(self, "_cw", None)
        if cache is None or cache[0] is not font:
            cache = self._cw = (font, {}, font.measure("M"))
        return cache[1]

    def _width_of(self, ch, cw):
        """A character's width: measured once for the common scripts; estimated (an 'M')
        beyond them - the first measurement of an emoji or a rare script makes Tk search
        the fallback fonts, seconds on some systems, and the width only decides the cut."""
        if ord(ch) > self.COMMON:
            w = cw[ch] = self._cw[2]
        else:
            w = cw[ch] = self._cw[0].measure(ch)
        return w

    def _measure(self, text, font):
        """Text width summed from per-character widths (each measured once)."""
        cw = self._char_widths(font)
        total = 0
        for ch in text:
            w = cw.get(ch)
            if w is None:
                w = self._width_of(ch, cw)
            total += w
        return total

    def _cut(self, text, width, font):
        if text is None or text == "":
            return ""
        text = str(text)
        if "\n" in text or "\t" in text or "\r" in text:
            text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if width <= 8:
            return ""
        if len(text) > 4000:
            text = text[:4000]
        cw = self._char_widths(font)
        total = 0
        ell = self._measure("\u2026", font)
        cut_at = None
        for i, ch in enumerate(text):
            w = cw.get(ch)
            if w is None:
                w = self._width_of(ch, cw)
            if cut_at is None and total + w + ell > width:
                cut_at = i
            total += w
            if total > width:
                return text[:cut_at or 0] + "\u2026"
        return text

    def _visible_rows(self):
        out = []

        def walk(parent, depth):
            for iid in self._rows[parent].children:
                out.append((iid, depth))
                if self._rows[iid].open and self._rows[iid].children:
                    walk(iid, depth + 1)
        walk("", 0)
        return out

    def _rebuild(self):
        self._after = None
        self._dirty = False
        t = self._text
        if not t.winfo_exists():
            return
        first = self._yfirst            # from the scroll callback: asking yview() would make
        self._layout_columns()          # the Text measure every line first
        rows = self._visible_rows()
        self._lines = [iid for iid, _d in rows]
        self._line_of = dict((iid, n) for n, iid in enumerate(self._lines))
        cols = self._shown_cols()
        tabs = self._tabs()
        font = self._font
        want = tabs + (tabs[-1] + 2,) if tabs else ()
        if want != getattr(self, "_tabs_set", None):
            t.configure(tabs=want)
            self._tabs_set = want
        t.configure(state="normal")
        t.delete("1.0", "end")
        chunks = []
        for iid, depth in rows:
            row = self._rows[iid]
            cells = []
            for k, c in enumerate(cols):
                w = self._width[c] - 10
                if c == "#0":
                    mark = ("▾" if row.open else "▸") if row.children else ""
                    cells.append(self._cut(("  " * depth) + mark, w, font))
                    continue
                vi = self._cols.index(c) - 1
                v = row.values[vi] if vi < len(row.values) else ""
                if k == (1 if self._show_tree else 0) and depth and not self._show_tree:
                    v = "    " + str(v)
                cells.append(self._cut("" if v is None else v, w, font))
            line = "\t".join(cells) + "\t\n"
            tags = tuple("t_" + g for g in row.tags)
            chunks.append((line, tags))
        if chunks:
            args = []
            for line, tags in chunks:
                args.append(line)
                args.append(tags)
            t.insert("end", *args)          # one Tk call for every line
        for tag in self._tags:
            try:
                t.tag_lower("t_" + tag, "fl_sel")
            except tk.TclError:
                pass
        t.configure(state="disabled")
        self._paint_selection()
        self._draw_header()
        if first > 0.0:
            t.yview_moveto(first)       # (at the top nothing to restore: the Text then lays
                                        # out the lines in view only, the rest in the background)

    def _paint_selection(self):
        t = self._text
        if self._dirty:
            return
        t.tag_remove("fl_sel", "1.0", "end")
        for iid in self._sel:
            n = self._line_of.get(iid)
            if n is not None:
                t.tag_add("fl_sel", "%d.0" % (n + 1), "%d.0" % (n + 2))
        t.tag_raise("fl_sel")

    def _draw_header(self):
        cv = self._header
        cv.delete("all")
        x = 0
        h = int(cv.cget("height"))
        line = K["border"]
        for c in self._shown_cols():
            w = self._width[c]
            text = self._heading[c]["text"]
            cv.create_text(x + 6, h // 2, anchor="w", text=self._cut(text, w - 10,
                                                                   self._head_font),
                           font=self._head_font, fill=K["heading"], tags=("h", "h_" + c))
            cv.create_line(x + w - 1, 3, x + w - 1, h - 3, fill=line)
            x += w
        cv.create_line(0, h - 1, max(x, cv.winfo_width()), h - 1, fill=line)
        cv.configure(scrollregion=(0, 0, max(x, 1), h))
        self._sync_header()

    def _sync_header(self):
        try:
            self._header.xview_moveto(self._xfirst)
        except tk.TclError:
            pass

    def _on_y(self, a, b):
        try:
            self._yfirst = float(a)
        except (TypeError, ValueError):
            pass
        if self._ycmd is not None:
            self._ycmd(a, b)

    def _on_x(self, a, b):
        try:
            self._xfirst = float(a)
        except (TypeError, ValueError):
            pass
        self._sync_header()
        if self._xcmd is not None:
            self._xcmd(a, b)

    def _on_configure(self, _e=None):
        w = self._text.winfo_width()
        if w != getattr(self, "_seen_w", None):
            self._seen_w = w
            self._changed()

    # -- events ---------------------------------------------------------------------------
    def _select_event(self):
        try:
            self._text.event_generate("<<TreeviewSelect>>")
        except tk.TclError:
            pass

    def _on_click(self, e, mod=None):
        self._text.focus_set()
        iid = self.identify_row(e.y)
        if not iid:
            return "break"
        col = self.identify_column(e.x)
        row = self._rows[iid]
        if col == "#0" and row.children and mod is None:
            self.item(iid, open=not row.open)
            self._rebuild()
            try:
                self._text.event_generate("<<TreeviewOpen>>" if row.open
                                          else "<<TreeviewClose>>")
            except tk.TclError:
                pass
        if mod == "shift" and self._anchor in self._line_of and self._selectmode == "extended":
            a, b = sorted((self._line_of[self._anchor], self._line_of[iid]))
            self._sel = self._lines[a:b + 1]
            self._focus = iid
            self._paint_selection()
            self._select_event()
        elif mod == "ctrl" and self._selectmode == "extended":
            if iid in self._sel:
                self._sel.remove(iid)
            else:
                self._sel.append(iid)
            self._focus = self._anchor = iid
            self._paint_selection()
            self._select_event()
        else:
            if self._sel != [iid]:
                self._sel = [iid]
                self._focus = self._anchor = iid
                self._paint_selection()
                self._select_event()
            self._focus = self._anchor = iid
        return None             # the caller's <Button-1> bindings still run

    def _on_right_press(self, e):
        self._text.focus_set()
        return None

    def _page(self):
        h = max(1, self._text.winfo_height())
        return max(1, h // max(1, self._line_h) - 1)

    def _move(self, step, _e=None, extend=False):
        self._flush()
        if not self._lines:
            return "break"
        cur = self._line_of.get(self._focus, -1 if step > 0 else len(self._lines))
        n = max(0, min(len(self._lines) - 1, cur + step))
        iid = self._lines[n]
        if extend and self._anchor in self._line_of and self._selectmode == "extended":
            a, b = sorted((self._line_of[self._anchor], n))
            self._sel = self._lines[a:b + 1]
        else:
            self._sel = [iid]
            self._anchor = iid
        self._focus = iid
        self._paint_selection()
        self._text.see("%d.0" % (n + 1))
        self._select_event()
        return "break"

    def _open_focus(self, open_):
        iid = self._focus
        if iid and iid in self._rows and self._rows[iid].children:
            if self._rows[iid].open != open_:
                self.item(iid, open=open_)
                self._rebuild()
                try:
                    self._text.event_generate("<<TreeviewOpen>>" if open_
                                              else "<<TreeviewClose>>")
                except tk.TclError:
                    pass
        return "break"

    def _on_wheel(self, e, horizontal=False):
        steps = -int(e.delta / 120) if e.delta else 0
        if steps == 0:
            steps = -1 if e.delta > 0 else 1
        if horizontal:
            self._text.xview_scroll(steps * 30, "units")
        else:
            self._text.yview_scroll(steps * 3, "units")
        return None             # the window's own input handlers (bind_all) run too

    def _select_all(self, _e=None):
        if self._selectmode == "extended":
            self._flush()
            self._sel = list(self._lines)
            self._paint_selection()
            self._select_event()
        return "break"

    def _on_head_click(self, e):
        x = e.x + self._xoffset()
        acc = 0
        for c in self._shown_cols():
            acc += self._width[c]
            if x < acc:
                cmd = self._heading[c].get("command")
                if cmd:
                    cmd()
                return
