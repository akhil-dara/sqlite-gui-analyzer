"""The condition dialog of the Query tab: a column, a condition in plain words, and the value
area that condition needs - one value, two ('between ... and ...'), a list ('is one of'), or
none ('has no value'). 'Pick values…' lists the column's real values with how many rows have
each (a checklist, searchable); a value can instead be another column; capitals can be told
not to count; values can be dates (a calendar), compared as the number the column stores. As
the condition changes, the dialog says how many rows it matches (or why it cannot run).
"""

import tkinter as tk
from tkinter import ttk

from combobox import SearchableCombobox
from constants import C
from engine import querymodel as qm
from engine import timeline as tl
from tokens import COLOR as K
from widgets import FlowFrame, SearchBox, wrap_to_width

DATE_KINDS = [k for k in tl.KINDS if k in tl.LABELS]


class ConditionDialog(tk.Toplevel):
    """columns: [(key 'alias.column' or a summary heading, label, summary?, date kind or
    None)]; values(key, done) and count(filter, done) run on the caller's worker and call
    done(result, error); on_ok(Filter); start: a Filter to edit, or (key) to preselect."""

    def __init__(self, master, columns, on_ok, values=None, count=None, start=None,
                 title="Add a condition"):
        tk.Toplevel.__init__(self, master)
        self.title(title)
        self.configure(bg=C["bg"])
        self.transient(master.winfo_toplevel())
        self.minsize(560, 360)
        self.columns, self.on_ok, self.values_fn, self.count_fn = columns, on_ok, values, count
        self._by_label = dict((c[1], c) for c in columns)
        self._count_job = None
        f = self.frm = ttk.Frame(self)
        f.pack(fill="both", expand=True, padx=14, pady=12)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Column").grid(row=0, column=0, sticky="w", pady=3)
        self.col = SearchableCombobox(f, values=[c[1] for c in columns], width=44)
        self.col.grid(row=0, column=1, sticky="we", pady=3)
        ttk.Label(f, text="Condition").grid(row=1, column=0, sticky="w", pady=3)
        self.op = SearchableCombobox(f, values=[label for _k, label, _n in qm.CONDITIONS],
                                     state="readonly", width=44)
        self.op.grid(row=1, column=1, sticky="we", pady=3)
        ttk.Label(f, text="Value").grid(row=2, column=0, sticky="nw", pady=(6, 3))
        self.valbox = ttk.Frame(f)
        self.valbox.grid(row=2, column=1, sticky="we", pady=3)

        opts = ttk.Frame(f)
        opts.grid(row=3, column=1, sticky="w", pady=(6, 0))
        self.other_var = tk.BooleanVar(value=False)
        self.nocase_var = tk.BooleanVar(value=False)
        self.date_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Compare with another column", variable=self.other_var,
                        command=self._layout).pack(anchor="w")
        self.nocase_chk = ttk.Checkbutton(opts, text="Capitals do not count (abc = ABC)",
                                          variable=self.nocase_var, command=self._changed)
        self.nocase_chk.pack(anchor="w")
        drow = ttk.Frame(opts)
        drow.pack(anchor="w")
        ttk.Checkbutton(drow, text="The values are dates (UTC), stored as", variable=self.date_var,
                        command=self._layout).pack(side="left")
        self.kind = SearchableCombobox(drow, values=[tl.LABELS[k] for k in DATE_KINDS],
                                       state="readonly", width=30)
        self.kind.pack(side="left", padx=(6, 0))
        self.kind.bind("<<ComboboxSelected>>", lambda e: self._changed())

        self.result = wrap_to_width(ttk.Label(f, text="", style="M.TLabel"))
        self.result.grid(row=4, column=0, columnspan=2, sticky="we", pady=(10, 0))
        bar = FlowFrame(self)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        self.ok_btn = bar.add(ttk.Button(bar, text="Save the condition" if isinstance(
            start, qm.Filter) else "Add the condition", style="P.TButton", command=self.ok))
        bar.add(ttk.Button(bar, text="Cancel", command=self.destroy), gap=8)
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.ok() if self.focus_get() is not getattr(
            self, "list_text", None) else None)

        self.col.bind("<<ComboboxSelected>>", lambda e: self._column_changed())
        self.op.bind("<<ComboboxSelected>>", lambda e: self._layout())
        self._entries = []
        self._fill_start(start)

    # -- setting up ---------------------------------------------------------------------------
    def _fill_start(self, start):
        if isinstance(start, qm.Filter):
            label = next((c[1] for c in self.columns if c[0] == (
                start.summary or "%s.%s" % (start.alias, start.column))), self.columns[0][1])
            self.col.set(label)
            self.op.set(qm.OP_LABEL[start.op])
            self.other_var.set(bool(start.other))
            self.nocase_var.set(start.nocase)
            self.date_var.set(bool(start.date))
            if start.date:
                self.kind.set(tl.LABELS.get(start.date, ""))
            self._layout()
            self._put_values(start)
        else:
            label = next((c[1] for c in self.columns if c[0] == start), self.columns[0][1])
            self.col.set(label)
            self.op.set(qm.OP_LABEL["="])
            self._column_changed()

    def _column(self):
        return self._by_label.get(self.col.get())

    def _opkey(self):
        label = self.op.get()
        return next((k for k, lab, _n in qm.CONDITIONS if lab == label), "=")

    def _column_changed(self):
        c = self._column()
        if c is not None and c[3]:
            self.date_var.set(True)
            self.kind.set(tl.LABELS.get(c[3], ""))
            if self._opkey() == "=":
                self.op.set(qm.OP_LABEL["between"])
        else:
            self.date_var.set(False)
        self._layout()

    def _layout(self):
        for w in self.valbox.winfo_children():
            w.destroy()
        self._entries = []
        self.list_text = None
        op = self._opkey()
        n = qm.OP_VALUES[op]
        # contains / starts / ends with run as LIKE, which never counts capitals of A-Z (other
        # letters always count): the option does nothing there, so it is not offered
        like = op in ("contains", "not contains", "starts with", "ends with")
        self.nocase_chk.state(["!disabled"] if op not in ("glob", "is null", "is not null")
                              and not like else ["disabled"])
        self.nocase_chk.configure(text="Capitals of A-Z never count for this condition "
                                       "(other letters do)" if like else
                                  "Capitals do not count (abc = ABC)")
        if n == 0:
            ttk.Label(self.valbox, text="(no value needed)", style="M.TLabel").pack(anchor="w")
        elif self.other_var.get():
            others = [c[1] for c in self.columns if not c[2] and c[1] != self.col.get()]
            box = SearchableCombobox(self.valbox, values=others, width=44)
            box.pack(fill="x")
            if others:
                box.set(others[0])
            box.bind("<<ComboboxSelected>>", lambda e: self._changed())
            self._entries.append(box)
        elif n == "list":
            self.list_text = tk.Text(self.valbox, height=5, width=46, bg=K["card"],
                                     fg=K["text"], insertbackground=K["text"], wrap="word")
            self.list_text.pack(fill="x")
            self.list_text.bind("<KeyRelease>", lambda e: self._changed())
            row = ttk.Frame(self.valbox)
            row.pack(fill="x", pady=(3, 0))
            ttk.Label(row, text="one per line (or separated by commas)",
                      style="M.TLabel").pack(side="left")
            if self.values_fn is not None:
                ttk.Button(row, text="Pick values…", style="Small.TButton",
                           command=self._pick_values).pack(side="right")
        else:
            for i in range(n):
                if i:
                    ttk.Label(self.valbox, text="and", style="M.TLabel").pack(anchor="w")
                if self.date_var.get():
                    from datepicker import DateTimePicker
                    e = DateTimePicker(self.valbox, end=bool(i), width=24,
                                       on_change=lambda dt: self._changed(),
                                       on_edit=self._changed)
                else:
                    e = ttk.Entry(self.valbox, width=46)
                    e.bind("<KeyRelease>", lambda ev: self._changed())
                e.pack(fill="x")
                self._entries.append(e)
            if self.values_fn is not None and not self.date_var.get() and n == 1:
                ttk.Button(self.valbox, text="Pick a value…", style="Small.TButton",
                           command=self._pick_values).pack(anchor="e", pady=(3, 0))
        self._changed()

    def _put_values(self, f):
        if self.list_text is not None:
            self.list_text.insert("1.0", "\n".join(qm.split_list(f.value)))
        elif self.other_var.get() and self._entries:
            other = next((c[1] for c in self.columns if c[0] == f.other), f.other)
            self._entries[0].set(other)
        else:
            for e, v in zip(self._entries, (f.value, f.value2)):
                if hasattr(e, "set_utc"):
                    e.set_utc(tl.parse_when(str(v)) if v else None)
                else:
                    e.insert(0, str(v))
        self._changed()

    # -- the values a column has --------------------------------------------------------------
    def _pick_values(self):
        c = self._column()
        if c is None or self.values_fn is None:
            return
        w = ValuePicker(self, c[1], multiple=self.list_text is not None)
        self.values_fn(c[0], w.loaded)
        self.wait_window(w)
        if w.chosen is None:
            return
        if self.list_text is not None:
            have = qm.split_list(self.list_text.get("1.0", "end"))
            self.list_text.delete("1.0", "end")
            self.list_text.insert("1.0", "\n".join(have + [v for v in w.chosen
                                                            if v not in have]))
        elif self._entries:
            self._entries[0].delete(0, "end")
            self._entries[0].insert(0, w.chosen[0] if w.chosen else "")
        self._changed()

    # -- the condition as it stands ---------------------------------------------------------------
    def build(self):
        """The Filter the fields describe (ValueError with a plain reason when incomplete)."""
        c = self._column()
        if c is None:
            raise ValueError("Choose a column from the list.")
        op = self._opkey()
        key, summary = c[0], c[1] if c[2] else None
        alias, _dot, column = key.partition(".") if not c[2] else (None, None, None)
        date = None
        if self.date_var.get():
            label = self.kind.get()
            date = next((k for k in DATE_KINDS if tl.LABELS[k] == label), None)
            if date is None:
                raise ValueError("Choose how the dates are stored.")
        kw = dict(nocase=self.nocase_var.get(), date=date, summary=key if c[2] else None)
        n = qm.OP_VALUES[op]
        if n == 0:
            f = qm.Filter(alias, column, op, **kw)
        elif self.other_var.get():
            other = self._by_label.get(self._entries[0].get()) if self._entries else None
            if other is None:
                raise ValueError("Choose the other column from the list.")
            f = qm.Filter(alias, column, op, other=other[0], **kw)
        elif n == "list":
            items = qm.split_list(self.list_text.get("1.0", "end"))
            if not items:
                raise ValueError("Give at least one value (or Pick values…).")
            f = qm.Filter(alias, column, op, "\n".join(items), **kw)
        else:
            vals = []
            for e in self._entries:
                if hasattr(e, "get_utc"):
                    dt = e.get_utc()
                    if dt is None:
                        raise ValueError("Give the date%s." % (" range" if n == 2 else ""))
                    vals.append(dt.strftime("%Y-%m-%d %H:%M:%S"))
                else:
                    vals.append(e.get())
            if any(v == "" for v in vals) and op not in ("=", "!="):
                raise ValueError("Give the value%s." % ("s" if n == 2 else ""))
            f = qm.Filter(alias, column, op, vals[0], vals[1] if n == 2 else "", **kw)
        f.sql()                         # a date that does not read, an empty list...
        if summary:
            f.alias, f.column = None, None
        return f

    def _changed(self, *_a):
        if self._count_job is not None:
            self.after_cancel(self._count_job)
        self._count_job = self.after(350, self._count)

    def _count(self):
        self._count_job = None
        try:
            f = self.build()
        except ValueError as e:
            self.result.configure(text=str(e), foreground=K["danger_text"])
            return
        self.result.configure(text="%s.  Counting the rows it matches…" % f.text(),
                              foreground=K["muted_text"])
        if self.count_fn is None:
            return

        def done(result, error):
            if not self.winfo_exists():
                return
            if error is not None:
                self.result.configure(text="%s.  SQLite says: %s" % (f.text(), error),
                                      foreground=K["danger_text"])
                return
            n, total = result
            self.result.configure(text="%s.  Matches %s of %s rows." % (
                f.text(), format(n, ","), format(total, ",")), foreground=K["muted_text"])
        self.count_fn(f, done)

    def ok(self):
        try:
            f = self.build()
        except ValueError as e:
            self.result.configure(text=str(e), foreground=K["danger_text"])
            return
        self.on_ok(f)
        self.destroy()


