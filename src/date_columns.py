"""Browse: show a column's stored numbers (or date text) as UTC dates.

Right-click a column header > Show as date > Auto / Unix s, ms, us, ns / Cocoa / WebKit /
FILETIME / HFS+ / .NET / OLE / GPS / date text / Off. The grid then draws the converted date
through its column-formatter hook (grid.DataGrid.set_column_formatter); sorting, filters,
copies of rows and exports keep the raw values, and the tooltip, the row inspector and 'Copy
raw value' give the raw value. Auto uses the timeline's detector (engine.timeline.judge) on a
sample of the column. The choice is kept per table with the tags of the database (never next to
the evidence) and applied again when the table is shown.
"""

import tkinter as tk
from tkinter import messagebox, ttk

from engine import limits
from tokens import FONT as F
from engine import timeline as tl

SECTION = "date_formats"        # {table: {column: kind or 'auto'}, '_display': {...}}
DISPLAY_KEY = "_display"        # {'format': 'iso'/'12h'/'custom', 'custom': strftime}
# the names of engine.decode.timestamps (the one decoder of the tool), in its order
CHOICES = tuple((k, tl.LABELS[k]) for k in tl.KINDS)


class BrowseDates(object):
    """The 'Show as date' choices of a grid (App._browse_dates for the Browse grid; a
    RelatedWindow makes its own for its grid, sharing the saved choices)."""

    def __init__(self, app, grid=None, table_fn=None, db_fn=None):
        self.app = app
        self._grid = grid               # None: the Browse grid
        self._table_fn = table_fn        # None: the Browse table
        self._db_fn = db_fn              # None: self.app.db
        self._suggested = {}            # (table, column) -> kind or None
        self._waiting = {}              # (table, column) -> [then(kind)] while a sample is read
        self._runner = None             # reads the samples (grid.Runner, made on first use)
        self.warn_with_dialogs = True   # ui_smoke reads last_message instead
        self.last_message = ""

    @property
    def grid(self):
        return self._grid if self._grid is not None else self.app._browse_grid

    def reset(self):
        self._suggested = {}
        self._waiting = {}
        if self._runner is not None:
            self._runner.cancel()       # a sample of the database shown until now

    def worker_threads(self):
        return self._runner.threads() if self._runner is not None else []

    def table(self):
        if self._table_fn is not None:
            return self._table_fn()
        t = self.app._browse_table_var.get()
        return t if t and self.app._browse_source is not None else None

    def _db(self):
        return self._db_fn() if self._db_fn is not None else self.app.db

    # -- saved choices -------------------------------------------------------------------------
    def saved(self, table):
        tags = getattr(self.app, 'tags', None)
        per = tags.state_section(SECTION).get(table) if tags is not None else None
        return dict((c, k) for c, k in per.items() if k in tl.KINDS or k == tl.AUTO) \
            if isinstance(per, dict) else {}

    def choice(self, table, column):
        return self.saved(table).get(column)

    def display_format(self):
        """(format, custom strftime): how 'Show as date' renders dates, everywhere."""
        tags = getattr(self.app, 'tags', None)
        d = tags.state_section(SECTION).get(DISPLAY_KEY) if tags is not None else None
        if not isinstance(d, dict):
            return "iso", ""
        fmt = d.get("format", "iso")
        return (fmt, d.get("custom", "")) if fmt in tl.TIME_FORMAT_KEYS else ("iso", "")

    def set_display_format(self, fmt, custom=""):
        st = self.app.tags.state_section(SECTION)
        st[DISPLAY_KEY] = {"format": fmt, "custom": custom}
        self.app.tags.set_state_section(SECTION, st)
        # re-render every column shown as a date, in this grid
        table = self.table()
        if table is not None:
            for column in self.saved(table):
                self._apply_one(table, column, self.choice(table, column), quiet=True)

    def _store(self, table, column, choice):
        st = self.app.tags.state_section(SECTION)
        per = st.get(table) if isinstance(st.get(table), dict) else {}
        if choice in (None, tl.OFF):
            per.pop(column, None)
        else:
            per[column] = choice
        if per:
            st[table] = per
        else:
            st.pop(table, None)
        self.app.tags.set_state_section(SECTION, st)

    # -- the detector ----------------------------------------------------------------------------
    def suggest(self, table, column):
        """The kind the timeline detector picks for a sample of the column (cached), read now
        (on this thread). The UI uses suggest_later()."""
        key = (table, column)
        if key not in self._suggested:
            self._suggested[key] = self._judge(*self._sample_job(table, column))
        return self._suggested[key]

    def known(self, table, column):
        """(True, kind) once the sample of the column was judged, else (False, None)."""
        key = (table, column)
        return (True, self._suggested[key]) if key in self._suggested else (False, None)

    def suggest_later(self, table, column, then=None):
        """Judge a sample of the column on a worker thread (the Tk thread never waits for a
        large table), then then(kind) on the Tk thread; at once when it is known."""
        key = (table, column)
        if key in self._suggested:
            if then is not None:
                then(self._suggested[key])
            return
        reading = key in self._waiting
        waiting = self._waiting.setdefault(key, [])
        if then is not None:
            waiting.append(then)
        if reading:
            return                      # already being read
        read = self._sample_job(table, column)
        db = self._db()

        def done(kind, _error):
            if self._db() is not db:
                return                  # another database since
            self._suggested[key] = kind
            for fn in self._waiting.pop(key, []):
                fn(kind)
        if self._runner is None:
            from grid import Runner
            self._runner = Runner(self.app, "date-sample",
                                  release=getattr(self.app, "_release_worker_connection", None))
        self._runner.submit(key, lambda: self._judge(*read), done)

    def _sample_job(self, table, column):
        """(column, reader, declared type): reader() gives the sample values (a WAL-only
        table's are taken now, from the rows in memory; a table's are read when called)."""
        db = self._db()
        if table.startswith("WAL: "):
            src = self.app._browse_source
            cols = src.columns() if src is not None else []
            i = cols.index(column) if column in cols else None
            values = []
            if i is not None:
                for n, row in enumerate(src.iter_all()):
                    if n >= 2 * limits.get("timeline_sample_rows"):
                        break
                    values.append(row[i] if i < len(row) else None)
            return column, (lambda: values), ""
        session = db.session
        try:
            decl = dict(db.columns(table)).get(column, "")
        except Exception:               # noqa: BLE001 - no declared type then
            decl = ""
        return column, (lambda: tl.sample_column(session, table, column)), decl

    @staticmethod
    def _judge(column, reader, decl):
        try:
            return tl.judge(column, reader(), decl).kind
        except Exception:               # noqa: BLE001 - no suggestion then
            return None

    # -- applying ---------------------------------------------------------------------------------
    def apply(self, table):
        """The Browse grid shows `table` now: give its columns their saved date formats."""
        for column, choice in self.saved(table).items():
            self._apply_one(table, column, choice, quiet=True)

    def _apply_one(self, table, column, choice, quiet=False):
        cols = self.grid.columns()
        if column not in cols:
            return None
        if choice == tl.AUTO:
            ok, kind = self.known(table, column)
            if not ok:                  # the sample is read on a worker, then applied
                def then(kind):
                    if self.table() == table and self.choice(table, column) == tl.AUTO:
                        self._show_kind(table, column, choice, kind, quiet)
                self.suggest_later(table, column, then)
                return None
        else:
            kind = choice
        return self._show_kind(table, column, choice, kind, quiet)

    def _show_kind(self, table, column, choice, kind, quiet):
        cols = self.grid.columns()
        if column not in cols:
            return None
        c = cols.index(column)
        if kind in tl.KINDS:
            fmt, custom = self.display_format()
            self.grid.set_column_formatter(c, tl.formatter(kind, fmt, custom),
                                           "UTC " + tl.SHORT[kind])
        else:
            self.grid.set_column_formatter(c, None)
            if choice == tl.AUTO and not quiet:
                self._tell("No date format fits the values of %s: it is shown as stored."
                           % column)
        return kind

    def set(self, table, column, choice):
        """Show a column as dates of `choice` (a kind, 'auto', or 'off'); remembered."""
        self._store(table, column, choice)
        return self._apply_one(table, column, choice)

    def _tell(self, text):
        self.last_message = text
        if self.warn_with_dialogs:
            messagebox.showinfo("Show as date", text, parent=self.app)

    # -- the header menu ----------------------------------------------------------------------------
    def header_menu(self, menu, c):
        """DataGrid on_header_menu: the 'Show as date' submenu for a data column."""
        table = self.table()
        cols = self.grid.columns()
        if table is None or c < 1 or c >= len(cols):
            return None
        column = cols[c]
        sub = tk.Menu(menu, tearoff=0)
        current = self.choice(table, column) or tl.OFF
        var = tk.StringVar(master=sub, value=current)
        sub.var = var
        ok, kind = self.known(table, column)
        if not ok:
            self.suggest_later(table, column)   # read now on a worker: named next time
        sub.add_radiobutton(label="Auto (%s)" % (
                                ("no date format fits" if kind is None else tl.LABELS[kind])
                                if ok else "reading a sample…"),
                            variable=var, value=tl.AUTO,
                            command=lambda: self.set(table, column, tl.AUTO))
        sub.add_separator()
        for k, label in CHOICES:
            sub.add_radiobutton(label=label + ("   (suggested)" if k == kind else ""),
                                variable=var, value=k,
                                command=lambda k=k: self.set(table, column, k))
        sub.add_separator()
        sub.add_radiobutton(label="Off (show as stored)", variable=var, value=tl.OFF,
                            command=lambda: self.set(table, column, tl.OFF))
        menu.add_separator()
        menu.add_cascade(label="Show as date", menu=sub)
        # how the dates render (ISO / 12-hour / custom strftime), for every column
        fmt, _custom = self.display_format()
        dsub = tk.Menu(menu, tearoff=0)
        dvar = tk.StringVar(master=dsub, value=fmt)
        dsub.var = dvar
        for _k, _label, _b, _a in tl.TIME_FORMATS:
            if _k == "custom":
                dsub.add_radiobutton(label=_label, variable=dvar, value=_k,
                                     command=self._custom_display_format)
            else:
                dsub.add_radiobutton(label=_label, variable=dvar, value=_k,
                                     command=lambda k=_k: self.set_display_format(k))
        menu.add_cascade(label="Date display", menu=dsub)
        return sub

    def _custom_display_format(self, on_done=None, sample=None):
        """The date format chooser (DateFormatDialog): the styles with an example each, ready
        patterns and one of your own; on_done() after it is applied. sample: the datetime the
        examples show (a value of the row), else a fixed one."""
        _fmt, custom = self.display_format()
        fmt, _c = self.display_format()

        def apply(key, pattern):
            if key == "custom":
                self.set_display_format("custom", pattern)
            else:
                self.set_display_format(key)
            if on_done is not None:
                on_done()
        return DateFormatDialog(self.app, fmt, custom, apply, sample)


