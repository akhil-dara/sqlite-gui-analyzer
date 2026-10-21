"""Fields inside a column's values (decoded BLOBs, plists, JSON): the dialog that lists them,
and Browse showing chosen fields in their column.

FieldsDialog decodes a sample of the column's values (engine.decode.fields.discover, on the
caller's worker), lists every path found with how many values have it, the kinds of value and
examples, searchable; lines are ticked by a click. The caller gives the actions (the Query tab:
each ticked field as a column, several as one column, join a table on the field; Browse: show
them in the column).

BrowseFields adds the chosen fields to the Browse grid as NEW columns after the table's own,
each named 'ƒ <field>' and drawn in its own colour (browse_sources.DerivedSource computes them
as the rows are read): the stored value stays untouched, and the grid sorts, filters, copies
and exports the derived columns like any other. The choice is kept per table name in the tool's own settings (never in or near the
evidence), so the same table in another database shows the columns too, when it has the
column they are read from.
"""

import tkinter as tk
from tkinter import simpledialog, ttk

from constants import C
from engine import limits
from tokens import COLOR as K
from widgets import FlowFrame, SearchBox, ToolTip, wrap_to_width

COLOR = "purple"        # fields read out of a value: a theme token, looked up when drawn


class FieldsDialog(tk.Toplevel):
    """The fields found inside table.column's values. load() runs on runner (the caller's
    worker) and returns (Discovery, values read); actions: [(label, primary, tip,
    fn(paths, dialog) -> number added or None)] - each gets the ticked paths in the order
    ticked."""

    def __init__(self, master, runner, table, column, load, actions):
        tk.Toplevel.__init__(self, master)
        self.title("Fields inside %s.%s" % (table, column))
        self.configure(bg=C["bg"])
        self.transient(master.winfo_toplevel())
        self.geometry("880x560")
        self.table, self.column = table, column
        self.ticked = []
        self.state = {"d": None}
        n = limits.get("blob_fields_sample")
        self.head = wrap_to_width(ttk.Label(
            self, text="Reading up to %s values of %s.%s and decoding them…" % (
                format(n, ","), table, column), style="M.TLabel"))
        self.head.pack(fill="x", padx=10, pady=(10, 4))
        self.find = SearchBox(self, placeholder="Find a field (part of its path or of a "
                              "value)…", delay=0, find_button=False, on_change=self.fill)
        self.find.pack(fill="x", padx=10)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        t = self.tree = ttk.Treeview(box, columns=("rows", "kinds", "examples"),
                                     show="tree headings", selectmode="none")
        t.heading("#0", text="Field (its path inside the value)")
        t.heading("rows", text="In values")
        t.heading("kinds", text="Kind")
        t.heading("examples", text="Examples")
        t.column("#0", width=330)
        t.column("rows", width=90, stretch=False, anchor="e")
        t.column("kinds", width=80, stretch=False)
        t.column("examples", width=330)
        ys = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        t.bind("<Button-1>", self._flip)
        t.bind("<space>", lambda e: self._toggle(t.focus()))
        self.note = ttk.Label(self, style="M.TLabel", text="")
        self.note.pack(fill="x", padx=10)
        bar = FlowFrame(self)
        bar.pack(fill="x", padx=10, pady=8)
        self.buttons = []
        for i, (label, primary, tip, fn) in enumerate(actions):
            b = bar.add(ttk.Button(bar, text=label, style="P.TButton" if primary else
                                   "TButton", command=lambda fn=fn: self._act(fn)),
                        gap=0 if i == 0 else 6)
            if tip:
                ToolTip(b, tip)
            self.buttons.append(b)
        bar.add(ttk.Button(bar, text="Close", command=self.destroy), gap=12)
        self.bind("<Escape>", lambda e: self.destroy())

        def done(result, error):
            if not self.winfo_exists():
                return
            if error is not None:
                self.head.configure(text="The values could not be read: %s" % error)
                return
            d, read = result
            self.state["d"] = d
            if not d.fields:
                self.head.configure(text="No fields inside: none of the %s values read from "
                                         "%s.%s decodes to a structure (bplist, keyed archive, "
                                         "JSON, protobuf...)." % (format(read, ","), table,
                                                                 column))
            else:
                self.head.configure(text="%d fields inside %s.%s - tick the ones you want, "
                                         "then add them as columns. (Found in %s of the %s "
                                         "values read: the first %s rows, blob_fields_sample%s; "
                                         "a field's name is its path inside the value.)" % (
                                             len(d.fields), table, column,
                                             format(d.structured, ","), format(read, ","),
                                             format(n, ","),
                                             "; the list is CUT at blob_fields_paths, %s paths" %
                                             format(limits.get("blob_fields_paths"), ",")
                                             if d.cut else ""))
            self.fill()
        runner.submit("fields-%s-%s" % (table, column), load, done)

    def fill(self, _text=None):
        t = self.tree
        t.delete(*t.get_children())
        d = self.state["d"]
        if d is None:
            return
        term = self.find.var.get().strip().lower() if hasattr(self.find, "var") else ""
        for f in sorted(d.fields, key=lambda f: (-f.count, f.order)):
            if term and term not in f.path.lower() and not any(
                    term in e.lower() for e in f.examples):
                continue
            t.insert("", "end", iid=f.path, text=("☑  " if f.path in self.ticked else "☐  ")
                     + pretty_path(f.path), values=("%s of %s" % (format(f.count, ","),
                                                      format(d.structured, ",")),
                                       f.kinds_text(), "  |  ".join(f.examples)))
        self.note.configure(text="%d ticked%s" % (len(self.ticked), (": " + ", then ".join(
            self.ticked)) if self.ticked else " - click a line to tick it"))

    def _flip(self, e):
        self._toggle(self.tree.identify_row(e.y))

    def _toggle(self, iid):
        if iid:
            self.ticked.remove(iid) if iid in self.ticked else self.ticked.append(iid)
            self.fill()

    def _act(self, fn):
        if not self.ticked:
            self.note.configure(text="Tick a field first (click its line).")
            return
        if fn(list(self.ticked), self):
            del self.ticked[:]
            self.fill()

    def discovery(self):
        return self.state["d"]