class ValuePicker(tk.Toplevel):
    """The values a column has, with how many rows have each: tick one (or several)."""

    def __init__(self, master, title, multiple=True):
        tk.Toplevel.__init__(self, master)
        self.title("Values of %s" % title)
        self.configure(bg=C["bg"])
        self.transient(master.winfo_toplevel())
        self.geometry("520x480")
        self.multiple, self.chosen, self.ticked, self.rows = multiple, None, [], []
        self.head = wrap_to_width(ttk.Label(self, text="Reading the values…",
                                            style="M.TLabel"))
        self.head.pack(fill="x", padx=10, pady=(10, 4))
        self.find = SearchBox(self, placeholder="Find a value…", delay=0, find_button=False,
                              on_change=lambda t: self.fill())
        self.find.pack(fill="x", padx=10)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        self.tree = ttk.Treeview(box, columns=("rows",), show="tree headings",
                                 selectmode="none")
        self.tree.heading("#0", text="Value")
        self.tree.heading("rows", text="Rows")
        self.tree.column("rows", width=90, stretch=False, anchor="e")
        ys = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._flip)
        bar = FlowFrame(self)
        bar.pack(fill="x", padx=10, pady=8)
        bar.add(ttk.Button(bar, text="Use the ticked value%s" % ("s" if multiple else ""),
                           style="P.TButton", command=self.ok))
        bar.add(ttk.Button(bar, text="Cancel", command=self.destroy), gap=8)

    def loaded(self, result, error):
        if not self.winfo_exists():
            return
        if error is not None:
            self.head.configure(text="The values could not be read: %s" % error)
            return
        rows, cut = result
        self.rows = rows
        self.head.configure(text="%s different values%s; most frequent first." % (
            format(len(rows), ","), " (the first %s - value_list_values)" % format(len(rows), ",")
            if cut else ""))
        self.fill()

    def fill(self):
        t = self.tree
        t.delete(*t.get_children())
        term = self.find.var.get().strip().lower() if hasattr(self.find, "var") else ""
        for i, (v, n) in enumerate(self.rows):
            text = "NULL" if v is None else str(v)
            if term and term not in text.lower():
                continue
            t.insert("", "end", iid=str(i), text=("☑  " if text in self.ticked else "☐  ") +
                     text[:200], values=(format(n, ","),))

    def _flip(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        v = self.rows[int(iid)][0]
        text = "NULL" if v is None else str(v)
        if not self.multiple:
            self.ticked = [text]
        elif text in self.ticked:
            self.ticked.remove(text)
        else:
            self.ticked.append(text)
        self.fill()

    def ok(self):
        self.chosen = list(self.ticked)
        self.destroy()