# ready patterns offered beside the presets: (pattern, what it is)
EXAMPLE_PATTERNS = (
    ("%d-%b-%Y %H:%M:%S", "Day-month-year, 24-hour"),
    ("%d/%m/%Y %I:%M:%S %p", "Day/month/year, 12-hour"),
    ("%d-%b-%Y %I:%M:%S.%7f %p", "Seven fraction digits, 12-hour (as .NET ticks)"),
    ("%Y-%m-%dT%H:%M:%S.%3fZ", "ISO 8601 with milliseconds, UTC marker"),
    ("%a %d %b %Y %H:%M:%S", "With the weekday"),
    ("%H:%M:%S", "Time only"),
)
# the codes, as buttons that insert them into your own pattern: (code, what it gives)
FORMAT_CODES = (("%d", "day 01-31"), ("%b", "month Jan"), ("%B", "month January"),
                ("%m", "month 01-12"), ("%Y", "year 2026"), ("%y", "year 26"),
                ("%H", "hour 00-23"), ("%I", "hour 01-12"), ("%M", "minutes"),
                ("%S", "seconds"), ("%3f", "milliseconds"), ("%6f", "microseconds"),
                ("%7f", "7 fraction digits"), ("%p", "AM / PM"), ("%a", "weekday Mon"),
                ("%j", "day of the year"))


