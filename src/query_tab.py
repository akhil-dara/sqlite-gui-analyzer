"""Query tab: build a SELECT without typing SQL.

Tables are added from the list on the left (double-click or Add); each becomes a card on the
canvas whose column lines are ticked to show them. Adding a table joins it to the tables
already there by the relationship engine's trusted links (a declared foreign key, or a link
verified by the values), drawn as lines between the columns; a line's menu makes it INNER or
LEFT or removes it, and a column's menu joins it to a column of another card by hand. Below:
the columns shown (order, label, shown as a date, sort), the filters, distinct and the row
limit; the SQL the query runs, always shown and copyable; and the results in the grid. The
SQL is read-only like every query of the tool; dates are converted only on screen (the grid's
column formatter), the stored values stay in the results and exports. Queries are saved by
name with the app settings and loaded back on any database with the same tables.
"""

import tkinter as tk
from tkinter import simpledialog, ttk

from browse_sources import ListSource
from combobox import SearchableCombobox
from constants import C
from engine import limits
from engine import querymodel as qm
from engine import timeline as tl
from dialogs import ValuesWindow
from grid import DataGrid, Runner
from tokens import COLOR as K, FONT as F
from widgets import FlowFrame, SearchBox, ToolTip, wrap_to_width

CARD_W = 236
ROW_H = 20
HEAD_H = 44                     # the coloured top edge, the table's name and what it holds
FOOT_H = 20
VISIBLE = 12                    # column lines a card shows at once; the wheel scrolls the rest
CARD_GAP = 96                   # room between cards for the join lines (tidy narrows it to
MIN_GAP = 60                    # fit the canvas, down to this)
PER_ROW = 3


def _short_path(path):
    path = path[5:] if path.startswith("root.") else path
    return path if len(path) <= 28 else "…" + path[-27:]
DEFAULT_LIMIT = 100000          # rows a new query returns at most (Limit; up to query_rows)
JOIN_WORDS = {"LEFT": "keep all", "INNER": "matches only"}


def read_fields(rows, fields):
    """rows with each field column's stored value replaced by the field read out of it
    (fields: (index, alias, column, paths)); a value is decoded once per row for all the
    fields of its column."""
    from engine.decode import fields as fl
    out = []
    for r in rows:
        r = list(r)
        trees = {}
        for i, a, col, paths in fields:
            key = (a, col)
            if key not in trees:
                trees[key] = fl.plain(r[i])
            tree = trees[key]
            r[i] = fl.read(r[i], paths, tree=tree)[0] if tree is not None else None
        out.append(r)
    return out


def apply_calcs(rows, calcs, where):
    """rows with each calculated column worked out (calcs: (position, steps), in column
    order, so a calculation can read a column worked out before it; where: heading ->
    position, for the steps that read another column of the row)."""
    from engine import calc
    out = []
    for r in rows:
        r = list(r)

        def look(name, r=r):
            k = where.get(name)
            return r[k] if k is not None and k < len(r) else None
        for p, steps in calcs:
            r[p] = calc.apply(r[p], steps, look)
        out.append(r)
    return out


def _key(v):
    """A value as a field join compares it: text, numbers without '.0', bytes as hex."""
    if v is None or v == "":
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    return str(v).strip()


def _table_rows(session, table, names, cap):
    """Rows of the named columns of a table (SQL when SQLite reads it, else natively)."""
    from engine.schema import quote_ident
    if getattr(session, "sql", None) is not None:
        cur = session.conn().execute("SELECT %s FROM %s" % (
            ", ".join(quote_ident(n) for n in names), quote_ident(table)))
        try:
            return cur.fetchmany(cap)
        finally:
            cur.close()
    all_names = list(session.info(table).column_names)
    at = [all_names.index(n) for n in names]
    out = []
    for r in session.iter_rows(table):
        out.append([r.values[i] if i < len(r.values) else None for i in at])
        if len(out) >= cap:
            break
    return out


def join_by_fields(cols, rows, fjoins, cols_model, in_sql, heads, pos, cap):
    """(columns, rows) of the results with the tables joined by a field matched in: each
    row's field (read out of the value the SQL carried in its '~key' column) looked up in the
    table's 'to' column. A row without a match is kept with empty cells; a row with several
    matches repeats once per match (as a LEFT JOIN does)."""
    import itertools
    from engine.decode import fields as fl
    base = len(cols) - len(fjoins)
    lookups = []
    for _alias, table, by, want, session in fjoins:
        need = [by["to"]] + [w for w in want if w != by["to"]]
        idx = {}
        for r in _table_rows(session, table, need, cap):
            k = _key(r[0])
            if k is not None:
                idx.setdefault(k, []).append(dict(zip(need, r)))
        lookups.append(idx)
    at = dict((a, n) for n, (a, _t, _b, _w, _s) in enumerate(fjoins))
    nsql = len(pos)
    out_cols = (list(heads) + cols[nsql:base]) if cols_model else cols[:base]
    out = []
    for r in rows:
        r = list(r)
        sqlpart, keys = r[:base], r[base:]
        choices = []
        for n, (_a, _t, by, _w, _s) in enumerate(fjoins):
            value = fl.read(keys[n], by["paths"])[0]
            choices.append(lookups[n].get(_key(value)) or [None])
        for combo in itertools.product(*choices):
            if not cols_model:
                out.append(list(sqlpart))
                continue
            row = []
            for i, c in enumerate(cols_model):
                if in_sql[i]:
                    row.append(sqlpart[pos[i]])
                else:
                    m = combo[at[c.alias]]
                    row.append(m.get(c.column) if m else None)
            out.append(row + sqlpart[nsql:])
        if len(out) > cap:
            break
    return out_cols, out


def _scrolled(box, tree):
    """Pack tree with a vertical scrollbar, so no line of a short list is out of reach."""
    ys = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    ys.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)


def _short(v, n=28):
    """A sample value for a tooltip: one line, cut."""
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "BLOB %d bytes" % len(v)
    t = str(v).replace("\n", " ")
    return t if len(t) <= n else t[:n - 1] + "\u2026"