def ask_name(parent, count):
    """The name of a column made of several fields, or None when cancelled."""
    name = simpledialog.askstring(
        "One column from several fields", "Name of the column (each row shows the first of "
        "the %d fields that has a value, in the order ticked):" % count, parent=parent)
    return None if name is None else (name.strip() or None)


class BrowseFields(object):
    """Browse: fields read out of a column's values shown as NEW columns of the grid, after
    the table's own (session only: a way of looking, nothing is written anywhere). Each is
    named 'ƒ <field>' and drawn in its own colour, so it is never taken for a column of the
    table; its header's tooltip says which column and path it is read from. The grid sorts,
    filters, copies and exports them like any column (browse_sources.DerivedSource)."""

    PREFIX = "\u0192 "          # ƒ: a value the tool computes, not one the table stores

    SETTING = "derived_columns"     # settings key: {table name: [[label, column, paths]]}

    def __init__(self, app):
        self.app = app
        self.chosen = {}            # table name -> [(label, raw column, [paths])]
        try:
            saved = app.tags.settings.get(self.SETTING)
        except Exception:           # noqa: BLE001 - no settings yet
            saved = None
        if isinstance(saved, dict):
            for table, items in saved.items():
                good = [(str(a), str(b), [str(x) for x in c]) for a, b, c in items
                        if isinstance(c, (list, tuple)) and c]
                if good:
                    self.chosen[str(table)] = good

    @property
    def grid(self):
        return self.app._browse_grid

    def _key(self, table):
        return table

    def _save(self):
        """Keep the choice (per table name) in the settings."""
        try:
            self.app.tags.settings[self.SETTING] = dict(
                (t, [[a, b, list(c)] for a, b, c in items])
                for t, items in self.chosen.items() if items)
            self.app.tags.save_settings()
        except Exception:           # noqa: BLE001 - a choice not kept is not fatal
            pass

    def _table_columns(self, table):
        try:
            return set(n for n, _t in self.app.db.columns(table))
        except Exception:           # noqa: BLE001 - unknown table: no derived columns
            return set()

    def table(self):
        t = self.app._browse_table_var.get()
        return t or None

    def fields_of(self, table, column=None):
        """[(label, raw column, paths)] of the table (of one column when given), only those
        whose column the table has (a remembered choice of another database's table of the
        same name may not fit)."""
        have = self._table_columns(table)
        out = [f for f in self.chosen.get(self._key(table), []) if f[1] in have]
        return out if column is None else [f for f in out if f[1] == column]

    def derived(self, table):
        """The DerivedColumns of a table, for its source."""
        from browse_sources import DerivedColumn
        from engine.decode import fields as fl
        out = []
        for label, column, paths in self.fields_of(table):
            out.append(DerivedColumn(
                self.PREFIX + label, column, lambda v, p=paths: fl.read(v, p)[0],
                "read by the tool out of %s (%s) - not a column of the table" % (
                    column, ", else ".join(paths))))
        return out

    def wrap(self, table, source):
        """The table's source with its derived columns, or the source itself."""
        from browse_sources import DerivedSource
        d = self.derived(table)
        return DerivedSource(source, d) if d else source

    # -- the header menu ----------------------------------------------------------------------
    def header_menu(self, menu, c):
        table = self.table()
        cols = self.grid.columns()
        if table is None or c < 1 or c >= len(cols):
            return
        column = cols[c]
        menu.add_separator()
        if column.startswith(self.PREFIX):
            label = column[len(self.PREFIX):]
            menu.add_command(label="Remove the derived column %s" % column,
                             command=lambda: self.remove(table, label))
            menu.add_command(label="Remove every derived column",
                             command=lambda: self.set_off(table))
            return
        menu.add_command(label="Fields inside %s\u2026 (add them as columns)" % column,
                         command=lambda: self.dialog(table, column))
        if self.fields_of(table, column):
            menu.add_command(label="Remove the columns derived from %s" % column,
                             command=lambda: self.set_off(table, column))
            menu.add_command(label="Open in the Query tab with these fields",
                             command=lambda: self.to_query(table, column))

    def dialog(self, table, column):
        app = self.app
        n = limits.get("blob_fields_sample")
        names = list(self.grid.columns())
        k = names.index(column) if column in names else None
        src = self.grid.source

        def load():
            from engine.decode import fields as fl
            vals = []
            if src is not None and k is not None:
                r = 0
                while len(vals) < n:
                    got = src.rows(r, min(500, n - len(vals)))
                    if not got:
                        break
                    vals.extend(v[k] if k < len(v) else None for v, _f in got)
                    r += len(got)
            return fl.discover(vals, limits.get("blob_fields_paths")), len(vals)

        def each(paths, dlg):
            for p in paths:
                self._add(table, column, short_path(p), [p])
            return self._shown(table, len(paths))

        def one(paths, dlg):
            name = ask_name(dlg, len(paths))
            if name is None:
                return None
            self._add(table, column, name, paths)
            return self._shown(table, 1)
        runner = getattr(app._browse_lookups, "runner", None)
        return FieldsDialog(app, runner, table, column, load, [
            ("Add as new columns", True, "One new column per ticked field, after the table's "
             "own columns, named \u0192 <field>: sort, filter, copy and export it like any "
             "column (the stored value stays as it is)", each),
            ("Add as ONE new column\u2026", False, "For values that keep the same thing in "
             "different places (a sender under 'from' in some rows and under 'paidFrom' in "
             "others): the first ticked field that has a value", one)])

    def _add(self, table, column, label, paths):
        have = self.chosen.setdefault(self._key(table), [])
        base, n = label, 2
        while any(f[0] == label for f in have):
            label = "%s (%d)" % (base, n)
            n += 1
        have.append((label, column, list(paths)))
        self._save()

    def _shown(self, table, k):
        self.apply(table, reload=True)
        return k

    def remove(self, table, label):
        key = self._key(table)
        self.chosen[key] = [f for f in self.chosen.get(key, []) if f[0] != label]
        if not self.chosen[key]:
            del self.chosen[key]
        self._save()
        self.apply(table, reload=True)

    def set_off(self, table, column=None):
        key = self._key(table)
        if column is None:
            self.chosen.pop(key, None)
        else:
            self.chosen[key] = [f for f in self.chosen.get(key, []) if f[1] != column]
            if not self.chosen[key]:
                del self.chosen[key]
        self._save()
        self.apply(table, reload=True)

    # -- drawing --------------------------------------------------------------------------------
    def apply(self, table, reload=False):
        """The Browse grid shows table: its derived columns are drawn in their colour (and
        the table is loaded again with them when the choice just changed)."""
        if reload:
            if self.table() == table:
                self.app._load_browse_table()
            return
        cols = self.grid.columns()
        colors = dict((i, COLOR) for i, c in enumerate(cols) if c.startswith(self.PREFIX))
        self.grid.set_column_colors(colors)

    def tooltip(self, column):
        """What a derived column is, for its header's tooltip; None for a table column."""
        table = self.table()
        if table is None or not column.startswith(self.PREFIX):
            return None
        label = column[len(self.PREFIX):]
        for lbl, raw, paths in self.fields_of(table):
            if lbl == label:
                return ("Derived by the tool from %s: the field %s read out of each value - "
                        "not a column of the table (right-click the header to remove it)"
                        % (raw, ", else ".join(paths)))
        return None

    def reset(self):
        pass

    # -- the Query tab -------------------------------------------------------------------------
    def to_query(self, table, column):
        """The table in the Query tab, with the chosen fields as columns."""
        q = self.app._query
        q.clear(quiet=True)
        a = q.add_table(table)
        if a is None:
            return
        for label, _raw, paths in self.fields_of(table, column):
            q.model.add_field(a, column, paths, None if label == short_path(paths[0]) else label)
        q._changed()
        self.app._nb.select(q)
        q.status.configure(text="From Browse: %s with the fields of %s as columns - Run to read "
                                "them; sort and filter them in the results." % (table, column))


def short_path(path):
    """A field's path without the leading 'root.' (the name a derived column gets)."""
    return path[5:] if path.startswith("root.") else path


def pretty_path(path):
    """A path as the list shows it: without 'root.', and a quoted key (one holding a dot or
    a space, ["processed.isValidFeed"]) written plainly - the real path stays the line's
    identity, so what is ticked is exact."""
    import re
    return re.sub(r'\["((?:[^"\\]|\\.)*)"\]', lambda m: "." + m.group(1), short_path(path)).lstrip(".")