class DateFormatDialog(tk.Toplevel):
    """Choose how dates are written: each preset and ready pattern with an example, or a
    pattern of your own (the codes are buttons, the result previews as you type). apply(key,
    pattern) is called with the choice (key 'custom' for a pattern)."""

    def __init__(self, parent, fmt, custom, apply, sample=None):
        from datetime import datetime
        tk.Toplevel.__init__(self, parent)
        self.title("Date format")
        self.transient(parent)
        self._apply = apply
        self.sample = sample or datetime(2026, 10, 2, 14, 30, 45, 123456)
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Choose a style", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text="The examples show %s." % (
            "this value" if sample else "a sample date"), style="Muted.TLabel").pack(anchor="w")
        self.choice = tk.StringVar(master=self)
        rows = ttk.Frame(body)
        rows.pack(fill="x", pady=(6, 4))
        self._choices = {}
        r = 0
        for key, label, _b, _a in tl.TIME_FORMATS:
            if key == "custom":
                continue
            self._add_row(rows, r, "preset:" + key, label, tl.format_dt(self.sample, key))
            r += 1
        for pattern, label in EXAMPLE_PATTERNS:
            self._add_row(rows, r, "pattern:" + pattern, label,
                          tl.format_dt(self.sample, "custom", pattern))
            r += 1
        self._add_row(rows, r, "own", "Write your own (below)", "")
        own = ttk.LabelFrame(body, text="Your own pattern", padding=8)
        own.pack(fill="x", pady=(8, 0))
        self.pattern = tk.StringVar(master=self, value=custom or "%d-%b-%Y %I:%M:%S.%7f %p")
        self.entry = ttk.Entry(own, textvariable=self.pattern, width=44, font=F["mono_large"])
        self.entry.pack(fill="x")
        codes = ttk.Frame(own)
        codes.pack(fill="x", pady=(6, 0))
        from widgets import ToolTip
        for i, (code, what) in enumerate(FORMAT_CODES):
            b = ttk.Button(codes, text=code, width=4, style="Small.TButton",
                           command=lambda c=code: self._insert(c))
            b.grid(row=i // 8, column=i % 8, padx=1, pady=1, sticky="w")
            ToolTip(b, "%s  \u2192  %s (%s)" % (code, tl.format_dt(
                self.sample, "custom", code), what))
        self.preview = ttk.Label(own, text="", style="Heading.TLabel")
        self.preview.pack(anchor="w", pady=(8, 0))
        self.advice = ttk.Label(own, text="", style="Muted.TLabel", wraplength=460,
                                justify="left")
        self.advice.pack(anchor="w")
        self.pattern.trace_add("write", lambda *_a: self._typed())
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Apply", style="Primary.TButton", command=self.ok).pack(
            side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.ok())
        current = "preset:" + fmt if fmt in tl.TIME_FORMAT_KEYS and fmt != "custom" else (
            "pattern:" + custom if any(p == custom for p, _l in EXAMPLE_PATTERNS) else "own")
        self.choice.set(current)
        self._typed(select=False)

    def _add_row(self, rows, r, value, label, example):
        rb = ttk.Radiobutton(rows, text=label, value=value, variable=self.choice)
        rb.grid(row=r, column=0, sticky="w", padx=(0, 16), pady=1)
        ex = ttk.Label(rows, text=example, font=F["mono_large"])
        ex.grid(row=r, column=1, sticky="w")
        if value.startswith("pattern:"):
            # a ready pattern copies itself into 'your own' too, to change it from there
            rb.configure(command=lambda p=value[8:]: self.pattern.set(p))
        self._choices[value] = (rb, ex)

    def _insert(self, code):
        self.entry.insert("insert", code)
        self.entry.focus_set()
        self.choice.set("own")

    def _typed(self, select=True):
        p = self.pattern.get()
        if select and self.choice.get() != "own" and \
                self.choice.get() != "pattern:" + p:
            self.choice.set("own")
        text, problem = self.check(p)
        self.preview.configure(text=("Preview:  " + text) if text else "Preview:  \u2014")
        self.advice.configure(text=problem or (
            "Tip: add %3f, %6f or %7f for fraction digits, %p for AM/PM. Times stay UTC: "
            "only how they are written changes."))

    def check(self, pattern):
        """(the sample written with pattern, a problem in words or '')."""
        if not pattern.strip():
            return "", "Empty: the ISO style is used."
        text = tl.format_dt(self.sample, "custom", pattern)
        iso = tl.format_dt(self.sample, "iso")
        if "%" not in pattern:
            return text, "No codes in it: every date would read the same. Use the buttons."
        if text == iso and pattern.strip() not in ("%Y-%m-%d %H:%M:%S",):
            return text, "This pattern cannot be written: the ISO style is used instead."
        if "%I" in pattern and "%p" not in pattern:
            return text, "12-hour without %p: 02:30 could be morning or afternoon."
        return text, ""

    def ok(self):
        c = self.choice.get()
        if c.startswith("preset:"):
            self._apply(c[7:], "")
        else:
            p = c[8:] if c.startswith("pattern:") else self.pattern.get().strip()
            if not p:
                self._apply("iso", "")
            else:
                self._apply("custom", p)
        self.destroy()
