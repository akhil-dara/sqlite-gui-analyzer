"""Calculated columns in Browse: a NEW column worked out from a column's values by steps
(engine.calc: a formula of your own, divide, round, thousands, text edits, another column of
the row...), named 'ƒ= <name>' and drawn in the derived colour, after the table's own columns
and the ƒ field columns. The stored values never change; the grid sorts, filters, copies and
exports the column like any other (browse_sources.DerivedSource). The choice is kept per
table name in the tool's own settings (never near the evidence), like the ƒ field columns.

Right-click a column header › Calculate from <column>… opens the calculator (calc_ui) with
a live preview on the rows shown; on a calculated column: Edit the calculation…, Remove.
"""

from engine import calc

PREFIX = "ƒ= "         # ƒ=: a value the tool works out from the row (not stored)
COLOR = "purple"
SETTING = "calc_columns"    # {table name: [[label, column, steps]]}


class BrowseCalcs(object):
    def __init__(self, app):
        self.app = app
        self.chosen = {}            # table name -> [(label, source column, steps)]
        try:
            saved = app.tags.settings.get(SETTING)
        except Exception:           # noqa: BLE001 - no settings yet
            saved = None
        if isinstance(saved, dict):
            for table, items in saved.items():
                good = []
                for it in items:
                    try:
                        label, column, steps = it
                    except (TypeError, ValueError):
                        continue
                    steps = [list(st) for st in steps if isinstance(st, (list, tuple)) and st]
                    if steps:
                        good.append((str(label), str(column), steps))
                if good:
                    self.chosen[str(table)] = good

    @property
    def grid(self):
        return self.app._browse_grid

    def _save(self):
        try:
            self.app.tags.settings[SETTING] = dict(
                (t, [[a, b, [list(st) for st in steps]] for a, b, steps in items])
                for t, items in self.chosen.items() if items)
            self.app.tags.save_settings()
        except Exception:           # noqa: BLE001 - a choice not kept is not fatal
            pass

    def table(self):
        t = self.app._browse_table_var.get()
        return t or None

    def calcs_of(self, table, columns=None):
        """[(label, source column, steps)] of the table; with `columns` (the source's), only
        those whose source column exists there (a remembered choice of another database's
        table of the same name may not fit)."""
        items = self.chosen.get(table, [])
        if columns is None:
            return list(items)
        have = set(columns)
        return [c for c in items if c[1] in have]

    def derived(self, table, columns):
        """The DerivedColumns of a table, for its source (columns: the source's, so a
        calculation can read a ƒ field column too)."""
        from browse_sources import DerivedColumn
        out = []
        for label, column, steps in self.calcs_of(table, columns):
            out.append(DerivedColumn(
                PREFIX + label, column,
                lambda v, look, st=steps: calc.apply(v, st, look),
                "worked out by the tool from %s: %s - not a column of the table" % (
                    column, calc.describe(steps)), row=True))
        return out

    def wrap(self, table, source):
        """The table's source with its calculated columns, or the source itself."""
        from browse_sources import DerivedSource
        d = self.derived(table, list(source.columns()))
        return DerivedSource(source, d) if d else source

    def apply(self, table, reload=False):
        """The grid shows table: the calculated columns are drawn in their colour (and the
        table is loaded again when the choice just changed)."""
        if reload:
            if self.table() == table:
                self.app._load_browse_table()
            return
        cols = self.grid.columns()
        colors = dict(self.grid._col_colors)
        colors.update((i, COLOR) for i, c in enumerate(cols) if c.startswith(PREFIX))
        self.grid.set_column_colors(colors)

    def tooltip(self, column):
        table = self.table()
        if table is None or not column.startswith(PREFIX):
            return None
        label = column[len(PREFIX):]
        for lbl, src, steps in self.calcs_of(table):
            if lbl == label:
                return ("Worked out by the tool from %s: %s - not a column of the table "
                        "(right-click the header to edit or remove it)" % (
                            src, calc.describe(steps)))
        return None

    def reset(self):
        pass

    # -- adding, editing, removing ------------------------------------------------------------
    def _add(self, table, label, column, steps):
        have = self.chosen.setdefault(table, [])
        base, n = label, 2
        while any(c[0] == label for c in have):
            label = "%s (%d)" % (base, n)
            n += 1
        have.append((label, column, [list(st) for st in steps]))
        self._save()
        self.apply(table, reload=True)

    def _replace(self, table, old_label, label, steps):
        items = self.chosen.get(table, [])
        for k, (lbl, src, _st) in enumerate(items):
            if lbl == old_label:
                items[k] = (label or lbl, src, [list(st) for st in steps])
        self._save()
        self.apply(table, reload=True)

    def remove(self, table, label):
        self.chosen[table] = [c for c in self.chosen.get(table, []) if c[0] != label]
        if not self.chosen[table]:
            del self.chosen[table]
        self._save()
        self.apply(table, reload=True)

    def remove_all(self, table):
        self.chosen.pop(table, None)
        self._save()
        self.apply(table, reload=True)

    def _samples(self, column, n=15):
        """[(value, {heading: value})] from the rows shown, for the live preview."""
        cols = list(self.grid.columns())
        if column not in cols:
            return []
        k = cols.index(column)
        out = []
        try:
            first = max(0, self.grid.visible_row_range()[0])
        except Exception:           # noqa: BLE001 - from the top then
            first = 0
        try:
            for vals, _f in self.grid.fetch_rows(first, first + n - 1):
                out.append((vals[k] if k < len(vals) else None, dict(zip(cols, vals))))
        except Exception:           # noqa: BLE001 - a preview without rows still works
            pass
        return out

    def dialog(self, table, column, edit_label=None):
        """The calculator for `column` (edit_label: change that calculated column)."""
        from calc_ui import CalcDialog
        cols = list(self.grid.columns())
        steps, name, source = None, None, column
        if edit_label is not None:
            for lbl, src, st in self.calcs_of(table):
                if lbl == edit_label:
                    steps, name, source = [list(x) for x in st], lbl, src
            if steps is None:
                return None
        others = [c for c in cols if c != source and c != PREFIX + (name or "")
                  and not c.startswith("#")]

        def ok(new_steps, new_name):
            if edit_label is not None:
                self._replace(table, edit_label, new_name, new_steps)
            else:
                self._add(table, new_name or "%s (calculated)" % source, source, new_steps)
        return CalcDialog(self.app, source, self._samples(source), others, ok,
                          steps=steps, name=name)

    # -- the header menu ----------------------------------------------------------------------
    def header_menu(self, menu, c):
        table = self.table()
        cols = self.grid.columns()
        if table is None or c < 1 or c >= len(cols):
            return
        column = cols[c]
        menu.add_separator()
        if column.startswith(PREFIX):
            label = column[len(PREFIX):]
            menu.add_command(label="Edit the calculation of %s…" % column,
                             command=lambda: self.dialog(table, column, edit_label=label))
            menu.add_command(label="Remove the calculated column %s" % column,
                             command=lambda: self.remove(table, label))
            if len(self.calcs_of(table)) > 1:
                menu.add_command(label="Remove every calculated column",
                                 command=lambda: self.remove_all(table))
            return
        menu.add_command(label="Calculate from %s… (a new column: formula, ÷, "
                               "×, round, text…)" % column,
                         command=lambda: self.dialog(table, column))