class QueryTab(ttk.Frame):
    def __init__(self, parent, app):
        ttk.Frame.__init__(self, parent)
        self.app = app
        self.model = qm.QueryModel()
        self._pos = {}                  # alias -> (x, y) of its card
        self._items = {}                # canvas item -> ("col", alias, column) | ("join", i)
        self._drag = None
        self._rmap = None
        self._specs = {}                # (db, table) -> the card's columns (name, type, keys)
        self._scroll = {}               # alias -> first column line the card shows
        self._others = {}               # alias -> the trusted links not used when it was added
        self._looking = set()           # aliases whose links are being looked up
        self._run_when_linked = None    # Run pressed meanwhile: its confirm flag
        self._fonts = {}
        self._anchors, self._rowbg, self._hover = {}, {}, None
        self._tip_job = self._tip_for = None
        self._samples = {}              # (alias, column) -> [values] shown in the tooltip
        self._manual = False            # a card was moved by hand: no automatic layout
        self.runner = Runner(self, "query", release=getattr(app, "_release_worker_connection",
                                                            None))
        self._build()

    # -- layout ---------------------------------------------------------------------------------
    def _build(self):
        top = FlowFrame(self)
        top.pack(fill="x", padx=8, pady=(6, 2))
        self.run_btn = top.add(ttk.Button(top, text="Run", style="P.TButton", command=self.run))
        ToolTip(self.run_btn, "Run the query (Ctrl+Enter); the SQL below is exactly what runs")
        top.add(ttk.Button(top, text="Save…", command=self.save), gap=4)
        self.load_btn = top.add(ttk.Button(top, text="Load ▾", command=self._load_menu),
                                gap=4)
        top.add(ttk.Button(top, text="Clear", command=self.clear), gap=4)
        self.export_btn = top.add(ttk.Button(top, text="Export ▾",
                                             command=self._export_menu), gap=4)
        ToolTip(self.export_btn, "The results as the grid shows them (column filters and sort "
                                 "applied), with the query in the export's manifest")
        self.distinct_var = tk.BooleanVar(value=False)
        top.add(ttk.Checkbutton(top, text="Distinct rows", variable=self.distinct_var,
                                command=self._changed), gap=12)
        self.count_var = tk.BooleanVar(value=False)
        cnt = top.add(ttk.Checkbutton(top, text="Count per shown value",
                                      variable=self.count_var, command=self._changed), gap=8)
        ToolTip(cnt, "One line per distinct combination of the shown columns, with how many "
                     "rows have it (a 'rows' column: GROUP BY ... COUNT(*))")
        self.limit_on = tk.BooleanVar(value=False)
        chk = top.add(ttk.Checkbutton(top, text="At most", variable=self.limit_on,
                                      command=self._changed), gap=12)
        self.limit_var = tk.StringVar(value=str(DEFAULT_LIMIT))
        lim = self.limit_box = top.add(ttk.Spinbox(
            top, from_=1, to=limits.get("query_rows"), width=8, textvariable=self.limit_var,
            command=self._changed), gap=2)
        top.add(ttk.Label(top, text="rows"), gap=2)
        lim.bind("<FocusOut>", lambda e: self._changed())
        lim.bind("<Return>", lambda e: self._changed())
        tip = ("Off: every row the query finds (up to the query_rows setting, %s, to protect "
               "memory - the results say when that is reached). On: a LIMIT in the SQL." %
               format(limits.get("query_rows"), ","))
        ToolTip(chk, tip)
        ToolTip(lim, tip)
        top.add(ttk.Label(top, text="skip the first"), gap=10)
        self.offset_var = tk.StringVar(value="0")
        off = top.add(ttk.Spinbox(top, from_=0, to=10 ** 9, width=7,
                                  textvariable=self.offset_var, command=self._changed), gap=2)
        off.bind("<FocusOut>", lambda e: self._changed())
        off.bind("<Return>", lambda e: self._changed())
        ToolTip(off, "Rows left out before the first one shown (OFFSET): page through a "
                     "sorted result")
        self.status = wrap_to_width(ttk.Label(self, text="Add a table from the list on the "
                                              "left to start.", style="M.TLabel"))
        self.status.pack(fill="x", padx=8, pady=(0, 4))

        outer = ttk.PanedWindow(self, orient="horizontal")
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        left = ttk.Frame(outer)
        outer.add(left, weight=0)
        self.find = SearchBox(left, placeholder="Find a table…", delay=0, width=20,
                              find_button=False, on_change=lambda t: self._fill_tables())
        self.find.pack(fill="x")
        box = ttk.Frame(left)
        box.pack(fill="both", expand=True, pady=(2, 2))
        self.tables_tree = ttk.Treeview(box, columns=("rows",), show="tree headings",
                                        selectmode="browse", height=10)
        self.tables_tree.heading("#0", text="Table")
        self.tables_tree.heading("rows", text="Rows")
        self.tables_tree.column("#0", width=120, stretch=True)
        self.tables_tree.column("rows", width=52, stretch=False, anchor="e")
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tables_tree.yview)
        self.tables_tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tables_tree.pack(fill="both", expand=True)
        self.tables_tree.bind("<Double-1>", lambda e: self.add_selected())
        self.tables_tree.bind("<ButtonPress-1>", self._list_press, add="+")
        self.tables_tree.bind("<B1-Motion>", self._list_drag, add="+")
        self.tables_tree.bind("<ButtonRelease-1>", self._list_drop, add="+")
        self.tables_tree.bind("<Return>", lambda e: self.add_selected())
        ttk.Button(left, text="Add to the query ▸", command=self.add_selected).pack(fill="x")

        right = ttk.PanedWindow(outer, orient="vertical")
        outer.add(right, weight=1)
        cv_box = self._cv_box = ttk.Frame(right)
        right.add(cv_box, weight=3)
        bar = ttk.Frame(cv_box)
        bar.pack(fill="x", pady=(0, 2))
        tidy = ttk.Button(bar, text="Tidy layout", style="Small.TButton", command=self.tidy)
        tidy.pack(side="left")
        ToolTip(tidy, "Place the cards in columns along their joins")
        self.legend = ttk.Frame(bar)
        self.legend.pack(side="left")
        self.hint = wrap_to_width(ttk.Label(bar, style="M.TLabel", text="Tick columns to "
                                            "show them · right-click a column: join, condition, "
                                            "sort · ⋯ on a card: sample rows"))
        self.hint.pack(side="left", fill="x", expand=True, padx=(10, 0))
        what = ttk.Frame(cv_box, style="Card.TFrame")
        what.pack(fill="x", pady=(0, 4))
        # folded until asked: the gist of it is one line of the status; the full
        # explanation (joins, what repeats) opens on a click and stays as chosen
        head = ttk.Label(what, text="▸ What you'll get (click to show)",
                         style="CardHeading.TLabel", cursor="hand2")
        head.pack(anchor="w", padx=8, pady=(4, 4))
        self.summary = wrap_to_width(ttk.Label(what, text="", style="Card.TLabel",
                                               justify="left"))
        self._summary_open = bool(self.app.tags.settings.get("query_summary_open", False)) \
            if hasattr(self.app, "tags") else False

        def show(on):
            if on:
                self.summary.pack(fill="x", padx=8, pady=(0, 5))
                head.configure(text="▾ What you'll get")
            else:
                self.summary.pack_forget()
                head.configure(text="▸ What you'll get (click to show)")

        def fold(_e=None):
            self._summary_open = not self.summary.winfo_ismapped()
            show(self._summary_open)
            try:
                self.app.tags.settings["query_summary_open"] = self._summary_open
                self.app.tags.save_settings()
            except Exception:           # noqa: BLE001 - a choice not kept is not fatal
                pass
        head.bind("<Button-1>", fold)
        self.fold_summary = fold
        show(self._summary_open)
        self.cv = tk.Canvas(cv_box, bg=K["background"], highlightthickness=0, height=300)
        xs = ttk.Scrollbar(cv_box, orient="horizontal", command=self.cv.xview)
        ys = ttk.Scrollbar(cv_box, orient="vertical", command=self.cv.yview)
        self.cv.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.cv.bind("<Button-3>", self._context)
        self.cv.bind("<Motion>", self._hover_at)
        self.cv.bind("<Leave>", self._tip_hide)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>", self._wheel)
        self.cv.bind("<Button-5>", self._wheel)

        # the columns shown and the conditions share one notebook (each gets the full width)
        mid = self.mid = ttk.Notebook(right)
        right.add(mid, weight=2)
        cols_box = ttk.Frame(mid)
        mid.add(cols_box, text="  Columns shown  ")
        cols_list = ttk.Frame(cols_box)
        cols_list.pack(fill="both", expand=True)
        self.cols_tree = ttk.Treeview(cols_list, columns=("table", "column", "type", "agg",
                                                          "label", "show", "sort"),
                                      show="headings", height=4, selectmode="browse")
        for c, t, w in (("table", "Table", 110), ("column", "Column", 120), ("type", "Type", 80),
                        ("agg", "Summary", 100), ("label", "Label", 90),
                        ("show", "Shown as", 190), ("sort", "Sort", 80)):
            self.cols_tree.heading(c, text=t)
            self.cols_tree.column(c, width=w, minwidth=w if c == "show" else 40,
                                  stretch=c in ("column", "label"))
        _scrolled(cols_list, self.cols_tree)
        self.cols_tree.bind("<B1-Motion>", self._cols_drag)
        self.cols_tree.bind("<Button-3>", self._cols_context)
        cb = FlowFrame(cols_box)
        cb.pack(fill="x")
        for text, cmd in (("Add column…", self.add_column_dialog),
                          ("Remove", self._remove_column),
                          ("↑", lambda: self._move(-1)), ("↓", lambda: self._move(1)),
                          ("Sort ▾", self._sort_menu)):
            # the actions used most; the rest under More ▾ (and in the right-click menu)
            cb.add(ttk.Button(cb, text=text, style="Small.TButton", command=cmd), gap=2)
        more = ttk.Button(cb, text="More ▾", style="Small.TButton")
        more.configure(command=lambda: self._more_menu(more))
        cb.add(more, gap=8)
        ToolTip(more, "Label, summary (count, sum…), how a value is shown, meanings of codes, "
                      "a join by hand")
        cb.add(ttk.Label(cb, text="right-click a line for the same", style="M.TLabel"), gap=8)
        flt_box = ttk.Frame(mid)
        mid.add(flt_box, text="  Rows where  ")
        mrow = FlowFrame(flt_box)
        mrow.pack(fill="x", pady=(2, 0))
        mrow.add(ttk.Label(mrow, text="Keep a row when it matches"))
        self.match_var = tk.StringVar(value="all")
        mrow.add(ttk.Radiobutton(mrow, text="all the conditions (AND)", value="all",
                                 variable=self.match_var, command=self._changed), gap=6)
        mrow.add(ttk.Radiobutton(mrow, text="any condition (OR)", value="any",
                                 variable=self.match_var, command=self._changed), gap=6)
        flt_list = ttk.Frame(flt_box)
        flt_list.pack(fill="both", expand=True)
        self.flt_tree = ttk.Treeview(flt_list, columns=(), show="tree", height=4,
                                     selectmode="browse")
        _scrolled(flt_list, self.flt_tree)
        self.flt_tree.bind("<Double-1>", lambda e: self._edit_filter())
        self.flt_tree.bind("<Delete>", lambda e: self._remove_filter())
        fb = FlowFrame(flt_box)
        fb.pack(fill="x")
        fb.add(ttk.Button(fb, text="Add condition…", style="Small.TButton",
                          command=self._add_condition))
        g = fb.add(ttk.Button(fb, text="Add a group (any of)…", style="Small.TButton",
                              command=self._add_group), gap=2)
        ToolTip(g, "Conditions of which any one is enough - (A or B) - used as one condition "
                   "of the list")
        fb.add(ttk.Button(fb, text="Edit…", style="Small.TButton",
                          command=self._edit_filter), gap=2)
        fb.add(ttk.Button(fb, text="Remove", style="Small.TButton",
                          command=self._remove_filter), gap=2)

        low = mid                       # one notebook: what the query is, its SQL, its rows
        self.low = low
        self._right = right
        right.bind("<Configure>", self._first_split, add="+")
        sql_f = ttk.Frame(low)
        low.add(sql_f, text="  SQL (what runs)  ")
        self.sql_text = tk.Text(sql_f, height=6, wrap="none", font=F["mono"], relief="flat",
                                bg=K["card"], fg=K["text"], padx=6, pady=4)
        self.sql_text.pack(fill="both", expand=True)
        self.sql_text.configure(state="disabled")
        sb = FlowFrame(sql_f)
        sb.pack(fill="x")
        sb.add(ttk.Button(sb, text="Copy SQL", style="Small.TButton", command=self.copy_sql))
        sb.add(ttk.Button(sb, text="Open in the SQL tab", style="Small.TButton",
                          command=self.open_in_sql_tab), gap=2)
        res_f = ttk.Frame(low)
        low.add(res_f, text="  Results  ")
        self.res_tab = res_f
        low.insert(sql_f, res_f)        # the results first; the SQL, for the record, last
        rbar = FlowFrame(res_f)
        rbar.pack(fill="x", pady=(2, 2))
        self.export_btn2 = rbar.add(ttk.Button(rbar, text="Export results ▾",
                                               style="Small.TButton",
                                               command=lambda: self._export_menu(
                                                   self.export_btn2)))
        self.big_btn = rbar.add(ttk.Button(rbar, text="Bigger results ⤢", style="Small.TButton",
                                           command=self.toggle_big), gap=4)
        self.pics_var = tk.BooleanVar(value=False)
        rbar.add(ttk.Checkbutton(rbar, text="Show pictures", variable=self.pics_var,
                                 command=lambda: self.grid.set_pictures(self.pics_var.get())),
                 gap=8)
        ToolTip(self.big_btn, "Hide the tables above to give the results the whole height "
                              "(again to bring them back)")
        self.res_note = rbar.add(ttk.Label(rbar, text="Double-click a row for every value; "
                                           "right-click a BLOB to inspect it (pictures, plists, "
                                           "text)", style="M.TLabel"), gap=10)
        self.grid = DataGrid(res_f, frozen=0, on_open_row=self._open_row,
                             on_open_blob=lambda row, column, value: self.app.open_blob(
                                 value, column, "query result, %s — row %s" % (
                                     column, format(row + 1, ","))))
        self.grid.pack(fill="both", expand=True)
        self.bind_all("<Control-Return>", self._ctrl_enter, add="+")
        self._refresh_lists()

    def toggle_big(self):
        """The results take the whole height (the canvas hidden), or the canvas comes back."""
        panes = [str(x) for x in self._right.panes()]
        if str(self._cv_box) in panes:
            self._right.forget(self._cv_box)
            self.big_btn.configure(text="Show the tables again ⤡")
        else:
            self._right.insert(0, self._cv_box, weight=3)
            self.big_btn.configure(text="Bigger results ⤢")
            self.after_idle(lambda: self._right.sashpos(0, int(
                self._right.winfo_height() * 0.56)))

    def _open_row(self, _row, vals):
        """Every value of a result line in the row detail (BLOBs open in the inspector)."""
        if not vals:
            return None
        from dialogs import ValuesWindow
        return ValuesWindow(self.app, "Row detail — query result",
                            "Query result:\n%s" % self.shown_sql()[:300],
                            list(self.grid.columns()), list(vals))

    def _auto_dates(self):
        """A column whose name and values look like a date is shown as one when it is first
        ticked, again after being unticked (the stored value stays in copies and exports);
        a column set back to 'The stored value' by hand stays so."""
        from date_reading import column_kind
        kept = self.__dict__.setdefault("_stored_by_hand", set())
        for c in self.model.columns:
            t = self.model.table(c.alias)
            if c.show is not None or c.agg or c.paths or t is None or \
                    t.db == qm.RECOVERED or (c.alias, c.column) in kept or \
                    tl.name_hint(c.column) not in ("strong", "weak"):
                continue
            m = self._member(t.db)
            db = m.db if m is not None else self.db
            kind = column_kind(db, t.table, c.column)
            if kind is not None:
                c.show = kind

    def _first_split(self, e):
        """The canvas is the main area: about half the height, the columns list a band of
        about seven lines, the SQL / results the rest (once; the sashes stay where moved)."""
        if getattr(self, "_split_done", False) or e.height < 300:
            return
        self._split_done = True
        try:
            self._right.sashpos(0, int(e.height * 0.56))
        except tk.TclError:
            pass

    def _ctrl_enter(self, _e):
        try:
            if self.winfo_ismapped():
                self.run()
                return "break"
        except tk.TclError:
            pass
        return None

    # -- the database ---------------------------------------------------------------------------
    @property
    def db(self):
        return self.app.db

    def on_open(self):
        """A database became active (or the case changed): the tables listed; the query is
        kept while every database and table it uses is still open."""
        self._rmap = None
        self._specs = {}
        missing = [t.table for t in self.model.tables if t.table not in self._tables(t.db)]
        if missing:
            self.clear(quiet=True)
        self._fill_tables()
        self._redraw()

    def reset(self):
        self._rmap = None
        self._specs = {}
        self.clear(quiet=True)
        self._fill_tables()

    # In a case every database's tables are listed; a table of one is keyed by the
    # database's path (stable across sessions, so saved queries load back). Alone: None.
    def _members(self):
        case = getattr(self.app, "case", None)
        if case is None or not getattr(case, "multi", False):
            return []
        return [m for m in case if m.db.ok]

    def _member(self, db):
        if db == qm.RECOVERED:
            return None
        return next((m for m in self._members() if m.key == db), None) if db else None

    REC_COLUMNS = ("recovered_state", "recovered_confidence", "recovered_where")

    def _recovered(self):
        """{table: [Record]} the Forensics recovery found in the active database (rows of a
        table: no index entries), {} when it has not run."""
        fx = getattr(self.app, "_forensics", None)
        if fx is None or not getattr(fx, "_scanned", False) or getattr(fx, "db", None) is not \
                self.db:
            return {}
        out = {}
        for r in getattr(fx, "_records", None) or ():
            if r.table and getattr(r, "index", None) is None:
                out.setdefault(r.table, []).append(r)
        return out

    def _recovered_rows(self, table):
        """(columns, rows) of recovered_<table>: the live table's columns, then state,
        confidence and where each record was found."""
        from forensics_tab import record_state
        base = table[len("recovered_"):]
        cols = self._columns(base, None) if self.model.databases() else []
        cols = cols or list(self.db.session.info(base).column_names)
        rows = []
        try:
            info = self.db.session.info(base)
            alias = info.columns[info.rowid_alias].name if info.rowid_alias is not None else None
        except Exception:               # noqa: BLE001
            alias = None
        for r in self._recovered().get(base, ()):
            d = r.values_dict() if hasattr(r, "values_dict") else dict(zip(r.columns, r.values))
            if alias is not None and d.get(alias) is None and isinstance(r.rowid, int):
                d[alias] = r.rowid      # an INTEGER PRIMARY KEY is stored as NULL: its rowid
            rows.append([d.get(c) for c in cols] + [record_state(r), r.confidence,
                                                   r.prov.where()])
        return cols + list(self.REC_COLUMNS), rows

    def _session(self, db=None):
        if db == qm.RECOVERED:
            return self.db.session if self.db.ok and self._recovered() else None
        m = self._member(db)
        if db and m is None:
            return None
        return m.db.session if m is not None else (self.db.session if self.db.ok else None)

    def _db_name(self, db):
        if db == qm.RECOVERED:
            return "recovered"
        m = self._member(db)
        return m.name if m is not None else ""

    def _tables(self, db=None):
        if db == qm.RECOVERED:
            return ["recovered_" + t for t in sorted(self._recovered())]
        try:
            m = self._member(db)
            if db and m is None:
                return []
            d = m.db if m is not None else self.db
            return list(d.tables()) if d.ok else []
        except Exception:               # noqa: BLE001
            return []

    def _columns(self, table, db=None):
        if db == qm.RECOVERED:
            try:
                base = table[len("recovered_"):]
                return list(self.db.session.info(base).column_names) + list(self.REC_COLUMNS)
            except Exception:           # noqa: BLE001
                return []
        try:
            return list(self._session(db).info(table).column_names)
        except Exception:               # noqa: BLE001
            return []

    def _fill_tables(self):
        tree = self.tables_tree
        tree.delete(*tree.get_children())
        self._tree_map = {}
        term = self.find.var.get().strip().lower() if hasattr(self.find, "var") else ""
        members = self._members()
        groups = [(m, m.key, m.counts) for m in members] if members else \
            [(None, None, getattr(self.app, "_count_cache", None) or {})]
        for k, (m, db, counts) in enumerate(groups):
            parent = ""
            if m is not None:
                parent = "g%d" % k
                tree.insert("", "end", iid=parent, text=m.name, open=True)
            for i, t in enumerate(self._tables(db)):
                if term and term not in t.lower():
                    continue
                n = counts.get(t)
                iid = t if m is None else "%s:%d" % (parent, i)
                self._tree_map[iid] = (t, db)
                if isinstance(n, int):
                    shown = format(n, ",")
                elif isinstance(n, str) and n.startswith("~") and n[1:].isdigit():
                    from navigator import short_count       # '~1.1k', as the navigator
                    shown = "~" + short_count(int(n[1:]))
                else:
                    shown = n or ""
                tree.insert(parent, "end", iid=iid, text=t, values=(shown,))
        rec = self._recovered()
        if rec:
            tree.insert("", "end", iid="grec", text="Recovered rows (Forensics)", open=True)
            for i, t in enumerate(sorted(rec)):
                name = "recovered_" + t
                if term and term not in name.lower():
                    continue
                iid = "grec:%d" % i
                self._tree_map[iid] = (name, qm.RECOVERED)
                tree.insert("grec", "end", iid=iid, text=name,
                            values=(format(len(rec[t]), ","),))

    # -- tables ---------------------------------------------------------------------------------
    def add_selected(self):
        sel = self.tables_tree.selection()
        if sel and sel[0] in getattr(self, "_tree_map", {}):
            self.add_table(*self._tree_map[sel[0]])

    # a table dragged from the list and dropped on the canvas lands where it was dropped
    def _list_press(self, e):
        iid = self.tables_tree.identify_row(e.y)
        self._list_drag_from = (iid, e.x_root, e.y_root) if iid in getattr(
            self, "_tree_map", {}) else None
        self._list_dragging = False

    def _list_drag(self, e):
        start = getattr(self, "_list_drag_from", None)
        if start and not self._list_dragging and abs(e.x_root - start[1]) + abs(
                e.y_root - start[2]) > 8:
            self._list_dragging = True
            self.tables_tree.configure(cursor="plus")

    def _list_drop(self, e):
        start = getattr(self, "_list_drag_from", None)
        dragging = getattr(self, "_list_dragging", False)
        self._list_drag_from, self._list_dragging = None, False
        self.tables_tree.configure(cursor="")
        if not (start and dragging):
            return
        if self.winfo_containing(e.x_root, e.y_root) is not self.cv:
            return
        alias = self.add_table(*self._tree_map[start[0]])
        if alias is not None:
            x = self.cv.canvasx(e.x_root - self.cv.winfo_rootx()) - CARD_W / 2
            y = self.cv.canvasy(e.y_root - self.cv.winfo_rooty()) - HEAD_H / 2
            self._pos[alias] = (max(0, x), max(0, y))
            self._manual = True
            self._redraw()

    def add_table(self, table, db=None):
        """Add a table (db: its database's path in a case); joins to the tables already there
        are looked up on the worker: trusted links inside a database, and the trusted value
        links Relationships found between databases."""
        if table not in self._tables(db) or self._session(db) is None:
            return None
        first = not self.model.tables
        alias = self.model.add_table(table, db=db)
        n = len(self.model.tables) - 1
        self._pos[alias] = (16 + (n % PER_ROW) * (CARD_W + CARD_GAP), 12 + (n // PER_ROW) * 220)
        if first:
            for c in self._columns(table, db)[:6]:
                self.model.toggle_column(alias, c)
        self._changed()
        if first:
            n = len(self._columns(table, db))
            self.status.configure(text="%s added with its first %d of %d columns ticked: tick "
                                       "the ones you want on its card, add more tables to "
                                       "join, then Run." % (table, min(6, n), n))
        if not first:
            self.status.configure(text="Looking for the links between %s and the other "
                                       "tables…" % table)
            session = self._session(db)
            live_db = db
            look = table
            if db == qm.RECOVERED:
                # the recovered records join like the live rows of their table
                live_db = next((t.db for t in self.model.tables if t.db != qm.RECOVERED), None)
                look = table[len("recovered_"):]
            model_tables = [(t.alias, t.table) for t in self.model.tables
                            if t.alias != alias and t.db == live_db]
            cross = self._cross_joins(alias, table, db) if db != qm.RECOVERED else []
            # tables of other databases with no link known yet (Relationships not run):
            # their values are compared now, these tables only
            others = {}
            if not cross and db not in (None, qm.RECOVERED):
                for t in self.model.tables:
                    if t.alias != alias and t.db not in (db, None, qm.RECOVERED) and \
                            not t.by and self._session(t.db) is not None:
                        others.setdefault(t.db, []).append((t.alias, t.table))

            def work():
                from engine.relations import relation_map
                joins = list(cross)
                if others:
                    joins.extend(self._value_joins(alias, table, db, session, others))
                if model_tables:
                    rmap = relation_map(session)
                    rmap.build()
                    tmp = qm.QueryModel()
                    for a, t in model_tables:
                        tmp.tables.append(qm.TableRef(a, t))
                    joins = qm.suggest_joins(rmap, tmp, look) + joins
                return joins

            def done(joins, error):
                self._looking.discard(alias)
                if alias not in self._pos:
                    self._run_if_waiting()
                    return
                joins = joins or []
                for j in joins:
                    j.left = alias
                # one link ties the new table in: the likeliest (suggest_joins ranks them);
                # every link at once (ANDed) would match almost nothing
                if joins:
                    self.model.joins.append(joins[0])
                self._others[alias] = joins[1:]
                self._changed()
                if not self._manual:
                    self.tidy()
                self._say_joins(alias, joins[:1], error, joins[1:])
                self._run_if_waiting()
            self._looking.add(alias)
            self._changed()
            self.runner.submit("joins-" + alias, work, done)
        return alias

    def _value_joins(self, alias, table, db, session, others):
        """(worker) Joins of the new table to tables of other databases found by comparing
        their values (engine.crossdb, only these tables): trusted links only, best first."""
        from engine import crossdb
        dbs = [(db, session)] + [(k, self._session(k)) for k in others]
        want = dict((k, set(t for _a, t in v)) for k, v in others.items())
        want[db] = {table}
        res = crossdb.find_links(dbs, tables=want)
        out = []
        for link in res.confident():
            for (d1, t1, c1), (d2, t2, c2) in (link.ends(), link.ends()[::-1]):
                if d1 != db or t1 != table:
                    continue
                for a, t in others.get(d2, ()):
                    if t == t2:
                        out.append(qm.Join(alias, c1, a, c2, "LEFT",
                                           "matched by value between databases (%d%% of "
                                           "sampled values found)" % round(100 * link.fraction)))
        return out

    def _run_if_waiting(self):
        """Run pressed while links were being looked up: run now that they are in."""
        if self._looking or self._run_when_linked is None:
            return
        confirm, self._run_when_linked = self._run_when_linked, None
        self.run(confirm=confirm)

    def _cross_joins(self, alias, table, db):
        """Joins from the trusted links between databases (matched by value) that tie the
        new table to tables of other databases already in the query."""
        rel = getattr(self.app, "relations", None)
        if db is None or rel is None or not hasattr(rel, "cross_links"):
            return []
        by_uid = dict((m.uid, m.key) for m in self._members())
        mine = next((m.uid for m in self._members() if m.key == db), None)
        out = []
        for link in rel.cross_links():
            for (d1, t1, c1), (d2, t2, c2) in (link.ends(), link.ends()[::-1]):
                if d1 != mine or t1 != table:
                    continue
                other = next((t for t in self.model.tables if t.db == by_uid.get(d2) and
                              t.table == t2 and t.alias != alias), None)
                if other is not None:
                    out.append(qm.Join(alias, c1, other.alias, c2, "LEFT",
                                       "matched by value between databases (%d%% of sampled "
                                       "values found)" % round(100 * link.fraction)))
        return out

    def _say_joins(self, alias, joins, error, others=()):
        t = self.model.table(alias)
        if error is not None:
            self.status.configure(text="The links of %s could not be read: %s" % (t.table, error))
        elif joins:
            j = joins[0]
            more = ("  ·  %d other link%s found: right-click the line to use one instead" % (
                len(others), "" if len(others) == 1 else "s")) if others else ""
            self.status.configure(text="%s joined: %s (%s)%s" % (t.table, j.text(), j.why, more))
        else:
            self.status.configure(text="No trusted link joins %s to the other tables: "
                                       "right-click one of its columns to join it by hand "
                                       "(until then every row meets every row)." % t.table)

    def remove_table(self, alias):
        self.model.remove_table(alias)
        self._pos.pop(alias, None)
        self._others.pop(alias, None)
        self._looking.discard(alias)
        self._changed()

    def clear(self, quiet=False):
        self.model = qm.QueryModel()
        self._pos = {}
        self._scroll = {}
        self._others = {}
        self._looking = set()
        self._run_when_linked = None
        self._manual = False
        self.grid.set_source(None)
        if not quiet:
            self.status.configure(text="Add a table from the list on the left to start.")
        self._changed()

    # -- the canvas -----------------------------------------------------------------------------
    def _spec(self, t):
        """[(name, declared type, key, fk, unique)] of a card's columns, cached per database and
        table. key: 'PK' (in the primary key, or the rowid alias), 'UQ' (unique by the schema)
        or ''; fk: 'table.column' a declared foreign key points at, or ''; unique: the schema
        makes the column's values unique (a join's '1' end)."""
        k = (t.db, t.table)
        hit = self._specs.get(k)
        if hit is not None:
            return hit
        rec = t.db == qm.RECOVERED
        base = t.table[len("recovered_"):] if rec else t.table
        types, keys, uniq, fks = {}, {}, set(), {}
        try:
            from engine.erd import declared_fks, table_specs
            s = self._session(None if rec else t.db)
            spec = table_specs(s, [base]).get(base)
            if spec is not None:
                for name, typ, pk in spec.columns:
                    types[name] = typ
                    if pk:
                        keys[name] = "PK"
                    elif name.lower() in spec.unique:
                        keys[name] = "UQ"
                    if name.lower() in spec.unique:
                        uniq.add(name)
            for low, (pt, pc) in declared_fks(s, base).items():
                fks[low] = "%s.%s" % (pt, pc or "rowid")
        except Exception:               # noqa: BLE001 - names only, as the list has them
            pass
        out = []
        for n in self._columns(t.table, t.db):
            if rec and n in self.REC_COLUMNS:
                out.append((n, "recovery", "", "", False))
            else:
                out.append((n, types.get(n, ""), keys.get(n, ""), fks.get(n.lower(), ""),
                            n in uniq))
        self._specs[k] = out
        return out

    def _rows_of(self, t):
        """The table's row count as the lists show it (an int, '~n' while estimated), or None."""
        if t.db == qm.RECOVERED:
            return len(self._recovered().get(t.table[len("recovered_"):], ()))
        m = self._member(t.db)
        counts = m.counts if m is not None else (getattr(self.app, "_count_cache", None) or {})
        return counts.get(t.table)

    def _exact_rows(self, alias):
        """The table's row count when it is exact (an estimate, '~n', is not said in the
        sentence that explains the result)."""
        n = self._rows_of(self.model.table(alias))
        return n if isinstance(n, int) else None

    def _db_color(self, db):
        if db == qm.RECOVERED:
            return K["accent"]
        m = self._member(db)
        return m.color if m is not None and getattr(m, "color", None) else K["primary"]

    def _card_h(self, t):
        n = len(self._spec(t))
        return HEAD_H + min(n, VISIBLE) * ROW_H + (FOOT_H if n > VISIBLE else 6)

    def _font(self, font):
        f = self._fonts.get(font)
        if f is None:
            from tkinter import font as tkfont
            f = self._fonts[font] = tkfont.Font(font=F[font])
        return f

    def _fonts_measure(self, font, text):
        return self._font(font).measure(text)

    def _fit(self, text, font, width):
        """text shortened in the middle to fit width pixels (the full text is in the hover
        card)."""
        f = self._font(font)
        if f.measure(text) <= width:
            return text
        keep = len(text)
        while keep > 3 and f.measure(text[:keep // 2] + "…" + text[-(keep - keep // 2) + 1:]) > \
                width:
            keep -= 1
        return text[:keep // 2] + "…" + text[-(keep - keep // 2) + 1:]

    def _redraw(self):
        cv = self.cv
        cv.delete("all")
        cv.configure(bg=K["background"])
        self._items, self._anchors, self._rowbg, self._hover = {}, {}, {}, None
        self._lone = set(self.model.unjoined()) - self._looking
        self._update_legend()
        if not self.model.tables:
            cv.create_text(28, 28, anchor="nw", width=560, font=F["body"], fill=K["muted_text"],
                           text="Double-click a table on the left to put it here. Tables added "
                                "next are joined to it by the links the Relationships engine "
                                "trusts; tick the columns to show, then Run.")
            cv.configure(scrollregion=(0, 0, 600, 200))
            return
        joined = set()
        for j in self.model.joins:
            joined.update(((j.left, j.left_col), (j.right, j.right_col)))
        many = len(self.model.databases()) > 1 or bool(self._members())
        for t in self.model.tables:
            self._draw_card(t, joined, many)
        for i, j in enumerate(self.model.joins):
            self._draw_join(i, j)
        for t in self.model.by_field():
            b = t.by
            self._draw_join(None, qm.Join(b["alias"], b["column"], t.alias, b["to"], "LEFT",
                                          "field"), field=t)
        cv.tag_lower("joinline")        # the lines pass under the cards, the kinds sit on top
        cv.tag_raise("pill")
        bbox = cv.bbox("all")
        if bbox:
            cv.configure(scrollregion=(0, 0, bbox[2] + 60, bbox[3] + 60))

    def _draw_card(self, t, joined, many):
        cv, a = self.cv, t.alias
        lone = a in self._lone
        x, y = self._pos.get(a, (16, 16))
        spec = self._spec(t)
        n = len(spec)
        top = max(0, min(self._scroll.get(a, 0), n - VISIBLE)) if n > VISIBLE else 0
        self._scroll[a] = top
        h = self._card_h(t)
        w = CARD_W
        color = self._db_color(t.db)
        body = cv.create_rectangle(x, y, x + w, y + h, fill=K["card"],
                                   outline=K["accent"] if lone else K["border"],
                                   width=2 if lone else 1, dash=(5, 3) if lone else ())
        self._items[body] = ("body", a)
        cv.create_rectangle(x, y, x + w, y + 4, fill=color, outline=color)
        head = cv.create_rectangle(x + 1, y + 4, x + w - 1, y + HEAD_H, fill=K["card"],
                                   outline="")
        self._items[head] = ("head", a)
        title = t.table if t.alias == t.table else "%s (%s)" % (t.table, t.alias)
        if title != t.table and self._fit(title, "body_bold", w - 62) != title:
            title = t.table     # no room for both: the table's name, not a cut alias
        it = cv.create_text(x + 10, y + 16, anchor="w", font=F["body_bold"], fill=K["text"],
                            text=self._fit(title, "body_bold", w - 62))
        self._items[it] = ("head", a)
        rows = self._rows_of(t)
        sub = []
        if many:
            sub.append(self._db_name(t.db) or "this database")
        # rows: exact '669,675', estimated '~669,675'; not counted yet ('?'): left out
        est = isinstance(rows, str) and rows.startswith("~") and rows[1:].isdigit()
        num = rows if isinstance(rows, int) else int(rows[1:]) if est else None
        at = len(sub)
        if num is not None:
            sub.append("%s%s rows" % ("~" if est else "", format(num, ",")))
        sub.append("%d columns" % n)
        if lone:
            sub.insert(0, "NOT JOINED")
            at += 1
        elif a in self._looking:
            sub.insert(0, "finding links…")
            at += 1
        sx = x + 10
        if many:
            cv.create_oval(sx, y + 29, sx + 8, y + 37, fill=color, outline=color)
            sx += 13
        room = x + w - 10 - sx
        if num is not None and self._fonts_measure("small", " · ".join(sub)) > room:
            from navigator import short_count  # 669,675 -> 670k: the line fits uncut
            sub[at] = "%s%s rows" % ("~" if est else "", short_count(num))
        if lone or a in self._looking:  # a status ('finding links…') matters more than counts
            while len(sub) > 1 and self._fonts_measure("small", " · ".join(sub)) > room:
                del sub[-1]
        it = cv.create_text(sx, y + 33, anchor="w", font=F["small"], fill=K["muted_text"],
                            text=self._fit(" · ".join(sub), "small", room))
        self._items[it] = ("head", a)
        for glyph, kind, dx in (("⋯", "menu", 32), ("×", "close", 13)):
            it = cv.create_text(x + w - dx, y + 16, text=glyph, font=F["heading"],
                                fill=K["muted_text"])
            self._items[it] = (kind, a)
        cv.create_line(x + 1, y + HEAD_H - 1, x + w - 1, y + HEAD_H - 1, fill=K["border"])
        room = w - (8 if n > VISIBLE else 0)    # the scroll track takes the right edge
        for i, (name, typ, key, fk, _u) in enumerate(spec[top:top + VISIBLE]):
            cy = y + HEAD_H + i * ROW_H
            mid = cy + ROW_H / 2
            on = self.model.has_column(a, name)
            hit = ("col", a, name)
            fill = K["chosen"] if on else K["card"]
            bg = cv.create_rectangle(x + 1, cy, x + room - 1, cy + ROW_H, fill=fill, outline="")
            self._rowbg[(a, name)] = (bg, fill)
            self._items[bg] = hit
            box = cv.create_rectangle(x + 10, mid - 6, x + 22, mid + 6, outline=K["primary"] if on
                                      else K["button_border"], fill=K["primary"] if on
                                      else K["card"])
            self._items[box] = hit
            if on:
                self._items[cv.create_text(x + 16, mid, text="✓", font=F["tiny_bold"],
                                           fill=K["on_primary"])] = hit
            badge = "PK" if key == "PK" else ("FK" if fk else key)
            if badge:
                self._items[cv.create_text(x + 28, mid, anchor="w", text=badge,
                                           font=F["tiny_bold"], fill={
                                               "PK": K["accent"], "FK": K["purple"],
                                               "UQ": K["teal"]}[badge])] = hit
            tname = (typ or "any").upper() if typ != "recovery" else "recovery"
            tname = self._fit(tname, "small", 84)
            tw = self._fonts["small"].measure(tname)
            self._items[cv.create_text(x + room - 10, mid, anchor="e", text=tname,
                                       font=F["small"], fill=K["muted_text"])] = hit
            if self._column_filter(a, name)[0] is not None:
                self._items[cv.create_text(x + room - 16 - tw, mid, anchor="e", text="▼",
                                           font=F["tiny_bold"], fill=K["accent"])] = hit
                tw += 12
            bold = (a, name) in joined
            font = "small_bold" if bold else "small"
            self._items[cv.create_text(x + 50, mid, anchor="w", font=F[font],
                                       fill=K["primary"] if bold else K["text"],
                                       text=self._fit(name, font, room - 50 - tw - 18))] = hit
            self._anchors[(a, name)] = (x, x + w, mid)
        if n > VISIBLE:
            t0, t1 = y + HEAD_H + 2, y + HEAD_H + VISIBLE * ROW_H - 2
            cv.create_rectangle(x + w - 6, t0, x + w - 3, t1, fill=K["muted"], outline="")
            span = t1 - t0
            ya = t0 + span * top / n
            yb = t0 + span * min(n, top + VISIBLE) / n
            cv.create_rectangle(x + w - 7, ya, x + w - 2, yb, fill=K["button_border"], outline="")
            it = cv.create_text(x + 10, y + h - FOOT_H / 2, anchor="w", font=F["small"],
                                fill=K["primary"], text="%d–%d of %d columns · choose…" % (
                                    top + 1, min(n, top + VISIBLE), n))
            self._items[it] = ("more", a)

    def _unique(self, alias, column):
        t = self.model.table(alias)
        if t is None:
            return False
        if column == qm.ROWID:
            return True
        return any(c[0] == column and c[4] for c in self._spec(t))

    def _draw_join(self, i, j, field=None):
        cv = self.cv
        a = self._anchors.get((j.left, j.left_col)) or self._card_mid(j.left)
        b = self._anchors.get((j.right, j.right_col)) or self._card_mid(j.right)
        if a is None or b is None:
            return
        if a[1] + 24 <= b[0]:
            ax, bx, da, db = a[1], b[0], 1, -1
        elif b[1] + 24 <= a[0]:
            ax, bx, da, db = a[0], b[1], -1, 1
        else:                           # cards above each other: out of the right and back
            ax, bx, da, db = a[1], b[1], 1, 1
        ay, by = a[2], b[2]
        k = max(36, abs(bx - ax) / 2.5) if da != db else 46 + abs(ay - by) / 8
        pts = (ax, ay, ax + da * k, ay, bx + db * k, by, bx, by)
        lt, rt = self.model.table(j.left), self.model.table(j.right)
        cross = lt is not None and rt is not None and lt.db != rt.db
        color = K["purple"] if field is not None else K["accent"] if cross else K["primary"]
        hit = ("fjoin", field.alias) if field is not None else ("join", i)
        line = cv.create_line(*pts, smooth="raw", splinesteps=24, width=2, fill=color,
                              dash=(2, 3) if field is not None else () if j.kind == "INNER"
                              else (6, 4), tags=("joinline",))
        self._items[line] = hit
        for (ex, ey, d), (alias, col) in (((ax, ay, da), (j.left, j.left_col)),
                                          ((bx, by, db), (j.right, j.right_col))):
            cv.create_oval(ex - 3, ey - 3, ex + 3, ey + 3, fill=color, outline=color,
                           tags=("pill",))
            self._items[cv.create_text(ex + d * 10, ey - 8, text="1" if self._unique(
                alias, col) else "N", font=F["tiny_bold"], fill=color, tags=("pill",))] = hit
        mx = (pts[0] + 3 * pts[2] + 3 * pts[4] + pts[6]) / 8
        my = (pts[1] + 3 * pts[3] + 3 * pts[5] + pts[7]) / 8
        label = ("by field " + _short_path(field.by["paths"][0])) if field is not None \
            else JOIN_WORDS[j.kind] + (" · across databases" if cross else "")
        tag = cv.create_text(mx, my, text=label, font=F["tiny_bold"], fill=color,
                             tags=("pill", "pilltext"))
        x0, y0, x1, y1 = cv.bbox(tag)
        pill = cv.create_rectangle(x0 - 5, y0 - 2, x1 + 5, y1 + 2, fill=K["card"], outline=color,
                                   tags=("pill",))
        cv.tag_raise(tag, pill)
        self._items[tag] = hit
        self._items[pill] = hit

    def card_title(self, alias):
        """'table', 'table (alias)' for a second copy, and 'db › table' in a case."""
        t = self.model.table(alias)
        label = t.table if t.alias == t.table else "%s (%s)" % (t.table, t.alias)
        if t.db is not None:
            label = "%s › %s" % (self._db_name(t.db), label)
        return label

    def _card_mid(self, alias):
        p = self._pos.get(alias)
        return None if p is None else (p[0], p[0] + CARD_W, p[1] + HEAD_H / 2)

    def _update_legend(self):
        """One chip per database the query reads, in its colour (only across databases)."""
        dbs = self.model.databases()
        key = tuple(dbs) if len(dbs) > 1 else ()
        if key == getattr(self, "_legend_key", None):
            return
        self._legend_key = key
        for w in self.legend.winfo_children():
            w.destroy()
        if not key:
            self.hint.pack(side="left", fill="x", expand=True, padx=(10, 0))
            return
        self.hint.pack_forget()
        for db in dbs:
            tk.Label(self.legend, text="●", fg=self._db_color(db), bg=K["background"],
                     font=F["body"]).pack(side="left", padx=(10, 2))
            ttk.Label(self.legend, text=self._db_name(db) or "this database",
                      style="M.TLabel").pack(side="left")
        ttk.Label(self.legend, text="· amber lines join two databases",
                  style="M.TLabel").pack(side="left", padx=(10, 0))

    def tidy(self):
        """Cards in columns along the joins, as the keys point: a table left of the tables it
        refers to (the join's 'N' end left of its '1' end), so a hub sits in the middle with
        the tables referring to it on its left and the ones it refers to on its right; ends
        that tell no direction follow the order the tables were added. Within a column the
        cards are ordered next to the cards they join; tables no join reaches come last."""
        ts = self.model.tables
        if not ts:
            return
        order = dict((t.alias, i) for i, t in enumerate(ts))
        edges = [(t.by["alias"], t.alias) for t in self.model.by_field()
                 if t.by["alias"] in order]  # a field's table right of the table holding it
        for j in self.model.joins:
            if j.left not in order or j.right not in order or j.left == j.right:
                continue
            ul, ur = self._unique(j.left, j.left_col), self._unique(j.right, j.right_col)
            if ur and not ul:
                edges.append((j.left, j.right))
            elif ul and not ur:
                edges.append((j.right, j.left))
            else:
                edges.append(tuple(sorted((j.left, j.right), key=order.get)))
        linked = set(a for e in edges for a in e) | {ts[0].alias}
        layer = dict((a, 0) for a in linked)
        for _ in range(len(ts)):        # longest path; a cycle stops growing after n rounds
            moved = False
            for u, v in edges:
                if layer[v] < layer[u] + 1 <= len(ts):
                    layer[v] = layer[u] + 1
                    moved = True
            if not moved:
                break
        last = max(layer.values()) + 1
        layers = {}
        for t in ts:
            layers.setdefault(layer.get(t.alias, last), []).append(t)
        ys = {}
        n = len(layers)
        width = self.cv.winfo_width()
        gap = CARD_GAP if n < 2 or width < 100 else \
            max(MIN_GAP, min(CARD_GAP, (width - 32 - n * CARD_W) // (n - 1)))
        for d in sorted(layers):
            def near(t):
                around = [ys[o] for u, v in edges for s_, o in ((u, v), (v, u))
                          if s_ == t.alias and o in ys]
                return (sum(around) / len(around)) if around else order[t.alias] * 1000
            y = 16
            for t in sorted(layers[d], key=near):
                self._pos[t.alias] = (16 + d * (CARD_W + gap), y)
                ys[t.alias] = y + HEAD_H
                y += self._card_h(t) + 28
        self._manual = False
        self._redraw()
        self.cv.xview_moveto(0)
        self.cv.yview_moveto(0)

    def _hit(self, e):
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        for it in reversed(self.cv.find_overlapping(x - 2, y - 2, x + 2, y + 2)):
            if it in self._items:
                return self._items[it], x, y
        return None, x, y

    def _press(self, e):
        self._tip_hide()
        hit, x, y = self._hit(e)
        if hit is None:
            return
        if hit[0] == "col":
            self.model.toggle_column(hit[1], hit[2])
            self._changed()
        elif hit[0] == "close":
            self.remove_table(hit[1])
        elif hit[0] == "menu":
            self._card_menu(e, hit[1])
        elif hit[0] == "head":
            self._drag = (hit[1], x, y)
        elif hit[0] == "more":
            self._more_columns(hit[1])
        elif hit[0] == "join":
            self._join_menu(e, hit[1])
        elif hit[0] == "fjoin":
            self._fjoin_menu(e, hit[1])

    def _fjoin_menu(self, e, alias):
        t = self.model.table(alias)
        if t is None:
            return
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="%s.%s = field %s inside %s.%s" % (
            t.table, t.by["to"], " / ".join(t.by["paths"]), t.by["alias"], t.by["column"]),
            state="disabled")
        m.add_separator()
        m.add_command(label="Remove %s (joined on this field)" % t.table,
                      command=lambda: self.remove_table(alias))
        m.tk_popup(e.x_root, e.y_root)

    def _motion(self, e):
        if not self._drag:
            return
        alias, x0, y0 = self._drag
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        px, py = self._pos.get(alias, (0, 0))
        self._pos[alias] = (max(0, px + x - x0), max(0, py + y - y0))
        self._drag = (alias, x, y)
        self._manual = True
        self._redraw()

    def _wheel(self, e):
        """Over a card with more columns than it shows: scroll its columns; else the canvas."""
        delta = getattr(e, "delta", 0)
        step = (-1 if delta > 0 else 1) if delta else (-1 if getattr(e, "num", 5) == 4 else 1)
        hit, _x, _y = self._hit(e)
        if hit is not None and hit[0] in ("col", "body", "more", "head"):
            t = self.model.table(hit[1])
            if t is not None and len(self._spec(t)) > VISIBLE:
                self._scroll[t.alias] = self._scroll.get(t.alias, 0) + 3 * step
                self._redraw()
                return "break"
        if e.state & 1:
            self.cv.xview_scroll(step, "units")
        else:
            self.cv.yview_scroll(step, "units")
        return "break"

    # the hover: the row lights up, and after a moment a card tells what the item is
    def _hover_at(self, e):
        if self._drag:
            return
        hit, x, y = self._hit(e)
        key = (hit[1], hit[2]) if hit and hit[0] == "col" else None
        if key != self._hover:
            if self._hover in self._rowbg:
                item, fill = self._rowbg[self._hover]
                self.cv.itemconfigure(item, fill=fill)
            if key in self._rowbg:
                self.cv.itemconfigure(self._rowbg[key][0], fill=K["hover"])
            self._hover = key
        self.cv.configure(cursor="fleur" if hit and hit[0] == "head" else (
            "hand2" if hit and hit[0] in ("col", "close", "menu", "more", "join", "fjoin")
            else ""))
        if hit != self._tip_for:
            self._tip_hide()
            self._tip_for = hit
            if hit is not None:
                self._tip_job = self.after(450, lambda: self._tip_show(hit, x, y))

    def _tip_text(self, hit):
        if hit[0] == "col":
            t = self.model.table(hit[1])
            row = next((c for c in self._spec(t) if c[0] == hit[2]), None) if t else None
            if row is None:
                return None
            name, typ, key, fk, uniq = row
            lines = ["%s.%s" % (t.table, name), "declared type: %s" % (
                typ or "none (any value)") if typ != "recovery" else
                     "added by the recovery: where and how surely the record was found"]
            if key == "PK":
                lines.append("primary key" + (" (unique)" if uniq else " (part of it)"))
            elif key == "UQ":
                lines.append("unique by the schema")
            if fk:
                lines.append("foreign key → %s" % fk)
            sample = self._samples.get((hit[1], hit[2]))
            if sample is None:
                self._fetch_samples(hit[1], hit[2])
                lines.append("sample values: reading…")
            elif sample:
                lines.append("sample values: " + " · ".join(_short(v) for v in sample))
            else:
                lines.append("sample values: none (the table has no rows)")
            lines.append("click: show or hide · right-click: join, condition, sort")
            return "\n".join(lines)
        if hit[0] == "join" and hit[1] < len(self.model.joins):
            j = self.model.joins[hit[1]]
            ends = "%s to %s" % ("one" if self._unique(j.left, j.left_col) else "many",
                                 "one" if self._unique(j.right, j.right_col) else "many")
            return "%s\n%s (%s JOIN) · %s · %s\nclick: keep all / matches only / remove" % (
                j.text(), JOIN_WORDS[j.kind], j.kind, ends, j.why or "joined by hand")
        if hit[0] == "fjoin":
            t = self.model.table(hit[1])
            return "%s.%s = the field %s inside %s.%s\nmatched by the tool after the query " \
                   "(SQLite cannot read inside the values) · rows without a match kept\n" \
                   "click: remove" % (t.table, t.by["to"], ", else ".join(t.by["paths"]),
                                      t.by["alias"], t.by["column"]) if t else None
        if hit[0] == "head":
            return "%s\ndrag to move · ⋯ for the columns and sample rows · × removes it" % \
                self.card_title(hit[1])
        if hit[0] == "more":
            return "The wheel scrolls the columns; click to choose them from a list"
        return None

    SAMPLE_VALUES = 5               # values a column's tooltip shows
    SAMPLE_ROWS = 25                # rows 'Show sample rows…' lists

    def _fetch_samples(self, alias, col):
        """Read a few distinct stored values of alias.column on the worker; the tooltip is
        drawn again when they arrive (if it still shows that column)."""
        key = (alias, col)
        self._samples[key] = None

        def work():
            seen, out = set(), []
            for v in self._sample_values(alias, col, 200):
                k = repr(v)
                if v is None or k in seen:
                    continue
                seen.add(k)
                out.append(v)
                if len(out) >= self.SAMPLE_VALUES:
                    break
            return out

        def done(result, error):
            self._samples[key] = [] if error is not None else list(result or [])
            if self._tip_for == ("col",) + key and self.cv.find_withtag("tip"):
                x0, y0 = self._tip_at
                self.cv.delete("tip")
                self._tip_show(self._tip_for, x0, y0)
        self.runner.submit("samples-%s-%s" % key, work, done)

    def sample_rows(self, alias):
        """A window with the first rows of a table of the query (its stored values), so the
        data can be seen before the query is built."""
        t = self.model.table(alias)
        if t is None:
            return None
        from browse_sources import ListSource
        from database import RID
        from widgets import fit_geometry, place_over
        win = tk.Toplevel(self)
        win.title("Sample rows — %s (the first %d)" % (t.table, self.SAMPLE_ROWS))
        fit_geometry(win, 900, 420)
        place_over(win, self)
        win.configure(bg=K["background"])
        note = ttk.Label(win, text="Reading the first %d rows of %s…" % (self.SAMPLE_ROWS,
                                                                        t.table),
                         style="M.TLabel")
        note.pack(fill="x", padx=8, pady=(8, 2))
        grid = DataGrid(win, frozen=1, on_open_row=lambda row, vals: ValuesWindow(
            self.app, "Row detail — sample row of %s" % t.table,
            "%s (a sample row, not the query's result)" % t.table,
            grid.columns()[1:], list(vals)[1:]), on_open_blob=lambda row, column, value:
            self.app.open_blob(value, column, "%s sample row %s" % (t.table, row + 1)))
        grid.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        ttk.Button(win, text="Close", command=win.destroy).pack(anchor="e", padx=8, pady=(0, 8))
        win.bind("<Escape>", lambda e: win.destroy())
        n = self.SAMPLE_ROWS

        def work():
            cols, rows = self._sample_table_rows(alias, n)
            return cols, rows

        def done(result, error):
            if not win.winfo_exists():
                return
            if error is not None:
                note.configure(text="The rows could not be read: %s" % error)
                return
            cols, rows = result
            grid.set_source(ListSource([RID] + list(cols),
                                       [([i + 1] + list(r), ()) for i, r in enumerate(rows)]))
            note.configure(text="%s: the first %d row%s as stored (double-click a row for every "
                                "value; the query's own results are under Results)" % (
                                    t.table, len(rows), "" if len(rows) == 1 else "s"))
        self.runner.submit("sample-rows-%s" % alias, work, done)
        return win

    def _sample_table_rows(self, alias, n):
        """(columns, rows) of the first n rows of alias's table (on the worker)."""
        t = self.model.table(alias)
        if t.db == qm.RECOVERED:
            cols, rows = self._recovered_rows(t.table)
            return cols, rows[:n]
        session = self._session(t.db)
        names = list(session.info(t.table).column_names)
        from engine.schema import quote_ident
        if getattr(session, "sql", None) is not None:
            cur = session.conn().execute("SELECT %s FROM %s LIMIT %d" % (
                ", ".join(quote_ident(c) for c in names), quote_ident(t.table), int(n)))
            try:
                return names, [list(r) for r in cur.fetchall()]
            finally:
                cur.close()
        out = []
        for r in session.iter_rows(t.table):
            out.append(list(r.values[:len(names)]))
            if len(out) >= n:
                break
        return names, out

    def _tip_show(self, hit, x, y):
        self._tip_job = None
        text = self._tip_text(hit)
        if not text or hit != self._tip_for:
            return
        self._tip_at = (x, y)
        cv = self.cv
        t = cv.create_text(x + 14, y + 18, anchor="nw", text=text, font=F["small"],
                           fill=K["tip_text"], tags=("tip",))
        x0, y0, x1, y1 = cv.bbox(t)
        r = cv.create_rectangle(x0 - 6, y0 - 4, x1 + 6, y1 + 4, fill=K["tip"], outline=K["tip"],
                                tags=("tip",))
        cv.tag_raise(t, r)

    def _tip_hide(self, _e=None):
        job = getattr(self, "_tip_job", None)
        if job is not None:
            self.after_cancel(job)
            self._tip_job = None
        self._tip_for = None
        self.cv.delete("tip")

    def _context(self, e):
        self._tip_hide()
        hit, _x, _y = self._hit(e)
        if hit is None:
            return
        if hit[0] in ("head", "close", "menu", "more", "body"):
            self._card_menu(e, hit[1])
            return
        if hit[0] == "join":
            self._join_menu(e, hit[1])
            return
        if hit[0] != "col":
            return
        m = tk.Menu(self, tearoff=0)
        alias, col = hit[1], hit[2]
        others = [t for t in self.model.tables if t.alias != alias]
        for t in others:
            sub = tk.Menu(m, tearoff=0)
            for row in self._spec(t)[:60]:
                c = row[0]
                sub.add_command(label="%s   %s" % (c, (row[1] or "").upper()),
                                command=lambda a=t.alias, c=c: self._join_by_hand(
                                    alias, col, a, c))
            m.add_cascade(label="Join to %s ▸" % self.card_title(t.alias), menu=sub)
        if others:
            m.add_separator()
        m.add_command(label="Filter %s…" % col, command=lambda: self.filter_panel(alias, col))
        m.add_separator()
        if self.model.has_column(alias, col):
            m.add_cascade(label="How %s is shown ▸" % col, menu=self._column_menu(alias, col))
        else:
            m.add_command(label="Show %s in the results" % col, command=lambda: (
                self.model.toggle_column(alias, col), self._changed()))
        m.add_command(label="Fields inside %s… (BLOB, plist, JSON)" % col,
                      command=lambda: self.fields_dialog(alias, col))
        m.add_command(label="Add a condition on %s…" % col,
                      command=lambda: self.add_filter(alias, col))
        m.add_command(label="Sort by %s" % col, command=lambda: self._set_sort(alias, col,
                                                                            False))
        m.add_command(label="Sort by %s, largest first" % col,
                      command=lambda: self._set_sort(alias, col, True))
        m.tk_popup(e.x_root, e.y_root)

    def _card_menu(self, e, alias):
        t = self.model.table(alias)
        if t is None:
            return
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Show every column", command=lambda: self._all_of(alias, True))
        m.add_command(label="Hide every column", command=lambda: self._all_of(alias, False))
        m.add_command(label="Choose columns…", command=lambda: self._more_columns(alias))
        m.add_separator()
        m.add_command(label="Show sample rows… (the first %d, as stored)" % self.SAMPLE_ROWS,
                      command=lambda: self.sample_rows(alias))
        m.add_command(label="Add a condition…", command=lambda: self.add_filter(alias, None))
        m.add_command(label="Tidy the layout", command=self.tidy)
        m.add_separator()
        m.add_command(label="Remove %s from the query" % t.table,
                      command=lambda: self.remove_table(alias))
        m.tk_popup(e.x_root, e.y_root)

    def _all_of(self, alias, on):
        t = self.model.table(alias)
        for row in self._spec(t):
            if self.model.has_column(alias, row[0]) != on:
                self.model.toggle_column(alias, row[0])
        self._changed()

    def _join_menu(self, e, i):
        if i >= len(self.model.joins):
            return
        j = self.model.joins[i]
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="%s  (%s)" % (j.text(), j.why or "joined by hand"), state="disabled")
        m.add_separator()
        for kind, text in (("LEFT", "Keep all - lines without a match stay, with empty cells "
                                    "(LEFT JOIN)"),
                           ("INNER", "Matches only - lines without a match are left out "
                                     "(INNER JOIN)")):
            m.add_command(label=("✓ " if j.kind == kind else "   ") + text,
                          command=lambda k=kind: self._set_join_kind(i, k))
        alts = self._alternatives(j)
        if alts:
            m.add_separator()
            m.add_command(label="Link by another column instead:", state="disabled")
            for alt in alts:
                m.add_command(label="   %s  (%s)" % (alt.text(), alt.why),
                              command=lambda a=alt: self._use_link(i, a))
        m.add_command(label="Remove this join", command=lambda: self._remove_join(i))
        m.tk_popup(e.x_root, e.y_root)

    def _alternatives(self, j):
        """The other trusted links found when the table at either end of j was added, whose
        tables are still in the query."""
        out = []
        for alias in (j.left, j.right):
            for alt in self._others.get(alias, ()):
                if self.model.table(alt.left) is not None and \
                        self.model.table(alt.right) is not None and \
                        alt.text() != j.text() and all(alt.text() != o.text() for o in out):
                    out.append(alt)
        return out

    def _use_link(self, i, alt):
        """Join by alt in place of join i; the replaced link becomes an alternative."""
        old = self.model.joins[i]
        owner = alt.left if alt in self._others.get(alt.left, ()) else alt.right
        rest = [o for o in self._others.get(owner, ()) if o is not alt]
        alt.kind = old.kind
        self.model.joins[i] = alt
        self._others[owner] = rest + [old]
        self._changed()
        self.status.configure(text="Joined by %s (%s) now; %s is kept in the line's menu." % (
            alt.text(), alt.why, old.text()))

    def _set_join_kind(self, i, kind):
        self.model.joins[i].kind = kind
        self._changed()

    def _remove_join(self, i):
        del self.model.joins[i]
        self._changed()

    def _join_by_hand(self, a, ca, b, cb):
        self.model.joins.append(qm.Join(a, ca, b, cb, "LEFT", "joined by hand"))
        self._changed()

    def _more_columns(self, alias):
        """Every column of a card in a checklist with a search (tables with many columns):
        ticks survive the search; OK applies them."""
        t = self.model.table(alias)
        spec = self._spec(t)
        want = set(r[0] for r in spec if self.model.has_column(alias, r[0]))
        w = tk.Toplevel(self)
        w.title("Columns of %s" % t.table)
        w.configure(bg=C["bg"])
        w.transient(self.winfo_toplevel())
        find = SearchBox(w, placeholder="Find a column…", delay=0, find_button=False)
        find.pack(fill="x", padx=8, pady=(8, 4))
        box = ttk.Frame(w)
        box.pack(fill="both", expand=True, padx=8)
        tree = ttk.Treeview(box, columns=("type", "key"), show="tree headings", height=16,
                            selectmode="none")
        tree.heading("#0", text="Column")
        tree.heading("type", text="Type")
        tree.heading("key", text="Key")
        tree.column("#0", width=230)
        tree.column("type", width=110, stretch=False)
        tree.column("key", width=120, stretch=False)
        _scrolled(box, tree)
        count = ttk.Label(w, style="M.TLabel")
        count.pack(fill="x", padx=8)

        def fill(_t=None):
            term = find.var.get().strip().lower() if hasattr(find, "var") else ""
            tree.delete(*tree.get_children())
            for name, typ, key, fk, _u in spec:
                if term and term not in name.lower():
                    continue
                mark = "☑  " if name in want else "☐  "
                tree.insert("", "end", iid=name, text=mark + name,
                            values=((typ or "any").upper(), " ".join(
                                x for x in (key, "FK → " + fk if fk else "") if x)))
            count.configure(text="%d of %d columns ticked" % (len(want), len(spec)))

        def flip(e):
            iid = tree.identify_row(e.y)
            if iid:
                want.symmetric_difference_update((iid,))
                fill()
        find.on_change = fill
        tree.bind("<Button-1>", flip)
        tree.bind("<space>", lambda e: (want.symmetric_difference_update(
            (tree.focus(),)) if tree.focus() else None, fill()))

        def every(on):
            shown = set(tree.get_children())
            want.update(shown) if on else want.difference_update(shown)
            fill()

        def ok():
            for name, *_rest in spec:
                if (name in want) != self.model.has_column(alias, name):
                    self.model.toggle_column(alias, name)
            w.destroy()
            self._changed()
        bar = FlowFrame(w)
        bar.pack(fill="x", padx=8, pady=8)
        bar.add(ttk.Button(bar, text="Tick the ones listed", command=lambda: every(True)))
        bar.add(ttk.Button(bar, text="Untick the ones listed", command=lambda: every(False)),
                gap=4)
        bar.add(ttk.Button(bar, text="OK", style="P.TButton", command=ok), gap=12)
        fill()
        w.ok, w.tree, w.find, w.want = ok, tree, find, want
        return w

    # -- columns, filters, options ---------------------------------------------------------------
    def _more_menu(self, button):
        """The column actions used less often, in one menu."""
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Label…", command=self._label)
        m.add_command(label="Summary (count, sum, min, max…) ▸", command=self._agg_menu)
        m.add_command(label="Shown as (date, size, hex…) ▸", command=self._show_menu)
        m.add_command(label="Meanings of codes…", command=self._meanings)
        m.add_separator()
        m.add_command(label="Join a table by hand…", command=self.join_dialog)
        try:
            m.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            m.grab_release()

    def _sel_column(self):
        sel = self.cols_tree.selection()
        return int(sel[0]) if sel else None

    def _cols_drag(self, e):
        i, j = self._sel_column(), self.cols_tree.identify_row(e.y)
        if i is None or not j or int(j) == i:
            return
        cols = self.model.columns
        cols.insert(int(j), cols.pop(i))
        self._changed()
        self.cols_tree.selection_set(j)
        self.cols_tree.focus(j)

    def _cols_context(self, e):
        row = self.cols_tree.identify_row(e.y)
        if not row:
            return
        self.cols_tree.selection_set(row)
        c = self.model.columns[int(row)]
        self._column_menu(c.alias, c.column, int(row)).tk_popup(e.x_root, e.y_root)

    def _column_menu(self, alias, col, i=None):
        """How a shown column is presented: as a date, with meanings, summarised, its label;
        the fields inside its values; and taking it out of the results."""
        m = tk.Menu(self, tearoff=0)
        if i is None:
            i = next((k for k, c in enumerate(self.model.columns)
                      if (c.alias, c.column) == (alias, col) and not c.paths), None)
        if i is None:
            return m
        c = self.model.columns[i]
        if not c.made_by_tool() and not c.agg:
            m.add_command(label="Filter %s…" % col, command=lambda: self.filter_panel(alias,
                                                                                       col))
            m.add_separator()
        show = tk.Menu(m, tearoff=0)
        show.add_command(label=("✓ " if c.show is None else "   ") + "The stored value",
                         command=lambda: self._set_show(i, None))
        show.add_separator()
        for kind, label in tl.LABELS.items():
            show.add_command(label=("✓ " if c.show == kind else "   ") + label + " (UTC)",
                             command=lambda k=kind: self._set_show(i, k))
        m.add_cascade(label="Show as ▸", menu=show)
        m.add_command(label="Meanings of the values…", command=lambda: self._meanings(i))
        agg = tk.Menu(m, tearoff=0)
        agg.add_command(label=("✓ " if c.agg is None else "   ") + "The value (a group)",
                        command=lambda: self._set_agg(i, None))
        agg.add_separator()
        for key, label, _sql in qm.AGGREGATES:
            agg.add_command(label=("✓ " if c.agg == key else "   ") + label,
                            command=lambda k=key: self._set_agg(i, k))
        if c.paths:
            m.add_command(label="A field read out of the values: no summary",
                          state="disabled")
        else:
            m.add_cascade(label="Summary ▸", menu=agg)
        m.add_command(label="Label…", command=lambda: (self.cols_tree.selection_set(str(i)),
                                                       self._label()))
        m.add_command(label="Fields inside %s… (BLOB, plist, JSON)" % col,
                      command=lambda: self.fields_dialog(alias, col))
        m.add_command(label="Calculate from this column… (÷, round, text, …)",
                      command=lambda: self.calc_dialog(i))
        if c.calc:
            m.add_command(label="Change the calculation…",
                          command=lambda: self.calc_dialog(i, edit=True))
        m.add_separator()
        m.add_command(label="Take it out of the results", command=lambda: (
            self.model.columns.pop(i), self._changed()))
        return m

    # -- fields inside a column's values ---------------------------------------------------------
    def _sample_values(self, alias, col, n):
        """Up to n stored values of alias.column (on the worker)."""
        t = self.model.table(alias)
        if t.db == qm.RECOVERED:
            cols, rows = self._recovered_rows(t.table)
            k = cols.index(col) if col in cols else None
            return [r[k] for r in rows[:n]] if k is not None else []
        session = self._session(t.db)
        from engine.schema import quote_ident
        if getattr(session, "sql", None) is not None:
            cur = session.conn().execute("SELECT %s FROM %s LIMIT %d" % (
                quote_ident(col), quote_ident(t.table), int(n)))
            try:
                return [r[0] for r in cur.fetchall()]
            finally:
                cur.close()
        names = list(session.info(t.table).column_names)
        k = names.index(col)
        out = []
        for r in session.iter_rows(t.table):
            out.append(r.values[k] if k < len(r.values) else None)
            if len(out) >= n:
                break
        return out

    def fields_dialog(self, alias, col):
        """The fields found inside a sample of alias.column's values (decoded BLOBs, plists,
        JSON): tick fields to show them as columns, each on its own or several as one column
        (the first that has a value in a row), or join a table on one."""
        from fields_ui import FieldsDialog, ask_name
        t = self.model.table(alias)
        if t is None:
            return None
        n = limits.get("blob_fields_sample")

        def added(k):
            self._changed()
            self.status.configure(text="%d column%s added from the fields inside %s.%s - Run "
                                       "to read them." % (k, "" if k == 1 else "s", t.table,
                                                          col))
            return k

        def each(paths, dlg):
            for p in paths:
                self._add_field(alias, col, [p], None, dlg.discovery())
            return added(len(paths))

        def one(paths, dlg, name=None):
            name = ask_name(dlg, len(paths)) if name is None else name
            if name is None:
                return None
            self._add_field(alias, col, paths, name, dlg.discovery())
            return added(1)

        def join(paths, dlg):
            self.join_on_field(alias, col, paths, parent=dlg)
            return None

        def load():
            from engine.decode import fields as fl
            vals = self._sample_values(alias, col, n)
            return fl.discover(vals, limits.get("blob_fields_paths")), len(vals)
        w = FieldsDialog(self, self.runner, t.table, col, load, [
            ("Add each ticked field as a column", True, None, each),
            ("Add the ticked fields as ONE column…", False,
             "For values that keep the same thing in different places (a sender under 'from' "
             "in some rows and under 'paidFrom' in others): the first ticked field that has a "
             "value is shown", one),
            ("Join a table on the ticked field…", False,
             "Match the ticked field (the first ticked that has a value) to a column of "
             "another table, as a join: the tool reads the field out of each row after the "
             "query and looks it up", join)])
        w.each = lambda: w._act(each)
        w.one_column = lambda name: (one(list(w.ticked), w, name), w.ticked.clear(), w.fill())
        return w

    def calc_dialog(self, i, edit=False):
        """The calculator for column i of 'Columns shown': a new column made from its values
        (edit: change column i's own calculation). The preview uses the rows of the last run
        when they have the column, else a sample of the stored values."""
        from calc_ui import CalcDialog
        if not 0 <= i < len(self.model.columns):
            return None
        c = self.model.columns[i]
        heads = self.model.headings()
        src_head = heads[i]
        others = [h for k, h in enumerate(heads) if k != i]
        samples = []
        cols = self.grid.columns()
        base_head = (c.field_name() if c.paths else c.column) if edit else src_head
        if not edit and src_head in cols and self.grid.row_count():
            k = cols.index(src_head)
            for vals, _f in self.grid.fetch_rows(0, 15):
                samples.append((vals[k], dict(zip(cols, vals))))
        else:
            try:
                from engine.decode import fields as fl
                for v in self._sample_values(c.alias, c.column, 15):
                    if c.paths:
                        v = fl.read(v, c.paths)[0]
                    samples.append((v, {}))
            except Exception:           # noqa: BLE001 - a preview without values still works
                samples = []

        def ok(steps, name):
            if edit:
                c.calc, c.label = steps, name or c.label
            else:
                self.model.add_calc(c, steps, name)
            self._changed()
            self.status.configure(text="'%s' %s - Run to work it out for every row." % (
                name or "the calculated column", "changed" if edit else "added"))
        return CalcDialog(self, base_head, samples, others, ok,
                          steps=c.calc if edit else None, name=c.label if edit else None)

    def join_on_field(self, alias, col, paths, parent=None, to=None):
        """Join a table of the same database on a field inside alias.column's values: pick
        'table.column' (searchable); to: that choice given directly (no dialog)."""
        if not paths:
            self.status.configure(text="Tick the field to join on first.")
            return None
        src = self.model.table(alias)
        choices = ["%s.%s" % (t, c) for t in self._tables(src.db) for c in self._columns(
            t, src.db)]

        def ok(vals):
            table, _dot, column = vals[0].partition(".")
            if table not in self._tables(src.db) or column not in self._columns(table, src.db):
                return "Choose a table.column from the list."
            a = self.model.add_by_field(table, alias, col, paths, column, src.db)
            n = len(self.model.tables) - 1
            self._pos[a] = (16 + (n % PER_ROW) * (CARD_W + CARD_GAP), 12 + (n // PER_ROW) * 220)
            self._changed()
            if not self._manual:
                self.tidy()
            self.status.configure(text="%s joined on the field %s inside %s.%s (matched by the "
                                       "tool after the query): tick its columns to show them."
                                       % (table, " / ".join(paths), src.table, col))
            return None
        if to is not None:
            return ok([to])
        return self._pick("Join a table on a field", [("Equals table.column", choices)], ok)

    def _add_field(self, alias, col, paths, label, discovery):
        """Add a field column; a field whose name and numbers look like a date is shown as
        one (the stored value stays in copies and exports)."""
        c = self.model.add_field(alias, col, paths, label)
        last = paths[0].replace("]", "").split(".")[-1].split("[")[0]
        f = next((x for x in getattr(discovery, "fields", ()) if x.path == paths[0]), None)
        if f is not None and tl.name_hint(last) in ("strong", "weak"):
            nums = []
            for e in f.examples:
                try:
                    nums.append(float(e) if "." in e else int(e))
                except ValueError:
                    pass
            kind = tl.suggest_kind(last, nums) if nums else None
            if kind in tl.KINDS:
                c.show = kind
        return c

    def _move(self, d):
        i = self._sel_column()
        cols = self.model.columns
        if i is None or not 0 <= i + d < len(cols):
            return
        cols[i], cols[i + d] = cols[i + d], cols[i]
        self._changed()
        self.cols_tree.selection_set(str(i + d))

    def _remove_column(self):
        i = self._sel_column()
        if i is not None:
            del self.model.columns[i]
            self._changed()

    def _label(self):
        i = self._sel_column()
        if i is None:
            return
        c = self.model.columns[i]
        text = simpledialog.askstring("Label", "Heading of %s.%s in the results (empty: its "
                                      "name):" % (c.alias, c.column), initialvalue=c.label or "",
                                      parent=self)
        if text is not None:
            c.label = text.strip() or None
            self._changed()

    def _show_menu(self):
        i = self._sel_column()
        if i is None:
            return
        c = self.model.columns[i]
        m = tk.Menu(self, tearoff=0)
        m.add_command(label=("✓ " if c.show is None else "   ") + "The stored value",
                      command=lambda: self._set_show(i, None))
        m.add_separator()
        for kind, label in tl.LABELS.items():
            m.add_command(label=("✓ " if c.show == kind else "   ") + label + " (UTC)",
                          command=lambda k=kind: self._set_show(i, k))
        m.tk_popup(self.winfo_pointerx(), self.winfo_pointery())

    def _agg_menu(self):
        """A summary of the column per group (the columns without one are the groups)."""
        i = self._sel_column()
        if i is None:
            self.status.configure(text="Select a column in 'Columns shown' first.")
            return
        c = self.model.columns[i]
        m = tk.Menu(self, tearoff=0)
        m.add_command(label=("✓ " if c.agg is None else "   ") + "The value (a group)",
                      command=lambda: self._set_agg(i, None))
        m.add_separator()
        for key, label, _sql in qm.AGGREGATES:
            m.add_command(label=("✓ " if c.agg == key else "   ") + label,
                          command=lambda k=key: self._set_agg(i, k))
        m.tk_popup(self.winfo_pointerx(), self.winfo_pointery())

    def _set_agg(self, i, key):
        self.model.columns[i].agg = key
        self._changed()
        self.cols_tree.selection_set(str(i))

    def _set_show(self, i, kind):
        c = self.model.columns[i]
        c.show = kind
        hand = self.__dict__.setdefault("_stored_by_hand", set())
        (hand.add if kind is None else hand.discard)((c.alias, c.column))
        self._changed()
        self._apply_formatters()

    def _meanings(self, i=None):
        """What the column's stored values mean ('2 = incoming'), shown beside the value;
        sets of meanings can be kept by name and used on other columns."""
        i = self._sel_column() if i is None else i
        if i is None:
            self.status.configure(text="Select a column in 'Columns shown' first.")
            return None
        c = self.model.columns[i]
        settings, sets = self._value_sets()
        w = tk.Toplevel(self)
        w.title("Meanings of %s.%s" % (c.alias, c.column))
        w.configure(bg=C["bg"])
        w.transient(self.winfo_toplevel())
        ttk.Label(w, text="One per line: stored value = what it means (shown as '2 (incoming)'; "
                          "the stored value stays in copies and exports).",
                  style="M.TLabel", wraplength=420).pack(fill="x", padx=10, pady=(10, 4))
        txt = tk.Text(w, height=12, width=50, font=F["mono"], bg=K["card"], fg=K["text"],
                      insertbackground=K["text"])
        txt.pack(fill="both", expand=True, padx=10)
        txt.insert("1.0", qm.meanings_text(c.meanings))
        msg = ttk.Label(w, text="", style="M.TLabel")
        msg.pack(fill="x", padx=10)
        bar = FlowFrame(w)
        bar.pack(fill="x", padx=10, pady=8)

        def use_set():
            m = tk.Menu(w, tearoff=0)
            if not sets:
                m.add_command(label="No saved sets yet", state="disabled")
            for name in sorted(sets, key=str.lower):
                m.add_command(label=name, command=lambda n=name: (
                    txt.delete("1.0", "end"), txt.insert("1.0", qm.meanings_text(sets[n]))))
            m.tk_popup(w.winfo_pointerx(), w.winfo_pointery())

        def save_set():
            try:
                meanings = qm.parse_meanings(txt.get("1.0", "end"))
            except ValueError as e:
                msg.configure(text=str(e))
                return
            name = simpledialog.askstring("Save meanings", "Name of the set:", parent=w)
            if name and settings is not None:
                sets[name.strip()] = meanings
                try:
                    self.app.tags.save_settings()
                except Exception:       # noqa: BLE001 - kept for this session
                    pass
                msg.configure(text="Saved as '%s'." % name.strip())

        def ok():
            try:
                c.meanings = qm.parse_meanings(txt.get("1.0", "end"))
            except ValueError as e:
                msg.configure(text=str(e))
                return
            w.destroy()
            self._changed()
            self._apply_formatters()
        bar.add(ttk.Button(bar, text="Use a saved set ▾", command=use_set))
        bar.add(ttk.Button(bar, text="Save as a set…", command=save_set), gap=4)
        bar.add(ttk.Button(bar, text="OK", style="P.TButton", command=ok), gap=12)
        w.text, w.ok = txt, ok
        return w

    def _value_sets(self):
        settings = getattr(getattr(self.app, "tags", None), "settings", None)
        if not isinstance(settings, dict):
            return None, {}
        return settings, settings.setdefault("value_sets", {})

    def _sort_menu(self):
        i = self._sel_column()
        if i is None:
            return
        c = self.model.columns[i]
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Ascending", command=lambda: self._set_sort(c.alias, c.column, False))
        m.add_command(label="Descending", command=lambda: self._set_sort(c.alias, c.column, True))
        m.add_command(label="Not sorted", command=lambda: self._set_sort(c.alias, c.column, None))
        m.tk_popup(self.winfo_pointerx(), self.winfo_pointery())

    def _set_sort(self, alias, col, desc):
        t = self.model.table(alias)
        if t is not None and t.by:
            self.status.configure(text="%s is matched after the query: sort it in the results "
                                       "(click its heading)." % t.table)
            return
        self.model.sort = [s for s in self.model.sort if (s[0], s[1]) != (alias, col)]
        if desc is not None:
            self.model.sort.append((alias, col, desc))
        self._changed()

    def _condition_columns(self):
        """[(key, label, summary?, date kind)] a condition can be on: every column of the
        tables in the SQL, and the summary columns (HAVING)."""
        from date_reading import column_kind
        out = []
        shown = dict(((c.alias, c.column), c.show) for c in self.model.columns
                     if c.show and not c.made_by_tool() and not c.agg)
        for t in self.model.tables:
            if t.by:
                continue
            m = self._member(t.db) if t.db != qm.RECOVERED else None
            db = m.db if m is not None else self.db
            for c in self._columns(t.table, t.db):
                kind = shown.get((t.alias, c))
                if kind is None and t.db != qm.RECOVERED and \
                        tl.name_hint(c) in ("strong", "weak"):
                    kind = column_kind(db, t.table, c)
                out.append(("%s.%s" % (t.alias, c), "%s.%s" % (self.card_title(t.alias), c),
                            False, kind))
        for c, h in zip(self.model.columns, self.model.headings()):
            if c.agg:
                out.append((h, "%s  (summary)" % h, True, None))
        return out

    def _values_of(self, key, done):
        """(values with how many rows have each, most frequent first; cut?) of alias.column."""
        alias, _dot, col = key.partition(".")
        t = self.model.table(alias)
        n = limits.get("value_list_values")
        if t is None:
            return done(None, "no such table")
        if t.db == qm.RECOVERED:
            cols, rows = self._recovered_rows(t.table)
            k = cols.index(col)
            counts = {}
            for r in rows:
                counts[r[k]] = counts.get(r[k], 0) + 1
            got = sorted(counts.items(), key=lambda x: -x[1])
            return done((got[:n], len(got) > n), None)
        session = self._session(t.db)
        from engine.schema import quote_ident

        def work():
            if getattr(session, "sql", None) is None:
                names = list(session.info(t.table).column_names)
                k = names.index(col)
                counts = {}
                for r in session.iter_rows(t.table):
                    v = r.values[k] if k < len(r.values) else None
                    counts[v] = counts.get(v, 0) + 1
                got = sorted(counts.items(), key=lambda x: -x[1])
                return got[:n], len(got) > n
            cur = session.conn().execute(
                "SELECT %s, COUNT(*) FROM %s GROUP BY 1 ORDER BY 2 DESC LIMIT %d" % (
                    quote_ident(col), quote_ident(t.table), n + 1))
            try:
                got = cur.fetchall()
            finally:
                cur.close()
            return got[:n], len(got) > n
        self.runner.submit("values", work, done)

    def _count_with(self, f, done):
        """(rows the query keeps with this one condition, rows without it) - single database
        queries (across databases the count shows when the query runs)."""
        dbs = self.model.databases()
        if len(dbs) != 1 or dbs[0] == qm.RECOVERED:
            return done(None, "the count shows when you Run (the query reads several "
                              "sources)")
        session = self._session(dbs[0])
        if session is None or getattr(session, "sql", None) is None:
            return done(None, "the count needs SQLite (this database is read natively)")
        m = qm.QueryModel.from_dict(self.model.to_dict())
        m.limit, m.offset, m.sort = None, 0, []
        m.filters, m.match = [], "all"
        base = m.to_sql()
        m.filters = [f]
        sql = m.to_sql()

        def work():
            conn = session.conn()
            out = []
            for q in (sql, base):
                cur = conn.execute("SELECT COUNT(*) FROM (%s)" % q)
                try:
                    out.append(cur.fetchone()[0])
                finally:
                    cur.close()
            return tuple(out)
        self.runner.submit("count", work, done)

    def add_filter(self, alias=None, column=None, into=None, edit=None):
        """The condition dialog (alias.column preselected; into: a Group to add to; edit:
        (index, sub-index or None) of a condition to change)."""
        from conditions_ui import ConditionDialog
        cols = self._condition_columns()
        if not cols:
            self.status.configure(text="Add a table first.")
            return None
        start = "%s.%s" % (alias, column) if alias else None
        if edit is not None:
            i, j = edit
            start = self.model.filters[i] if j is None else self.model.filters[i].items[j]

        def ok(f):
            if edit is not None:
                i, j = edit
                if j is None:
                    self.model.filters[i] = f
                else:
                    self.model.filters[i].items[j] = f
            elif into is not None:
                into.items.append(f)
            else:
                self.model.filters.append(f)
            self.mid.select(1)
            self._changed()
        w = ConditionDialog(self, cols, ok, values=self._values_of, count=self._count_with,
                            start=start, title="Change the condition" if edit else
                            "Add a condition")
        w.column, w.value = w.col, None
        return w

    def _column_filter(self, alias, col):
        """(index, condition) of the column's own condition (the Filter panel's), or
        (None, None)."""
        for i, f in enumerate(self.model.filters):
            if isinstance(f, qm.Group):
                if f.items and all((x.alias, x.column) == (alias, col) for x in f.items):
                    return i, f
            elif (f.alias, f.column) == (alias, col) and not f.summary:
                return i, f
        return None, None

    def _date_kind(self, alias, col):
        """The date kind the column is read as: its 'Shown as', else the detector's."""
        from date_reading import column_kind
        for c in self.model.columns:
            if (c.alias, c.column) == (alias, col) and c.show and not c.made_by_tool():
                return c.show
        t = self.model.table(alias)
        if t is None or t.db == qm.RECOVERED or tl.name_hint(col) not in ("strong", "weak"):
            return None
        m = self._member(t.db)
        return column_kind(m.db if m is not None else self.db, t.table, col)

    def filter_panel(self, alias, col):
        """Filter one column by what it holds (filter_panel.FilterPanel)."""
        from filter_panel import FilterPanel
        from engine.profile import profile_sql, profile_values
        t = self.model.table(alias)
        if t is None or t.by:
            return None
        at, current = self._column_filter(alias, col)

        def load(kind, done):
            if t.db == qm.RECOVERED:
                cols, rows = self._recovered_rows(t.table)
                k = cols.index(col)
                return done(profile_values([r[k] for r in rows], kind), None)
            session = self._session(t.db)

            def work():
                if getattr(session, "sql", None) is None:
                    names = list(session.info(t.table).column_names)
                    k = names.index(col)
                    return profile_values([r.values[k] if k < len(r.values) else None
                                           for r in session.iter_rows(t.table)], kind)
                return profile_sql(session.conn(), t.table, col, kind)
            self.runner.submit("profile", work, done)

        def applied(f):
            i, _cur = self._column_filter(alias, col)
            if i is not None:
                del self.model.filters[i]
            if f is not None:
                self.model.filters.insert(i if i is not None else len(self.model.filters), f)
            self._changed()
            self.status.configure(text=("Filter on %s: %s - Run to see the rows." % (
                col, f.text(self.card_title))) if f is not None else
                "The filter on %s is removed." % col)

        def more():
            if at is not None and isinstance(current, qm.Filter):
                return self.add_filter(edit=(at, None))
            return self.add_filter(alias, col)
        return FilterPanel(self, (alias, col, "%s.%s" % (self.card_title(alias), col)),
                           self._date_kind(alias, col), load, self._count_with, applied,
                           more=more, current=current)

    def _selected_filter(self):
        """(index, sub-index or None) of the selected line of 'Rows where', or None."""
        sel = self.flt_tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if iid.startswith("g"):
            g, _c, j = iid[1:].partition(":")
            return int(g), (int(j) if j else None)
        return int(iid), None

    def _add_condition(self):
        """Into the selected group when a group (or one of its lines) is selected."""
        at = self._selected_filter()
        if at is not None and isinstance(self.model.filters[at[0]], qm.Group):
            return self.add_filter(into=self.model.filters[at[0]])
        return self.add_filter()

    def _add_group(self):
        g = qm.Group("any", [])
        self.model.filters.append(g)

        def closed(_e=None):
            if not g.items and g in self.model.filters:
                self.model.filters.remove(g)
            self._changed()
        w = self.add_filter(into=g)
        if w is not None:
            w.bind("<Destroy>", lambda e: closed() if e.widget is w else None, add="+")
        return w

    def _edit_filter(self):
        at = self._selected_filter()
        if at is None:
            return None
        i, j = at
        if j is None and isinstance(self.model.filters[i], qm.Group):
            g = self.model.filters[i]
            g.mode = "all" if g.mode == "any" else "any"     # a group's line: any <-> all
            self._changed()
            return None
        return self.add_filter(edit=(i, j))

    # -- the keyboard path (no canvas needed) -------------------------------------------------------
    def _all_columns(self):
        return ["%s.%s" % (t.alias, c) for t in self.model.tables
                for c in self._columns(t.table, t.db)]

    def _pick(self, title, rows, on_ok):
        """A small dialog of searchable lists (label, choices); Enter or Add confirms."""
        w = tk.Toplevel(self)
        w.title(title)
        w.configure(bg=C["bg"])
        w.transient(self.winfo_toplevel())
        frm = ttk.Frame(w)
        frm.pack(fill="both", expand=True, padx=10, pady=10)
        boxes = []
        for i, (label, choices) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=2)
            box = SearchableCombobox(frm, values=choices, width=38)
            box.grid(row=i, column=1, sticky="we", pady=2)
            if choices:
                box.set(choices[0])
            boxes.append(box)
        msg = ttk.Label(frm, text="", style="M.TLabel")
        msg.grid(row=len(rows), column=0, columnspan=2, sticky="w")

        def ok(_e=None):
            err = on_ok([b.get() for b in boxes])
            if err:
                msg.configure(text=err)
                return
            w.destroy()
        ttk.Button(frm, text="Add", style="P.TButton", command=ok).grid(
            row=len(rows) + 1, column=1, sticky="e", pady=(8, 0))
        w.bind("<Return>", ok)
        w.bind("<Escape>", lambda e: w.destroy())
        if boxes:
            boxes[0].focus_set()
        w.boxes, w.ok = boxes, ok
        return w

    def _split(self, text):
        a, _dot, c = text.partition(".")
        t = self.model.table(a)
        if t is None or c not in self._columns(t.table, t.db):
            return None, None
        return a, c

    def add_column_dialog(self):
        """Add a column to 'Columns shown' by typing its name."""
        if not self.model.tables:
            self.status.configure(text="Add a table first.")
            return None

        def ok(vals):
            a, c = self._split(vals[0])
            if a is None:
                return "Choose a column from the list."
            if not self.model.has_column(a, c):
                self.model.toggle_column(a, c)
            self._changed()
            return None
        return self._pick("Add a column", [("Column", self._all_columns())], ok)

    def join_dialog(self):
        """Join two columns of two tables of the query, as the canvas's right-click does."""
        if len(self.model.tables) < 2:
            self.status.configure(text="Add a second table to join.")
            return None
        cols = self._all_columns()

        def ok(vals):
            a, ca = self._split(vals[0])
            b, cb = self._split(vals[1])
            if a is None or b is None:
                return "Choose both columns from the lists."
            if a == b:
                return "Choose columns of two different tables."
            self._join_by_hand(a, ca, b, cb)
            return None
        return self._pick("Join two tables", [("Column", cols), ("equals column", cols)], ok)

    def _remove_filter(self):
        at = self._selected_filter()
        if at is None:
            return
        i, j = at
        if j is None:
            del self.model.filters[i]
        else:
            g = self.model.filters[i]
            del g.items[j]
            if not g.items:
                del self.model.filters[i]
        self._changed()

    # -- state -> views ---------------------------------------------------------------------------
    def _changed(self):
        self._auto_dates()
        self.model.distinct = bool(self.distinct_var.get())
        self.model.match = self.match_var.get()
        self.model.count = bool(self.count_var.get())
        try:
            n = int(self.limit_var.get())
        except ValueError:
            n = DEFAULT_LIMIT
        n = max(1, min(n, limits.get("query_rows")))
        self.limit_var.set(str(n))
        on = bool(self.limit_on.get())
        self.limit_box.state(["!disabled"] if on else ["disabled"])
        self.model.limit = n if on else None
        try:
            self.model.offset = max(0, int(self.offset_var.get() or 0))
        except ValueError:
            self.model.offset = 0
        self.offset_var.set(str(self.model.offset))
        self._refresh_lists()
        self._redraw()

    def _refresh_lists(self):
        tree = self.cols_tree
        tree.delete(*tree.get_children())
        sorts = dict(((a, c), d) for a, c, d in self.model.sort)
        for i, c in enumerate(self.model.columns):
            d = sorts.get((c.alias, c.column))
            t = self.model.table(c.alias)
            typ = next((r[1] for r in self._spec(t) if r[0] == c.column), "") if t else ""
            if c.calc:
                typ = "calculated"
            elif c.paths:
                typ = "field inside"
            tree.insert("", "end", iid=str(i), values=(
                self.card_title(c.alias) if t else c.alias,
                (c.column if not c.paths else "%s › %s" % (c.column, " | ".join(c.paths))) +
                (" → " + __import__("engine.calc", fromlist=["describe"]).describe(c.calc)
                 if c.calc else ""),
                (typ or "any").upper(), qm.AGG_LABEL.get(c.agg, "—"
                                                         if self.model.grouped() else ""),
                c.label or "", tl.LABELS.get(c.show, "stored value")
                if c.show else ("with %d meanings" % len(c.meanings) if c.meanings
                                else "stored value"),
                "" if d is None else ("descending" if d else "ascending")))
        self.mid.tab(0, text="  Columns shown (%d)  " % len(self.model.columns))
        self.mid.tab(1, text="  Rows where (%d, %s)  " % (
            len(self.model.filters), "all hold" if self.model.match == "all" else "any holds")
            if self.model.filters else "  Rows where  ")
        ft = self.flt_tree
        sel = ft.selection()
        ft.delete(*ft.get_children())
        for i, f in enumerate(self.model.filters):
            if isinstance(f, qm.Group):
                gid = "g%d" % i
                ft.insert("", "end", iid=gid, open=True, text="Any of these (%d):" % len(
                    f.items) if f.mode == "any" else "All of these (%d):" % len(f.items))
                for j, x in enumerate(f.items):
                    ft.insert(gid, "end", iid="%s:%d" % (gid, j), text=x.text(self.card_title))
            else:
                ft.insert("", "end", iid=str(i), text=("on the summaries: " if f.summary
                                                         else "") + f.text(self.card_title))
        if sel and ft.exists(sel[0]):
            ft.selection_set(sel[0])
        sql = self.shown_sql()
        self.sql_text.configure(state="normal")
        self.sql_text.delete("1.0", "end")
        self.sql_text.insert("1.0", sql or "-- add a table to build a query")
        self.sql_text.configure(state="disabled")
        self.summary.configure(text=qm.describe(
            self.model, unique=self._unique, label=self.card_title,
            rows=self._exact_rows, looking=self._looking) or
            "Nothing yet: add a table, tick the columns you want, then Run.")
        lone = [a for a in self.model.unjoined() if a not in self._looking]
        if lone and self.model.tables:
            self.status.configure(text="Not joined: %s - every one of its rows meets every row "
                                       "of the others (right-click a column to join it)."
                                       % ", ".join(lone))

    # -- running ----------------------------------------------------------------------------------
    def _schemas(self):
        """{database path: 'db1'...} when the query spans databases (the order they appear)."""
        if not self.model.spans_databases():
            return None
        from engine.crossquery import schema_name
        real = [db for db in self.model.databases() if db != qm.RECOVERED]
        return dict((db, schema_name(i)) for i, db in enumerate(real))

    def shown_sql(self):
        """The SQL as run; across databases, which dbN is which database first (comments)."""
        schemas = self._schemas()
        sql = self.model.to_sql(schemas)
        after = self._after_sql_notes()
        if after and sql:
            sql = "\n".join(after) + "\n" + sql
        if schemas and sql:
            head = ["-- %s = %s" % (sch, self._db_name(db) or db) for db, sch in schemas.items()]
            if qm.RECOVERED in self.model.databases():
                head.append("-- temp.recovered_* = the records the Forensics recovery found "
                            "(a copy in memory)")
            sql = "\n".join(head + [sql])
        return sql

    def _after_sql_notes(self):
        """Comment lines for what the tool works out after the SQL (SQLite cannot)."""
        from engine import calc
        out = []
        heads = self.model.headings()
        for c, h in zip(self.model.columns, heads):
            if c.calc:
                out.append("-- %s = %s%s, then %s (worked out by the tool after the query)" % (
                    h, c.column, " field " + " / ".join(c.paths) if c.paths else "",
                    calc.describe(c.calc)))
            elif c.paths:
                out.append("-- %s = the field %s inside %s (read by the tool after the query)"
                           % (h, " / ".join(c.paths), c.column))
        for t in self.model.by_field():
            out.append("-- %s joined on %s.%s = field %s inside %s (matched by the tool after "
                       "the query)" % (t.table, t.table, t.by["to"], " / ".join(t.by["paths"]),
                                       t.by["column"]))
        return out

    def run(self, confirm=True):
        sql = self.model.to_sql()
        if not sql:
            self.status.configure(text="Add a table first.")
            return
        if self._looking:
            # the links are not in yet: running now would pair every row with every row
            self._run_when_linked = confirm
            self.status.configure(text="Run starts as soon as the links of %s are found…" %
                                       ", ".join(self.card_title(a) for a in self._looking
                                                 if self.model.table(a) is not None))
            return
        lone = self.model.unjoined()
        if lone and confirm:
            from tkinter import messagebox
            names = ", ".join(self.card_title(a) for a in lone)
            if not messagebox.askyesno(
                    "Not joined", "%s is not joined to the other tables, so every one of its "
                    "rows is paired with every line of the others (the counts multiply).\n\n"
                    "To join it: right-click one of its columns on its card and choose "
                    "'Join to ...', or use Join... under 'Columns shown'.\n\nRun anyway?"
                    % names, icon="warning", default="no", parent=self):
                self.status.configure(text="Not run: join %s first (right-click one of its "
                                           "columns)." % names)
                return
        dbs = self.model.databases()
        sessions = [(db, self._session(db)) for db in dbs]
        lost = [self._db_name(db) or "the database" for db, s in sessions if s is None]
        if lost:
            self.status.configure(text="%s is no longer open: remove its tables or open it "
                                       "again." % ", ".join(lost))
            return
        recovered = None
        if qm.RECOVERED in dbs:
            recovered = dict((t.table, self._recovered_rows(t.table)) for t in self.model.tables
                             if t.db == qm.RECOVERED)
            sessions = [(db, s) for db, s in sessions if db != qm.RECOVERED]
        native = [self._db_name(db) or "This database" for db, s in sessions
                  if getattr(s, "sql", None) is None]
        if native:
            self.status.configure(text="%s is read natively (SQLite cannot open it), and the "
                                       "Query tab needs SQLite: Browse, Search and the Timeline "
                                       "read it." % ", ".join(native))
            return
        spans, limit = len(dbs) > 1 or recovered is not None, self.model.limit
        cap = limits.get("query_rows")      # no LIMIT: the setting still bounds memory
        schemas = self._schemas()
        sql = self.model.to_sql(schemas)
        self.status.configure(text="Running…")
        self.run_btn.state(["disabled"])
        cols_model = list(self.model.columns)
        in_sql = [self.model.in_sql(c) for c in cols_model]
        heads = self.model.headings()
        pos, k = {}, 0
        for i, ok in enumerate(in_sql):
            if ok:
                pos[i], k = k, k + 1
        fields = [(pos[i], c.alias, c.column, list(c.paths)) for i, c in enumerate(cols_model)
                  if c.paths and in_sql[i]]
        calcs = [(pos[i], [list(st) for st in c.calc]) for i, c in enumerate(cols_model)
                 if c.calc and in_sql[i]]
        where = dict((heads[i], p) for i, p in pos.items())
        fjoins = [(t.alias, t.table, dict(t.by), [c.column for c in cols_model
                                                   if c.alias == t.alias],
                   self._session(t.db)) for t in self.model.by_field()]

        def work():
            conn = None
            if spans:
                from engine.crossquery import case_connection
                conn, _s = case_connection(sessions, recovered)
            try:
                cur = (conn or sessions[0][1].conn()).execute(sql)
                try:
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchmany(cap + 1)
                finally:
                    cur.close()
            finally:
                if conn is not None:
                    conn.close()
            if fields:
                rows = read_fields(rows, fields)
            if calcs:
                rows = apply_calcs(rows, calcs, where)
            if fjoins:
                cols, rows = join_by_fields(cols, rows, fjoins, cols_model, in_sql, heads,
                                            pos, cap)
            return cols, rows

        def done(result, error):
            import sqlite3
            from engine.crossquery import CaseUnavailable
            self.run_btn.state(["!disabled"])
            if isinstance(error, (sqlite3.DatabaseError, CaseUnavailable)):
                self.status.configure(text="SQLite could not run the query: %s (Browse reads "
                                           "damaged tables natively)." % error)
                return
            if error is not None:
                self.status.configure(text="The query failed: %s" % error)
                return
            cols, rows = result
            capped = len(rows) > cap
            del rows[cap:]
            self.grid.set_source(ListSource(cols, [(list(r), ()) for r in rows],
                                            self.db.encoding if self.db.ok else "utf-8"))
            self._apply_formatters()
            self.low.select(self.res_tab)
            text = "%s rows" % format(len(rows), ",")
            if capped:
                text += " - CUT OFF at the query_rows setting (%s): the query finds more; " \
                        "add a condition, or raise query_rows in Settings" % format(cap, ",")
            elif limit and len(rows) >= limit:
                text += " - stopped at 'At most %s rows' (untick it to see every row)" % \
                    format(limit, ",")
            if fields:
                text += " - %d field%s read out of the values (%s)" % (
                    len(fields), "" if len(fields) == 1 else "s", ", ".join(
                        sorted(set(c for _i, _a, c, _p in fields))))
            shown = [c for c in self.model.columns if c.show]
            if shown:
                text += " - %s shown as dates (UTC); exports keep the stored " \
                        "values and add the dates beside them" % ", ".join(c.heading() for c in shown)
            self.status.configure(text=text)
        self.runner.submit("run", work, done)

    def _apply_formatters(self):
        cols = self.grid.columns()
        if not cols:
            return
        for c, h in zip(self.model.columns, self.model.headings()):
            if h not in cols:
                continue
            i = cols.index(h)
            if c.agg not in qm.KEEPS_VALUE:     # a count or sum is not a date or a code
                self.grid.set_column_formatter(i, None)
            elif c.show in tl.KINDS:
                self.grid.set_column_formatter(i, tl.formatter(c.show), "UTC " +
                                               tl.SHORT.get(c.show, ""))
            elif c.meanings:
                self.grid.set_column_formatter(i, qm.meaning_formatter(c.meanings), "meanings")
            else:
                self.grid.set_column_formatter(i, None)

    def _export_menu(self, anchor=None):
        m = tk.Menu(self, tearoff=0)
        state = "normal" if self.grid.source is not None else "disabled"
        m.add_command(label="CSV…", state=state, command=lambda: self.export("csv"))
        m.add_command(label="JSON…", state=state, command=lambda: self.export("json"))
        m.add_command(label="HTML report…", state=state, command=lambda: self.export("html"))
        b = anchor or self.export_btn
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def export(self, fmt, path=None):
        """Write the results (as shown) on a Job, the SQL and the query in the provenance."""
        from jobs import ask_path, export_options, export_rows
        from utils import write_allowed
        src = self.grid.source
        if src is None:
            return None
        n = src.row_count()
        if path is None:
            opts = export_options(self.app, "Export query results",
                                  [("rows", "The %s rows shown" % format(n, ","))], fmt=fmt,
                                  formats=("csv", "json", "html"), spreadsheet_safe=True)
            if opts is None:
                return None
            fmt = opts["fmt"]
            path = ask_path(self.app, fmt, "query_results")
        else:
            opts = {"blob_mode": "hex", "spreadsheet_safe": True}
        if not write_allowed(path):
            return None
        cols = list(src.columns())
        # a column shown as a date keeps its stored value; the date text gets a column of its
        # own next to it, as the grid shows it
        dates = []
        for c, h in zip(self.model.columns, self.model.headings()):
            if c.show in tl.KINDS and c.agg in qm.KEEPS_VALUE and h in cols:
                dates.append((cols.index(h), tl.formatter(c.show)))
        at = dict(dates)
        out_cols = []
        for i, name in enumerate(cols):
            out_cols.append(name)
            if i in at:
                out_cols.append("%s (date, UTC)" % name)
        rows = []
        for r in src.iter_rows():
            row = []
            for i, v in enumerate(r):
                row.append(v)
                if i in at:
                    row.append(at[i](v))
            rows.append(row)
        cols = out_cols
        extra = {"sql": self.shown_sql(), "query": self.model.to_dict(),
                 "dates_shown": dict((h, c.show) for c, h in zip(self.model.columns,
                                                                 self.model.headings())
                                     if c.show)}
        return export_rows(self.app, "Export query results", path, fmt, cols,
                           lambda: iter(rows), "Query tab results", self._export_members(),
                           scope="%s rows" % format(n, ","),
                           filters="; ".join("%s: %s" % kv for kv in sorted(
                               self.grid.filter_texts().items())),
                           blob_mode=opts["blob_mode"], total=len(rows), extra=extra,
                           spreadsheet_safe=opts.get("spreadsheet_safe", True))

    def _export_members(self):
        """The databases the query read (each with its evidence hashes in the manifest)."""
        out = []
        for db in self.model.databases():
            m = self._member(db) if db else getattr(self.app.case, "active", None)
            if m is not None and m not in out:
                out.append(m)
        return out or [self.app.case.active]

    def copy_sql(self):
        self.clipboard_clear()
        self.clipboard_append(self.shown_sql())

    def open_in_sql_tab(self):
        ed = getattr(self.app, "_sql_editor", None)
        if ed is None:
            return
        ed.delete("1.0", "end")
        if self.model.spans_databases():
            self.status.configure(text="This query joins several databases: the SQL tab runs "
                                       "on one database. Copy SQL to keep it.")
            return
        ed.insert("1.0", self.model.to_sql())
        self.app._nb.select(self.app._sql_frame)

    # -- saved queries ---------------------------------------------------------------------------
    def _saved(self):
        settings = getattr(getattr(self.app, "tags", None), "settings", None)
        if not isinstance(settings, dict):
            return None, {}
        return settings, settings.setdefault("queries", {})

    def save(self, name=None):
        if not self.model.tables:
            self.status.configure(text="Nothing to save yet.")
            return None
        if name is None:
            name = simpledialog.askstring(
                "Save query", "Name of the query. To file it under an application, put the "
                "application first: 'Skype / contacts'.", parent=self)
        if not name:
            return None
        settings, saved = self._saved()
        if settings is None:
            return None
        saved[name.strip()] = {"model": self.model.to_dict(),
                               "tables": sorted(set(t.table for t in self.model.tables))}
        try:
            self.app.tags.save_settings()
        except Exception:               # noqa: BLE001 - kept for this session at least
            pass
        self.status.configure(text="Saved as '%s' (with the app settings)." % name.strip())
        return name.strip()

    def _load_menu(self):
        _s, saved = self._saved()
        m = tk.Menu(self, tearoff=0)
        have = set(self._tables())
        if not saved:
            m.add_command(label="No saved queries yet", state="disabled")
        groups = {}
        for name in saved:
            group, sep, short = name.partition(" / ")
            groups.setdefault(group.strip() if sep else "", []).append(
                (short.strip() if sep else name, name))
        for group in sorted(groups, key=lambda g: (g != "", g.lower())):
            menu = m
            if group:
                menu = tk.Menu(m, tearoff=0)
                m.add_cascade(label="%s  (%d)" % (group, len(groups[group])), menu=menu)
            for short, name in sorted(groups[group], key=lambda x: x[0].lower()):
                missing = [t for t in saved[name].get("tables", ()) if t not in have]
                menu.add_command(label=short + ("  (needs %s)" % ", ".join(missing)
                                                if missing else ""),
                                 state="disabled" if missing else "normal",
                                 command=lambda n=name: self.load(n))
        m.tk_popup(self.load_btn.winfo_rootx(), self.load_btn.winfo_rooty() +
                   self.load_btn.winfo_height())

    def load(self, name):
        _s, saved = self._saved()
        spec = saved.get(name)
        if spec is None:
            return False
        self.model = qm.QueryModel.from_dict(spec["model"])
        self._pos = dict((t.alias, (16 + (i % PER_ROW) * (CARD_W + CARD_GAP),
                                    12 + (i // PER_ROW) * 220))
                         for i, t in enumerate(self.model.tables))
        self.distinct_var.set(self.model.distinct)
        self.count_var.set(self.model.count)
        self.limit_on.set(bool(self.model.limit))
        self.match_var.set(self.model.match)
        self.offset_var.set(str(self.model.offset or 0))
        self.limit_var.set(str(self.model.limit or DEFAULT_LIMIT))
        self._scroll = {}
        self._changed()
        self.tidy()
        self.status.configure(text="Loaded '%s'." % name)
        return True
