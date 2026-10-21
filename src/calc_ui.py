"""The calculator: a new column made from a column's values by steps (engine.calc), with a
live preview on real values. Operations are listed by group with plain names and can be found
by typing (÷, paise, concat, phone...); a double-click or Enter adds one as the next step;
the selected step's settings are edited below the steps; every change redraws the preview:
the stored value, the result, and why a result is empty. The stored column never changes.
"""

import tkinter as tk
from tkinter import ttk

from combobox import SearchableCombobox
from constants import C
from engine import calc
from tokens import COLOR as K, FONT as F
from widgets import FlowFrame, SearchBox, wrap_to_width


def _shown(v):
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "[BLOB %d bytes]" % len(v)
    s = str(v)
    return s if len(s) <= 60 else s[:59] + "…"


class CalcDialog(tk.Toplevel):
    """source: the column's heading; samples: [(value, {heading: value})] from real rows;
    others: headings of the other columns (for 'with another column'); on_ok(steps, name)."""

    def __init__(self, master, source, samples, others, on_ok, steps=None, name=None):
        tk.Toplevel.__init__(self, master)
        self.title("Calculate from %s" % source)
        self.configure(bg=C["bg"])
        self.transient(master.winfo_toplevel())
        self.geometry("1180x600")
        self.minsize(940, 480)
        self.source, self.samples, self.others, self.on_ok = source, samples, others, on_ok
        self.steps = [list(s) for s in (steps or [])]
        self._editors = []

        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(top, text="New column", style="Heading.TLabel").pack(side="left")
        self.name_var = tk.StringVar(value=name or "")
        self.name_entry = ttk.Entry(top, textvariable=self.name_var, width=36)
        self.name_entry.pack(side="left", padx=(8, 12))
        wrap_to_width(ttk.Label(top, style="M.TLabel", text="made from %s - the stored column "
                                "is not changed; copies and exports carry the result as its "
                                "own column." % source)).pack(side="left", fill="x",
                                                              expand=True)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12, pady=4)

        # 1. the operations
        left = ttk.Frame(body)
        body.add(left, weight=1)
        ttk.Label(left, text="1  Pick an operation", style="Heading.TLabel").pack(anchor="w")
        self.find = SearchBox(left, placeholder="Find (÷, round, text, phone…)", delay=0,
                              find_button=False, on_change=lambda t: self._fill_ops())
        self.find.pack(fill="x", pady=(2, 2))
        ob = ttk.Frame(left)
        ob.pack(fill="both", expand=True)
        self.ops = ttk.Treeview(ob, show="tree", selectmode="browse")
        ys = ttk.Scrollbar(ob, orient="vertical", command=self.ops.yview)
        self.ops.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.ops.pack(side="left", fill="both", expand=True)
        self.ops.bind("<Double-1>", lambda e: self.add_selected())
        self.ops.bind("<Return>", lambda e: self.add_selected())
        ttk.Button(left, text="Add as the next step ▸", command=self.add_selected).pack(
            fill="x", pady=(4, 0))

        # 2. the steps and the selected step's settings
        mid = ttk.Frame(body)
        body.add(mid, weight=1)
        ttk.Label(mid, text="2  Steps, top to bottom", style="Heading.TLabel").pack(anchor="w")
        sb = ttk.Frame(mid)
        sb.pack(fill="both", expand=True, pady=(2, 2))
        self.list = ttk.Treeview(sb, show="tree", selectmode="browse", height=6)
        ys2 = ttk.Scrollbar(sb, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=ys2.set)
        ys2.pack(side="right", fill="y")
        self.list.pack(side="left", fill="both", expand=True)
        self.list.bind("<<TreeviewSelect>>", lambda e: self._edit_selected())
        self.list.bind("<Delete>", lambda e: self.remove())
        lb = FlowFrame(mid)
        lb.pack(fill="x")
        lb.add(ttk.Button(lb, text="↑", width=3, style="Small.TButton",
                          command=lambda: self.move(-1)))
        lb.add(ttk.Button(lb, text="↓", width=3, style="Small.TButton",
                          command=lambda: self.move(1)), gap=2)
        lb.add(ttk.Button(lb, text="Remove", style="Small.TButton", command=self.remove),
               gap=2)
        self.form = ttk.LabelFrame(mid, text="Settings of the selected step")
        self.form.pack(fill="x", pady=(6, 0))
        self.form_msg = ttk.Label(self.form, style="M.TLabel",
                                  text="Add a step, then set it here.")
        self.form_msg.grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=4)

        # 3. the preview
        right = ttk.Frame(body)
        body.add(right, weight=1)
        body.bind("<Configure>", lambda e: self._split(body), add="+")
        ttk.Label(right, text="3  Preview on real values", style="Heading.TLabel").pack(
            anchor="w")
        pb = ttk.Frame(right)
        pb.pack(fill="both", expand=True, pady=(2, 0))
        self.preview = ttk.Treeview(pb, columns=("stored", "result", "why"), show="headings",
                                    selectmode="none")
        for c, t, w in (("stored", "Stored value", 150), ("result", "Result", 150),
                        ("why", "Why empty", 150)):
            self.preview.heading(c, text=t)
            self.preview.column(c, width=w)
        ys3 = ttk.Scrollbar(pb, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=ys3.set)
        ys3.pack(side="right", fill="y")
        self.preview.pack(side="left", fill="both", expand=True)
        self.preview.tag_configure("empty", foreground=K["danger_text"])
        self.summary = wrap_to_width(ttk.Label(right, style="M.TLabel", text=""))
        self.summary.pack(fill="x", pady=(4, 0))

        bar = FlowFrame(self)
        bar.pack(fill="x", padx=12, pady=10)
        self.ok_btn = bar.add(ttk.Button(bar, text="Add the column", style="P.TButton",
                                         command=self.ok))
        bar.add(ttk.Button(bar, text="Cancel", command=self.destroy), gap=8)
        self.bind("<Escape>", lambda e: self.destroy())
        self._fill_ops()
        self._fill_steps()
        self.refresh()

    def _split(self, body):
        """The operation names and the steps read in full: 30% / 30% / 40% (once)."""
        if getattr(self, "_split_done", False):
            return
        try:
            w = body.winfo_width()
            if w > 300:
                body.sashpos(0, int(w * 0.29))
                body.sashpos(1, int(w * 0.60))
                self._split_done = True
        except tk.TclError:
            pass

    # -- operations ---------------------------------------------------------------------------
    def _fill_ops(self):
        t = self.ops
        t.delete(*t.get_children())
        term = self.find.var.get().strip().lower() if hasattr(self.find, "var") else ""
        groups = {}
        for key, group, label, settings, _fn, words in calc.OPS:
            if group == "With another column" and not self.others:
                continue
            if term and term not in label.lower() and term not in words.lower() and \
                    term not in group.lower():
                continue
            if group not in groups:
                groups[group] = t.insert("", "end", text=group, open=True)
            t.insert(groups[group], "end", iid="op:" + key, text=label)
        first = next((i for g in t.get_children() for i in t.get_children(g)), None)
        if first:
            t.selection_set(first)
            t.focus(first)

    def add_selected(self):
        sel = self.ops.selection()
        if sel and sel[0].startswith("op:"):
            self.add(sel[0][3:])

    def add(self, key):
        st = calc.new_step(key)
        for i, (name, kind, _d) in enumerate(calc.settings_of(key)):
            if kind == "column" and self.others:
                st[i + 1] = self.others[0]
        self.steps.append(st)
        self._fill_steps(select=len(self.steps) - 1)
        if not self.name_var.get().strip():
            self.name_var.set("%s (calculated)" % self.source)
        self.refresh()

    # -- steps ----------------------------------------------------------------------------------
    def _fill_steps(self, select=None):
        t = self.list
        t.delete(*t.get_children())
        for i, st in enumerate(self.steps):
            t.insert("", "end", iid=str(i), text="%d.  %s" % (i + 1, calc.step_text(st)))
        if select is not None and 0 <= select < len(self.steps):
            t.selection_set(str(select))
            t.focus(str(select))
        else:
            self._edit_selected()

    def _selected(self):
        sel = self.list.selection()
        return int(sel[0]) if sel else None

    def move(self, d):
        i = self._selected()
        if i is None or not 0 <= i + d < len(self.steps):
            return
        self.steps[i], self.steps[i + d] = self.steps[i + d], self.steps[i]
        self._fill_steps(select=i + d)
        self.refresh()

    def remove(self):
        i = self._selected()
        if i is None:
            return
        del self.steps[i]
        self._fill_steps(select=min(i, len(self.steps) - 1))
        self.refresh()

    def _edit_selected(self):
        for w in self._editors:
            w.destroy()
        self._editors = []
        i = self._selected()
        if i is None:
            self.form_msg.configure(text="Add a step, then set it here.")
            return
        st = self.steps[i]
        settings = calc.settings_of(st[0])
        if not settings:
            self.form_msg.configure(text="%s has nothing to set." % calc.get(st[0])[2])
            return
        self.form_msg.configure(text=calc.get(st[0])[2])
        if st[0] == "expr":
            self._formula_editor(i, st)
            return
        for k, (name, kind, _d) in enumerate(settings):
            lab = ttk.Label(self.form, text=name.capitalize())
            lab.grid(row=k + 1, column=0, sticky="w", padx=6, pady=2)
            var = tk.StringVar(value=str(st[k + 1]))
            if kind == "column":
                box = SearchableCombobox(self.form, values=self.others, textvariable=var,
                                         width=28)
                box.bind("<<ComboboxSelected>>", lambda e, k=k, v=var, i=i, kd=kind:
                         self._set(i, k, kd, v.get()))
            else:
                box = ttk.Entry(self.form, textvariable=var, width=30)
            box.grid(row=k + 1, column=1, sticky="we", padx=6, pady=2)
            var.trace_add("write", lambda *_a, k=k, v=var, i=i, kd=kind:
                          self._set(i, k, kd, v.get()))
            self._editors += [lab, box]
        self.form.columnconfigure(1, weight=1)

    # -- the formula editor -------------------------------------------------------------------
    EXAMPLES = (
        ("x / 100", "an amount kept in the smallest unit"),
        ("round(x / 100, 2)", "two decimals"),
        ("thousands(x / 100, 2)", "1,234.50"),
        ("x * 1.18", "add 18 %"),
        ("x if x > 0 else 0", "negatives as 0"),
        ("upper(left(x, 3))", "the first three letters, upper case"),
        ("date(x, \"ms\")", "a Unix-millisecond timestamp as a UTC date"),
        ("json(x, \"to.name\")", "a field inside a JSON value"),
        ("coalesce(x, \"-\")", "a dash when empty"),
        ("len(x)", "how long the text is"),
    )

    def _formula_editor(self, i, st):
        """The 'Formula' step's settings: the formula (checked as you type, the preview
        live) and, under it, a column, a function or an example to insert, each picked from
        a searchable list."""
        from engine import formula as fm
        f = self.form
        text = st[1] if len(st) > 1 else ""
        box = tk.Text(f, height=3, wrap="word", font=F["mono"], undo=True,
                      bg=C["bg"], fg=C["text"], insertbackground=C["text"],
                      relief="solid", bd=1, highlightthickness=0, padx=6, pady=4)
        box.insert("1.0", text)
        box.grid(row=1, column=0, columnspan=2, sticky="we", padx=6, pady=(2, 0))
        status = wrap_to_width(ttk.Label(f, style="M.TLabel", text=""))
        status.grid(row=2, column=0, columnspan=2, sticky="we", padx=6)

        def insert(piece):
            box.insert("insert", piece)
            box.focus_set()
            changed()

        def col_piece(name):
            return name if name.isidentifier() and name not in fm.FUNCS else \
                'col("%s")' % name.replace('"', '\\"')
        rows = []
        # a column of the row (x first)
        cols = SearchableCombobox(f, values=["x  (this column's value)"] + self.others,
                                  width=30, placeholder="type to find a column…")
        cols.bind("<<ComboboxSelected>>", lambda e: (
            insert("x" if cols.get().startswith("x  (") else col_piece(cols.get())),
            cols.set("")))
        rows.append(("Insert a column", cols))
        # a function
        fnames = ["%s  -  %s" % (n, w) for n, w in fm.FUNCTION_HELP
                  if "(" in n or n.split(",")[0].strip() in fm.FUNCS]
        fns = SearchableCombobox(f, values=fnames, width=30,
                                 placeholder="type to find a function…")

        def insert_fn(_e=None):
            first = fns.get().split("  -  ")[0].split(",")[0].strip()
            name = first.split("(")[0].strip()
            if name:
                insert(name + "(" if name in fm.FUNCS else first)
            fns.set("")
        fns.bind("<<ComboboxSelected>>", insert_fn)
        rows.append(("Insert a function", fns))
        # an example, in place of the formula
        exs = SearchableCombobox(f, values=["%s  -  %s" % (e, w) for e, w in self.EXAMPLES],
                                 width=30, placeholder="examples…")

        def use_example(_e=None):
            ex = exs.get().split("  -  ")[0]
            if ex:
                box.delete("1.0", "end")
                box.insert("1.0", ex)
                changed()
            exs.set("")
        exs.bind("<<ComboboxSelected>>", use_example)
        rows.append(("Start from an example", exs))
        widgets = [box, status]
        for k, (label, w) in enumerate(rows):
            lab = ttk.Label(f, text=label, style="M.TLabel")
            lab.grid(row=3 + k, column=0, sticky="w", padx=6, pady=(3, 0))
            w.grid(row=3 + k, column=1, sticky="we", padx=6, pady=(3, 0))
            widgets += [lab, w]
        hint = wrap_to_width(ttk.Label(f, style="M.TLabel", text=(
            "Excel-like: + - * / ** %, comparisons, and / or / not, 'a if test else b', text "
            "in quotes, [start:end] of a text; Help lists every function.")))
        hint.grid(row=3 + len(rows), column=0, columnspan=2, sticky="we", padx=6, pady=(4, 2))
        widgets.append(hint)
        self._formula_after = None

        def changed(_e=None):
            if self._formula_after is not None:
                try:
                    self.after_cancel(self._formula_after)
                except tk.TclError:
                    pass
            self._formula_after = self.after(120, apply)

        def apply():
            self._formula_after = None
            if i >= len(self.steps) or not box.winfo_exists():
                return
            t = box.get("1.0", "end-1c")
            why = fm.check(t)
            if why is None:
                names = fm.compile_formula(t).names
                unknown = [n for n in names if n not in self.others]
                if unknown:
                    status.configure(text="No column named %s in this row." % ", ".join(
                        unknown), foreground=K["danger_text"])
                else:
                    status.configure(text="Reads: x%s." % (
                        ", " + ", ".join(names) if names else " only"),
                        foreground=K["success_text"])
            else:
                status.configure(text="Not a formula yet: %s" % why,
                                 foreground=K["danger_text"])
            self.steps[i][1] = t
            self.list.item(str(i), text="%d.  %s" % (i + 1, calc.step_text(self.steps[i])))
            self.refresh()
        box.bind("<KeyRelease>", changed)
        box.bind("<<Paste>>", changed)
        box.bind("<Tab>", lambda e: (cols.focus_set(), "break")[1])
        box.focus_set()
        self._editors += widgets
        self.form.columnconfigure(1, weight=1)
        apply()

    def _set(self, i, k, kind, text):
        if i >= len(self.steps):
            return
        try:
            self.steps[i][k + 1] = calc.coerce_setting(kind, text)
        except ValueError as e:
            self.summary.configure(text="Step %d: %s" % (i + 1, e))
            return
        self.list.item(str(i), text="%d.  %s" % (i + 1, calc.step_text(self.steps[i])))
        self.refresh()

    # -- preview ----------------------------------------------------------------------------------
    def refresh(self):
        t = self.preview
        t.delete(*t.get_children())
        empty = 0
        bad = [n + 1 for n, st in enumerate(self.steps) if calc.check_step(st)]
        for n, (value, row) in enumerate(self.samples):
            result, why = calc.explain(value, self.steps, row.get) if self.steps else (value,
                                                                                      None)
            if value is None and why == "empty":
                why = None              # nothing stored: nothing to explain
            if result is None and value is not None:
                empty += 1
            t.insert("", "end", iid=str(n), values=(_shown(value), _shown(result), why or ""),
                     tags=("empty",) if why and value is not None else ())
        if not self.steps:
            self.summary.configure(text="No steps yet: the result is the stored value.")
        else:
            self.summary.configure(text="%s.%s" % (
                calc.describe(self.steps), "  %d of the %d sample values give no result (see "
                "'Why empty')." % (empty, len(self.samples)) if empty else ""))
        if bad:
            self.summary.configure(text="Step %s cannot be used yet: %s" % (
                bad[0], calc.check_step(self.steps[bad[0] - 1])))
        self.ok_btn.state(["!disabled"] if self.steps and not bad else ["disabled"])

    def ok(self):
        if not self.steps:
            return
        self.on_ok([list(s) for s in self.steps], self.name_var.get().strip() or None)
        self.destroy()
