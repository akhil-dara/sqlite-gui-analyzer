"""Filter one column of a query by what it holds - no condition words to learn.

The panel reads the column first (engine.profile) and shows it the way it is:
  - a date column: a bar per month of the data (click a bar for that month, drag across bars
    for a range; From / To calendars for exact times); how the dates are stored is found by
    the tool and only named in a detail line, which can change it;
  - a column of a few values (type, state...): the values with how many rows have each, ticked
    to keep;
  - numbers: the smallest and largest, and From / To;
  - other text: 'contains', and the most frequent values to pick from.
As the choice changes the panel says how many rows it keeps. Apply makes it the column's
condition of the query (one per column, replaced when the panel is used again); More
options... opens the full condition dialog.
"""

import tkinter as tk
from tkinter import ttk

from constants import C
from engine import querymodel as qm
from engine import timeline as tl
from engine.profile import month_range
from tokens import COLOR as K, FONT as F
from widgets import FlowFrame, SearchBox, wrap_to_width

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_label(ym):
    return "%s %s" % (MONTHS[int(ym[5:7]) - 1], ym[:4])


def all_months(first, last):
    """Every 'YYYY-MM' from first to last (months without rows included)."""
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while "%04d-%02d" % (y, m) <= last and len(out) < 2400:
        out.append("%04d-%02d" % (y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class MonthBars(tk.Canvas):
    """Rows per month as bars: click a bar, or drag across bars, to choose months;
    on_pick(first 'YYYY-MM', last 'YYYY-MM' or None)."""

    def __init__(self, master, months, on_pick, height=190):
        tk.Canvas.__init__(self, master, height=height, bg=K["card"], highlightthickness=0)
        counts = dict(months)
        self.months = all_months(months[0][0], months[-1][0]) if months else []
        self.counts = [counts.get(m, 0) for m in self.months]
        self.on_pick = on_pick
        self.sel = None                 # (i, j)
        self._down = None
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self.delete("tip"))

    def _geom(self):
        w = max(200, self.winfo_width())
        n = max(1, len(self.months))
        return 8, 8, (w - 16) / n, int(self["height"]) - 34

    def draw(self):
        self.delete("all")
        if not self.months:
            return
        x0, top, bw, h = self._geom()
        peak = max(self.counts) or 1
        for i, (m, n) in enumerate(zip(self.months, self.counts)):
            x = x0 + i * bw
            bh = 0 if not n else max(2, h * n / peak)
            on = self.sel is not None and self.sel[0] <= i <= self.sel[1]
            self.create_rectangle(x + 1, top + h - bh, x + max(2, bw - 1), top + h,
                                  fill=K["accent"] if on else K["secondary"] if n else
                                  K["muted"], outline="", tags=("bar",))
            if len(self.months) <= 36 and bw >= 9:      # a short range: every month named
                self.create_text(x + bw / 2, top + h + 3, anchor="n",
                                 text=MONTHS[int(m[5:7]) - 1][0], font=F["tiny"],
                                 fill=K["muted_text"])
            if m[5:7] == "01" or i == 0:
                self.create_text(x + 1, top + h + 14, anchor="nw", text=m[:4],
                                 font=F["tiny_bold"], fill=K["muted_text"])
        self.create_line(x0, top + h, x0 + len(self.months) * bw, top + h, fill=K["border"])

    def _index(self, x):
        x0, _t, bw, _h = self._geom()
        return max(0, min(len(self.months) - 1, int((x - x0) // bw)))

    def _press(self, e):
        if self.months:
            self._down = self._index(e.x)
            self.sel = (self._down, self._down)
            self.draw()

    def _drag(self, e):
        if self._down is not None:
            i = self._index(e.x)
            self.sel = (min(i, self._down), max(i, self._down))
            self.draw()

    def _release(self, e):
        if self._down is not None and self.sel is not None:
            self._down = None
            self.on_pick(self.months[self.sel[0]], self.months[self.sel[1]])

    def _hover(self, e):
        self.delete("tip")
        if not self.months:
            return
        i = self._index(e.x)
        text = "%s: %s rows" % (month_label(self.months[i]), format(self.counts[i], ","))
        t = self.create_text(min(e.x + 10, self.winfo_width() - 140), 4, anchor="nw",
                             text=text, font=F["small"], fill=K["tip_text"], tags=("tip",))
        x0, y0, x1, y1 = self.bbox(t)
        r = self.create_rectangle(x0 - 4, y0 - 2, x1 + 4, y1 + 2, fill=K["tip"],
                                  outline=K["tip"], tags=("tip",))
        self.tag_raise(t, r)

    def select(self, i, j):
        self.sel = (i, j) if i is not None else None
        self.draw()


class FilterPanel(tk.Toplevel):
    """column: (alias, column, label shown); kind: the date kind found (or None); load(kind,
    done) profiles the column on the caller's worker; count(Filter or None, done) -> (kept,
    total); on_apply(Filter / Group or None to remove); more(): the full condition dialog;
    current: the column's condition now (to start from)."""

    def __init__(self, master, column, kind, load, count, on_apply, more=None, current=None):
        tk.Toplevel.__init__(self, master)
        self.alias, self.column, self.label = column
        self.title("Filter: %s" % self.label)
        self.configure(bg=C["bg"])
        self.transient(master.winfo_toplevel())
        self.geometry("620x520")
        self.minsize(520, 420)
        self.kind, self.load_fn, self.count_fn = kind, load, count
        self.on_apply, self.more_fn, self.current = on_apply, more, current
        self.profile = None
        self._job = None
        head = ttk.Frame(self)
        head.pack(fill="x", padx=14, pady=(12, 0))
        ttk.Label(head, text=self.label, style="Heading.TLabel").pack(anchor="w")
        self.info = wrap_to_width(ttk.Label(head, text="Reading the column…", style="M.TLabel"))
        self.info.pack(fill="x")
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=14, pady=8)
        self.result = wrap_to_width(ttk.Label(self, text="", style="M.TLabel"))
        self.result.pack(fill="x", padx=14)
        bar = FlowFrame(self)
        bar.pack(fill="x", padx=14, pady=10)
        self.apply_btn = bar.add(ttk.Button(bar, text="Apply", style="P.TButton",
                                            command=self.apply))
        if current is not None:
            bar.add(ttk.Button(bar, text="Remove this filter", command=self.remove), gap=6)
        if more is not None:
            bar.add(ttk.Button(bar, text="More options…", command=self._more), gap=6)
        bar.add(ttk.Button(bar, text="Cancel", command=self.destroy), gap=12)
        self.bind("<Escape>", lambda e: self.destroy())
        self._read()

    # -- reading the column ---------------------------------------------------------------------
    def _read(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.info.configure(text="Reading the column…")
        self.load_fn(self.kind, self._loaded)

    def _loaded(self, p, error):
        if not self.winfo_exists():
            return
        if error is not None:
            self.info.configure(text="The column could not be read: %s" % error)
            return
        self.profile = p
        looks = p.looks()
        none = p.nulls + p.empties
        base = "%s rows%s" % (format(p.total, ","), " · %s with no value" % format(none, ",")
                              if none else "")
        if looks == "date" and p.months:
            self._date_view(base)
        elif looks == "list":
            self.info.configure(text="%s · %d different values" % (base, p.distinct))
            self._list_view()
        elif looks == "number":
            self.info.configure(text="%s · numbers from %s to %s" % (base, _n(p.lo), _n(p.hi)))
            self._number_view()
        else:
            self.info.configure(text="%s · %s different values" % (
                base, "more than %d" % len(p.top) if p.top_cut else p.distinct))
            self._text_view()
        if (looks == "date" and p.months) or looks == "number":
            self._fit_height()          # no list to scroll: no empty block under the controls
        self._changed()

    def _fit_height(self):
        """Shrink the window to what the view asks for (the list views keep their room)."""
        self.update_idletasks()
        need = self.winfo_reqheight()
        if 0 < need < self.winfo_height():
            self.minsize(520, min(need, 420))
            self.geometry("%dx%d" % (self.winfo_width(), need))

    # -- dates ----------------------------------------------------------------------------------
    def _date_view(self, base):
        p = self.profile
        from datepicker import DateTimePicker
        self.info.configure(text="%s · dates from %s to %s (UTC)" % (
            base, p.first.strftime("%d %b %Y") if p.first else "?",
            p.last.strftime("%d %b %Y") if p.last else "?"))
        ttk.Label(self.body, text="Rows per month - click a month, or drag across months:",
                  style="M.TLabel").pack(anchor="w")
        self.bars = MonthBars(self.body, p.months, self._months_picked)
        self.bars.pack(fill="x", pady=(2, 6))
        row = ttk.Frame(self.body)
        row.pack(fill="x")
        ttk.Label(row, text="From").grid(row=0, column=0, sticky="w")
        self.d_from = DateTimePicker(row, width=22, on_change=lambda dt: self._dates_typed())
        self.d_from.grid(row=0, column=1, sticky="w", padx=(6, 14))
        ttk.Label(row, text="To").grid(row=0, column=2, sticky="w")
        self.d_to = DateTimePicker(row, end=True, width=22,
                                   on_change=lambda dt: self._dates_typed())
        self.d_to.grid(row=0, column=3, sticky="w", padx=(6, 0))
        quick = FlowFrame(self.body)
        quick.pack(fill="x", pady=(6, 0))
        last = p.months[-1][0]
        quick.add(ttk.Button(quick, text="Latest month (%s)" % month_label(last),
                             style="Small.TButton",
                             command=lambda: self._months_picked(last, last, mark=True)))
        quick.add(ttk.Button(quick, text="Whole range", style="Small.TButton",
                             command=lambda: self._months_picked(p.months[0][0], last,
                                                                 mark=True)), gap=4)
        kind_row = ttk.Frame(self.body)
        kind_row.pack(fill="x", pady=(10, 0))
        self.kind_note = ttk.Label(kind_row, style="M.TLabel",
                                   text="The tool reads these values as %s%s." % (
                                       tl.LABELS.get(p.kind, p.kind), ", %s rows are not a "
                                       "date of that kind (0, -1 or other)" % format(
                                           p.not_dates, ",") if p.not_dates else ""))
        self.kind_note.pack(side="left")
        ttk.Button(kind_row, text="Read them differently ▾", style="Small.TButton",
                   command=self._kind_menu).pack(side="left", padx=(8, 0))
        cur = self.current
        if isinstance(cur, qm.Filter) and cur.op == "between" and cur.date == p.kind:
            self.d_from.set_utc(tl.parse_when(cur.value))
            self.d_to.set_utc(tl.parse_when(cur.value2, end=True))

    def _months_picked(self, a, b, mark=False):
        start, end = month_range(a, b)
        self.d_from.set_utc(start)
        self.d_to.set_utc(end)
        if mark:
            ms = self.bars.months
            self.bars.select(ms.index(a), ms.index(b))
        self._changed()

    def _dates_typed(self):
        self.bars.select(None, None)
        self._changed()

    def _kind_menu(self):
        m = tk.Menu(self, tearoff=0)
        for k in tl.KINDS:
            if k in tl.LABELS:
                m.add_command(label=("✓ " if k == self.kind else "   ") + tl.LABELS[k],
                              command=lambda k=k: self._set_kind(k))
        m.add_separator()
        m.add_command(label="Not a date (show its values)", command=lambda: self._set_kind(None))
        m.tk_popup(self.winfo_pointerx(), self.winfo_pointery())

    def _set_kind(self, k):
        self.kind = k
        self._read()

    # -- a few values ---------------------------------------------------------------------------
    def _list_view(self):
        p = self.profile
        if len(p.top) > 12:
            self.find = SearchBox(self.body, placeholder="Find a value…", delay=0,
                                  find_button=False, on_change=lambda t: self._fill_list())
            self.find.pack(fill="x", pady=(0, 4))
        else:
            self.find = None
        box = ttk.Frame(self.body)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("rows", "share"), show="tree headings",
                                 selectmode="none")
        self.tree.heading("#0", text="Keep the rows whose value is ticked")
        self.tree.heading("rows", text="Rows")
        self.tree.heading("share", text="")
        self.tree.column("rows", width=80, stretch=False, anchor="e")
        self.tree.column("share", width=140, stretch=False)
        ys = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._flip)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.focus()))
        quick = FlowFrame(self.body)
        quick.pack(fill="x", pady=(4, 0))
        quick.add(ttk.Button(quick, text="Tick all", style="Small.TButton",
                             command=lambda: self._tick_all(True)))
        quick.add(ttk.Button(quick, text="Untick all", style="Small.TButton",
                             command=lambda: self._tick_all(False)), gap=4)
        self.keep = set()
        cur = self.current
        items = []
        if isinstance(cur, qm.Group):
            items = [x for x in cur.items if x.op in ("in", "is null")]
        elif isinstance(cur, qm.Filter):
            items = [cur]
        for f in items:
            if f.op == "in":
                self.keep.update(qm.split_list(f.value))
            elif f.op == "is null":
                self.keep.add(None)
        self._fill_list()

    def _key(self, v):
        return None if v is None else str(v)

    def _fill_list(self):
        p = self.profile
        t = self.tree
        t.delete(*t.get_children())
        term = self.find.var.get().strip().lower() if self.find is not None and hasattr(
            self.find, "var") else ""
        peak = max((n for _v, n in p.top), default=1)
        for i, (v, n) in enumerate(p.top):
            text = "(no value)" if v is None else ("(empty text)" if v == "" else str(v))
            if term and term not in text.lower():
                continue
            on = self._key(v) in self.keep
            t.insert("", "end", iid=str(i), text=("☑  " if on else "☐  ") + text[:120],
                     values=(format(n, ","), "█" * max(1, int(12 * n / peak))))

    def _flip(self, e):
        self._toggle(self.tree.identify_row(e.y))

    def _toggle(self, iid):
        if not iid:
            return
        k = self._key(self.profile.top[int(iid)][0])
        self.keep.symmetric_difference_update((k,))
        self._fill_list()
        self._changed()

    def _tick_all(self, on):
        if on:
            self.keep = set(self._key(v) for v, _n in self.profile.top)
        else:
            self.keep = set()
        self._fill_list()
        self._changed()

    # -- numbers and text -----------------------------------------------------------------------
    def _number_view(self):
        p = self.profile
        row = ttk.Frame(self.body)
        row.pack(fill="x")
        ttk.Label(row, text="From").grid(row=0, column=0, sticky="w")
        self.n_from = ttk.Entry(row, width=16)
        self.n_from.grid(row=0, column=1, padx=(6, 14), sticky="w")
        ttk.Label(row, text="to").grid(row=0, column=2, sticky="w")
        self.n_to = ttk.Entry(row, width=16)
        self.n_to.grid(row=0, column=3, padx=(6, 0), sticky="w")
        for e in (self.n_from, self.n_to):
            e.bind("<KeyRelease>", lambda ev: self._changed())
        ttk.Label(self.body, style="M.TLabel", text="Leave one empty for 'at least' or 'at "
                  "most'. Smallest %s, largest %s." % (_n(p.lo), _n(p.hi))).pack(
            anchor="w", pady=(6, 0))
        cur = self.current
        if isinstance(cur, qm.Filter) and cur.op in ("between", ">=", "<="):
            if cur.op in ("between", ">="):
                self.n_from.insert(0, str(cur.value))
            if cur.op == "between":
                self.n_to.insert(0, str(cur.value2))
            elif cur.op == "<=":
                self.n_to.insert(0, str(cur.value))

    def _text_view(self):
        p = self.profile
        row = ttk.Frame(self.body)
        row.pack(fill="x")
        ttk.Label(row, text="Keep the rows whose value contains").pack(side="left")
        self.contains = ttk.Entry(row, width=30)
        self.contains.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.contains.bind("<KeyRelease>", lambda e: self._changed())
        if isinstance(self.current, qm.Filter) and self.current.op == "contains":
            self.contains.insert(0, self.current.value)
        ttk.Label(self.body, text="Most frequent values (click one to use it):",
                  style="M.TLabel").pack(anchor="w", pady=(8, 2))
        box = ttk.Frame(self.body)
        box.pack(fill="both", expand=True)
        t = ttk.Treeview(box, columns=("rows",), show="tree headings", selectmode="browse")
        t.heading("#0", text="Value")
        t.heading("rows", text="Rows")
        t.column("rows", width=80, stretch=False, anchor="e")
        ys = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        for i, (v, n) in enumerate(p.top[:200]):
            t.insert("", "end", iid=str(i), text="(no value)" if v is None else str(v)[:120],
                     values=(format(n, ","),))

        def use(_e=None):
            sel = t.selection()
            if sel:
                v = p.top[int(sel[0])][0]
                self.contains.delete(0, "end")
                self.contains.insert(0, "" if v is None else str(v))
                self._changed()
        t.bind("<<TreeviewSelect>>", use)

    # -- the condition --------------------------------------------------------------------------
    def build(self):
        """The condition chosen (Filter or Group), None for 'no filter'; ValueError when the
        typed values do not read."""
        p = self.profile
        if p is None:
            return None
        a, c = self.alias, self.column
        looks = p.looks()
        if looks == "date" and p.months:
            lo, hi = self.d_from.get_utc(), self.d_to.get_utc()
            if lo is None and hi is None:
                return None
            if lo is not None and hi is not None:
                return qm.Filter(a, c, "between", lo.strftime("%Y-%m-%d %H:%M:%S"),
                                 hi.strftime("%Y-%m-%d %H:%M:%S"), date=p.kind)
            if lo is not None:
                return qm.Filter(a, c, ">=", lo.strftime("%Y-%m-%d %H:%M:%S"), date=p.kind)
            return qm.Filter(a, c, "<=", hi.strftime("%Y-%m-%d %H:%M:%S"), date=p.kind)
        if looks == "list":
            all_keys = set(self._key(v) for v, _n in p.top)
            if not self.keep or self.keep == all_keys:
                return None if not self.keep else None
            vals = [k for k in self.keep if k is not None]
            parts = []
            if vals:
                parts.append(qm.Filter(a, c, "in", "\n".join(sorted(vals))))
            if None in self.keep:
                parts.append(qm.Filter(a, c, "is null"))
            return parts[0] if len(parts) == 1 else qm.Group("any", parts)
        if looks == "number":
            lo, hi = self.n_from.get().strip(), self.n_to.get().strip()
            for v in (lo, hi):
                if v:
                    try:
                        float(v.replace(",", ""))
                    except ValueError:
                        raise ValueError("'%s' is not a number" % v)
            lo, hi = lo.replace(",", ""), hi.replace(",", "")
            if lo and hi:
                return qm.Filter(a, c, "between", lo, hi)
            if lo:
                return qm.Filter(a, c, ">=", lo)
            if hi:
                return qm.Filter(a, c, "<=", hi)
            return None
        text = self.contains.get()
        return qm.Filter(a, c, "contains", text) if text else None

    def _changed(self):
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(300, self._count)

    def _count(self):
        self._job = None
        try:
            f = self.build()
        except ValueError as e:
            self.result.configure(text=str(e), foreground=K["danger_text"])
            return
        if f is None:
            self.result.configure(text="No filter on this column: every row is kept.",
                                  foreground=K["muted_text"])
            return
        self.result.configure(text="Counting…", foreground=K["muted_text"])

        def done(result, error):
            if not self.winfo_exists():
                return
            if error is not None:
                self.result.configure(text="%s." % f.text(), foreground=K["muted_text"])
                return
            n, total = result
            self.result.configure(text="Keeps %s of %s rows." % (format(n, ","),
                                                                 format(total, ",")),
                                  foreground=K["primary"] if n else K["danger_text"])
        self.count_fn(f, done)

    def apply(self):
        try:
            f = self.build()
        except ValueError as e:
            self.result.configure(text=str(e), foreground=K["danger_text"])
            return
        self.on_apply(f)
        self.destroy()

    def remove(self):
        self.on_apply(None)
        self.destroy()

    def _more(self):
        self.destroy()
        self.more_fn()


def _n(v):
    if v is None:
        return "?"
    if isinstance(v, float) and not v.is_integer():
        return format(v, ",.2f")
    return format(int(v), ",")
