"""'Show as number' of a Browse column: a stored number drawn divided by 100, 1,000 or
1,000,000 and with thousands separators, as a currency app keeps amounts in the smallest
unit (34000 stored, 340.00 shown), a sensor in thousandths, a size in bytes.

The stored value is never changed: the grid draws the derived text in its own colour with a
badge on the column name ('amount · ÷100'), copies keep the stored value, exports keep it and
add '<column> (÷100)' beside it, the tooltip shows both. The choice is kept per table and
column in the database's saved state (the tool's own files, never near the evidence), like
'Show as date'. Right-click a column header > Show as number.
"""

import tkinter as tk

SECTION = "number_formats"      # {table: {column: scale key}}
BADGE_COLOR = "teal"            # a theme token: the derived text follows the theme

# (key, divisor, decimals, menu label, header badge)
SCALES = (
    ("plain", 1, 0, "With thousands separators (1,234,567)", "1,000s"),
    ("c100", 100, 2, "÷ 100 — hundredths (amounts kept in the smallest unit: 34000 → 340.00)",
     "÷100"),
    ("k", 1000, 3, "÷ 1,000 — thousandths (34000 → 34.000)", "÷1,000"),
    ("m", 1000000, 6, "÷ 1,000,000 — millionths (micro-units: 34000000 → 34.000000)",
     "÷1,000,000"),
)
KEYS = tuple(k for k, _d, _p, _l, _b in SCALES)


def scale_of(key):
    """(divisor, decimals, badge) of a scale key, or None."""
    for k, d, p, _l, b in SCALES:
        if k == key:
            return d, p, b
    return None


def decimals_text(value, div, places):
    """value / div with `places` decimals and thousands separators, exact for integers (no
    binary float rounding: 34005 ÷ 100 is '340.05'); a float is rounded to the places."""
    if isinstance(value, int) and div > 1:
        neg = value < 0
        q, r = divmod(abs(value), div)
        text = "%s.%0*d" % (format(q, ","), places, r)
        return "-" + text if neg else text
    if isinstance(value, int):
        return format(value, ",")
    return format(value / float(div), ",.%df" % places)


def formatter(key):
    """fn(value) -> the number as text in that scale, or None for a value that is no number
    (text, BLOBs and NULL stay as they are; a numeric text is read as a number)."""
    spec = scale_of(key)
    if spec is None:
        return None
    div, places, _badge = spec

    def f(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            s = value.strip()
            try:
                value = int(s) if s.lstrip("+-").isdigit() else float(s)
            except ValueError:
                return None
        if not isinstance(value, (int, float)):
            return None
        return decimals_text(value, div, places)
    return f


class BrowseNumbers(object):
    """The 'Show as number' choices of the Browse grid (App._browse_numbers)."""

    def __init__(self, app, grid=None, table_fn=None):
        self.app = app
        self._grid = grid
        self._table_fn = table_fn

    @property
    def grid(self):
        return self._grid if self._grid is not None else self.app._browse_grid

    def table(self):
        if self._table_fn is not None:
            return self._table_fn()
        t = self.app._browse_table_var.get()
        return t if t and self.app._browse_source is not None else None

    # -- saved choices -------------------------------------------------------------------------
    def saved(self, table):
        tags = getattr(self.app, "tags", None)
        per = tags.state_section(SECTION).get(table) if tags is not None else None
        return dict((c, k) for c, k in per.items() if k in KEYS) if isinstance(per, dict) \
            else {}

    def choice(self, table, column):
        return self.saved(table).get(column)

    def _store(self, table, column, key):
        tags = getattr(self.app, "tags", None)
        if tags is None:
            return
        st = tags.state_section(SECTION)
        per = dict(st.get(table)) if isinstance(st.get(table), dict) else {}
        if key is None:
            per.pop(column, None)
        else:
            per[column] = key
        if per:
            st[table] = per
        else:
            st.pop(table, None)
        tags.set_state_section(SECTION, st)

    # -- applying ---------------------------------------------------------------------------------
    def apply(self, table):
        """The Browse grid shows `table` now: give its columns their saved number formats."""
        for column, key in self.saved(table).items():
            self._show(column, key)

    def _show(self, column, key):
        cols = self.grid.columns()
        if column not in cols:
            return False
        c = cols.index(column)
        if key is None:
            self.grid.set_column_formatter(c, None)
            return True
        spec = scale_of(key)
        if spec is None:
            return False
        self.grid.set_column_formatter(c, formatter(key), spec[2], BADGE_COLOR)
        return True

    def set(self, table, column, key):
        """Show a column divided by the scale `key` (None: as stored); remembered."""
        self._store(table, column, key)
        return self._show(column, key)

    def reset(self):
        pass

    def export_columns(self, table, names):
        """[(index of the raw column, name of the added column, fn(value) -> text)] for an
        export: each column shown as a scaled number keeps its stored value and adds
        '<column> (÷100)' as the grid shows it."""
        out = []
        for i, name in enumerate(names):
            key = self.choice(table, name)
            spec = scale_of(key) if key else None
            if spec is not None:
                out.append((i, "%s (%s)" % (name, spec[2]), formatter(key)))
        return out

    # -- the header menu ----------------------------------------------------------------------------
    def header_menu(self, menu, c):
        """DataGrid on_header_menu: the 'Show as number' submenu for a data column (one with
        a date format already keeps that: the submenu says so)."""
        table = self.table()
        cols = self.grid.columns()
        if table is None or c < 1 or c >= len(cols):
            return None
        column = cols[c]
        sub = tk.Menu(menu, tearoff=0)
        current = self.choice(table, column) or "off"
        var = tk.StringVar(master=sub, value=current)
        sub.var = var
        for key, _div, _places, label, _badge in SCALES:
            sub.add_radiobutton(label=label, variable=var, value=key,
                                command=lambda k=key: self.set(table, column, k))
        sub.add_separator()
        sub.add_radiobutton(label="Off (show as stored)", variable=var, value="off",
                            command=lambda: self.set(table, column, None))
        menu.add_cascade(label="Show as number", menu=sub)
        return sub
