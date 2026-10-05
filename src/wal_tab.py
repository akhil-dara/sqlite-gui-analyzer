"""WAL tab: every frame of the write-ahead log, the records they hold, and how each record
compares with the database.

The tab is shown for every database whose -wal file exists, also when that file cannot be read
(it then says why, and that its frames are not applied). Frames: one line per frame with its
table, state, page type, records, checksum and transaction (commit); selecting one shows its
summary, its records (double-click opens one) and its bytes. Records: every record of the WAL
frames, for one table at a time (its own columns) or for all tables (a 'Values' column),
compared with the database's current row: same, different (which columns), not in the
database, a table only the WAL has, or 'could not compare' with the reason. The comparison
runs on a worker thread with progress and Stop.

Exports (frames, records, BLOBs) run on a worker thread with the provenance of engine.export.
"""

import tkinter as tk
from collections import OrderedDict
from tkinter import filedialog, messagebox, ttk

from browse_sources import ListSource
from combobox import SearchableCombobox
from constants import C, VERSION, WAL_STATES, wal_state_label
from tokens import COLOR as K, FONT as F
from database import RID
from engine.tags import entry_from_wal_record
from grid import DataGrid, Runner, grid_search
from hexview import HexView
from utils import export_row_blobs, fmtb, vb, write_allowed
from widgets import FlowFrame, SearchBox, ToolTip, TreeFilter, fit_geometry, wrap_to_width

TABLE_LEAF, INDEX_LEAF, INDEX_INTERIOR = 0x0D, 0x0A, 0x02
ALL = "All"
SHOW_CHOICES = (("all", "All"), ("different", "Different from DB"),
                ("not_in_db", "Not in DB (WAL only)"), ("wal_table", "Tables only in the WAL"),
                ("same", "Same as DB"), ("error", "Could not compare"))
DIFF_MARKS = {"same": "✓ same", "different": "≠ different", "not_in_db": "∅ not in DB",
              "wal_table": "★ WAL-only table", "error": "? could not compare"}
PREVIEW_CHARS = 200


def same_value(a, b):
    """Whether two stored values are the same: 1 and 1.0 are the same number (as in SQLite),
    text never equals bytes, and bytes compare as bytes (never as their text)."""
    if a is None or b is None:
        return a is None and b is None
    num = (int, float)
    if isinstance(a, num) and isinstance(b, num) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        return a == b
    if isinstance(a, (bytes, bytearray)) or isinstance(b, (bytes, bytearray)):
        return isinstance(a, (bytes, bytearray)) and isinstance(b, (bytes, bytearray)) and \
            type(a) is type(b) and bytes(a) == bytes(b)
    return type(a) is type(b) and a == b


def compare_with_db(db, table, locator, columns, values, db_tables, wal_only):
    """(status, differing columns, db values {column: value} or None, reason) of one WAL
    record against the database's current row. status: same, different, not_in_db, wal_table
    or error (reason says why)."""
    if table not in db_tables:
        return ("wal_table" if table in wal_only else "not_in_db"), set(columns), None, \
            "the table is not in the database"
    try:
        row = db.session.row(table, locator)
    except Exception as e:              # noqa: BLE001 - said, never taken for 'not in DB'
        return "error", set(), None, "the database's row could not be read: %s" % e
    if row is None:
        return "not_in_db", set(columns), None, "no row with this key in the database now"
    snap = row.locator.snapshot
    cols = list(snap[0]) if snap is not None else db.session.visible_columns(table)
    now = dict(zip(cols, row.values))
    diff = set()
    for c, v in zip(columns, values):
        if c in now and not same_value(v, now[c]):
            diff.add(c)
        elif c not in now:
            diff.add(c)
    return ("different" if diff else "same"), diff, now, ""


def values_preview(columns, values, limit=PREVIEW_CHARS):
    parts = []
    for c, v in zip(columns, values):
        parts.append("%s=%s" % (c, vb(v)))
        if sum(len(p) + 2 for p in parts) > limit:
            break
    text = ", ".join(parts)
    return text if len(text) <= limit else text[:limit] + "…"


class WalTab(ttk.Frame):
    """The WAL tab of App (app gives db, the record window, tags and exports)."""

    def __init__(self, parent, app):
        ttk.Frame.__init__(self, parent)
        self.app = app
        self._runner = Runner(self, "wal-records", release=app._release_worker_connection)
        self._stop = [False]
        self._records = []               # [(rec, status, diff cols, reason)] of the last load
        self._view = []                  # the records the grid lists now
        self._frames = []                # frames the list shows (filters, sort applied)
        self._sort = ("index", False)
        self._frame_records = []         # records of the selected frame
        self._rec_counts = None          # {frame index: records on its page}, once counted
        self._build()

    @property
    def db(self):
        return self.app.db

    # -- layout ---------------------------------------------------------------------------------
    def _build(self):
        head = FlowFrame(self)
        head.pack(fill="x", padx=10, pady=(8, 2))
        head.add(ttk.Label(head, text="WAL (write-ahead log)", style="B.TLabel"))
        self.summary = wrap_to_width(ttk.Label(self, text="", style="M.TLabel"))
        self.summary.pack(fill="x", padx=10)
        # the WAL file could not be read: said here, the rest of the tab is empty
        self.problem = wrap_to_width(tk.Label(self, text="", bg=C["rl"], fg=C["red"],
                                              anchor="w", justify="left",
                                              font=F["body"]), pad=24)

        # per-table statistics, collapsed
        self.stats_btn = ttk.Button(self, text="▶ Per-table statistics",
                                    command=self._toggle_stats)
        self.stats_btn.pack(anchor="w", padx=10, pady=(2, 0))
        self.stats_box = ttk.Frame(self)
        self.stats_open = False

        bar = self.bar_row = FlowFrame(self)
        bar.pack(fill="x", padx=10, pady=(4, 2))
        self.view_var = tk.StringVar(value="frames")
        bar.add(ttk.Radiobutton(bar, text="Frames", value="frames", variable=self.view_var,
                                style="Toolbutton", command=self._switch_view))
        bar.add(ttk.Radiobutton(bar, text="Records", value="records", variable=self.view_var,
                                style="Toolbutton", command=self._switch_view), gap=1)
        bar.add(ttk.Label(bar, text="Status:"), gap=12)
        self.status_var = tk.StringVar(value=ALL)
        self.status_combo = bar.add(SearchableCombobox(
            bar, textvariable=self.status_var, state="readonly", width=12,
            values=[ALL] + [v[0] for v in WAL_STATES.values()]), gap=2)
        self.status_combo.bind("<<ComboboxSelected>>", lambda e: self._filters_changed())
        ToolTip(self.status_combo, "Frame states:\n" + "\n".join(
            "  %s: %s" % (v[0], v[3]) for v in WAL_STATES.values()))
        bar.add(ttk.Label(bar, text="Table:"), gap=8)
        self.table_var = tk.StringVar(value=ALL)
        self.table_combo = bar.add(SearchableCombobox(bar, textvariable=self.table_var,
                                                state="readonly", width=24, values=[ALL]),
                                   gap=2)
        self.table_combo.bind("<<ComboboxSelected>>", lambda e: self._filters_changed())
        bar.add(ttk.Label(bar, text="Page type:"), gap=8)
        self.type_var = tk.StringVar(value=ALL)
        self.type_combo = bar.add(SearchableCombobox(bar, textvariable=self.type_var,
                                               state="readonly", width=14, values=[ALL]), gap=2)
        self.type_combo.bind("<<ComboboxSelected>>", lambda e: self._filters_changed())
        bar.add(ttk.Label(bar, text="Page #:"), gap=8)
        self.page_var = tk.StringVar()
        self.page_entry = bar.add(ttk.Entry(bar, textvariable=self.page_var, width=7), gap=2)
        self.page_var.trace_add("write", lambda *_a: self._filters_changed())
        self.page_note = bar.add(ttk.Label(bar, text="", foreground=C["red"]), gap=2)
        self.export_btn = bar.add(ttk.Button(bar, text="Export ▾"), gap=12)
        self.export_menu = tk.Menu(self, tearoff=0)
        self.export_menu.add_command(label="Frames (CSV/JSON)…", command=self.export_frames)
        self.export_menu.add_command(label="Records (CSV/JSON)…", command=self.export_records)
        self.export_menu.add_command(label="BLOBs as files…", command=self.export_blobs)
        self.export_btn.configure(command=lambda: self.app._post_menu(self.export_menu,
                                                                      self.export_btn))
        bar.add(ttk.Button(bar, text="Technical details", command=self.show_header), gap=4)

        # frames view: the list, and the selected frame below
        self.frames_pane = ttk.PanedWindow(self, orient="vertical")
        top = ttk.Frame(self.frames_pane)
        self.frames_pane.add(top, weight=3)
        cols = ("frame", "table", "status", "type", "records", "checksum", "commit")
        self.frames_search = SearchBox(top, placeholder="Find frames (table, status, page "
                                                        "type…)", delay=150, find_button=False,
                                       width=30)
        self.frames_search.pack(fill="x", pady=(0, 2))
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse")
        for c, text, w, st in (("frame", "Frame", 60, False), ("table", "Table", 240, True),
                               ("status", "Status", 100, False),
                               ("type", "Page type", 110, False),
                               ("records", "Records", 70, False),
                               ("checksum", "Checksum", 80, False),
                               ("commit", "Transaction", 110, False)):
            self.tree.heading(c, text=text, command=lambda c=c: self._sort_by(c))
            self.tree.column(c, width=w, minwidth=40, stretch=st)
        ysb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.frames_filter = TreeFilter(self.tree, self.frames_search, "frame", "frames")
        self.tree.bind("<<TreeviewSelect>>", self._on_frame)
        for state, (_l, fg, bg, _d) in WAL_STATES.items():
            self.tree.tag_configure(state, foreground=fg, background=bg)
        low = ttk.Notebook(self.frames_pane)
        self.frames_pane.add(low, weight=2)
        self.detail_nb = low
        info = ttk.Frame(low)
        low.add(info, text="  Summary  ")
        self.info = tk.Text(info, wrap="word", height=8, bg=C["bg2"], fg=C["text"],
                            font=F["mono_large"], state="disabled")
        self.info.pack(fill="both", expand=True)
        recs = ttk.Frame(low)
        low.add(recs, text="  Records on the page  ")
        self.frame_note = ttk.Label(recs, text="", style="M.TLabel")
        self.frame_note.pack(fill="x")
        self.frame_grid = DataGrid(recs, frozen=1, on_open_row=self._open_frame_record,
                                   on_context_menu=self._frame_menu)
        grid_search(recs, self.frame_grid, placeholder="Find records on the page…").pack(
            fill="x", pady=(2, 2))
        self.frame_grid.pack(fill="both", expand=True)
        hexf = ttk.Frame(low)
        low.add(hexf, text="  Page bytes  ")
        hb = ttk.Frame(hexf)
        hb.pack(fill="x")
        ttk.Button(hb, text="Copy hex", command=self._copy_hex).pack(side="left", padx=4, pady=2)
        ttk.Button(hb, text="Copy base64", command=self._copy_b64).pack(side="left", padx=4)
        self.hex = HexView(hexf)
        self.hex.pack(fill="both", expand=True)

        # records view
        self.records_box = ttk.Frame(self)
        rbar = self._rec_rbar = FlowFrame(self.records_box)
        rbar.pack(fill="x", pady=(0, 2))
        rbar.add(ttk.Label(rbar, text="Show:"))
        self.show_var = tk.StringVar(value=SHOW_CHOICES[0][1])
        self.show_combo = rbar.add(SearchableCombobox(rbar, textvariable=self.show_var,
                                                state="readonly", width=22,
                                                values=[t for _k, t in SHOW_CHOICES]), gap=2)
        self.show_combo.bind("<<ComboboxSelected>>", lambda e: self._show_records())
        rbar.add(ttk.Button(rbar, text="Compare with the database", style="P.TButton",
                            command=self.load_records), gap=10)
        # Stop is only shown while a comparison runs; an idle Stop button is misleading.
        self._rec_stop_btn = rbar.add(ttk.Button(rbar, text="Stop", style="D.TButton",
                                                  command=self.stop), gap=4, visible=False)
        self.rec_bar = rbar.add(ttk.Progressbar(rbar, mode="determinate", length=140), gap=6)
        self.rec_status = wrap_to_width(ttk.Label(self.records_box, text="", style="M.TLabel"))
        self.rec_status.pack(fill="x")
        self.rec_grid = DataGrid(self.records_box, frozen=1, on_open_row=self._open_record,
                                 on_context_menu=self._record_menu)
        self.rec_search = rbar.add(grid_search(rbar, self.rec_grid, width=26,
                                               placeholder="Find records…"), gap=10,
                                   stretch=True)
        self.rec_grid.pack(fill="both", expand=True)
        self.frames_pane.pack(fill="both", expand=True, padx=10, pady=(2, 10))

    # -- lifecycle ------------------------------------------------------------------------------
    def reset(self):
        self.stop()
        self._records, self._view, self._frames, self._frame_records = [], [], [], []
        self.tree.delete(*self.tree.get_children())
        self.rec_grid.set_source(None)
        self.frame_grid.set_source(None)
        self.hex.set_data(b"")
        self._set_info("")
        self.summary.configure(text="")
        self.rec_status.configure(text="")

    def stop(self):
        self._stop[0] = True
        getattr(self, "_count_stop", [False])[0] = True
        self._runner.cancel()
        self._rec_rbar.show(self._rec_stop_btn, False)

    def worker_threads(self):
        return self._runner.threads()

    def on_open(self):
        """Show the active database's WAL (or why it cannot be read)."""
        self.reset()
        db = self.db
        problem = db.session.wal_problem if db.ok else None
        if problem or not db.has_wal:
            path = db.evidence.path("wal") if db.ok else None
            self.problem.configure(text=(
                "The WAL file could not be read: %s. Its frames are NOT applied, so tables may "
                "show an older state than the last commit, and no WAL frames are listed here. "
                "The file itself is untouched and listed with its SHA-256 in Evidence.%s"
                % (problem or "no frames", "\n%s" % path if path else "")))
            if not self.problem.winfo_manager():
                self.problem.pack(fill="x", padx=10, pady=(4, 2), after=self.summary)
            for w in (self.stats_btn, self.bar_row, self.frames_pane, self.records_box):
                w.pack_forget()
            return
        if self.problem.winfo_manager():
            self.problem.pack_forget()
        self.stats_btn.pack(anchor="w", padx=10, pady=(2, 0), after=self.summary)
        self.bar_row.pack(fill="x", padx=10, pady=(4, 2), after=self.stats_btn)
        self._switch_view()
        wp = db.wal
        ws = wp.summary()
        parts = ["%s frames" % format(ws["total_frames"], ",")]
        for state, (label, _fg, _bg, _desc) in WAL_STATES.items():
            if ws.get(state):
                parts.append("%s %s" % (format(ws[state], ","), label.lower()))
        parts.append("%s commits" % format(ws.get("commits", 0), ","))
        if ws.get("checksum_failures"):
            parts.append("%d checksum failures" % ws["checksum_failures"])
        parts.append("WAL size %s, page size %s bytes" % (fmtb(ws["wal_size"]), ws["page_size"]))
        self.summary.configure(text="  |  ".join(parts))
        self.type_combo.configure(values=[ALL] + sorted(ws.get("page_types", {}).keys()))
        wal_only = set(self._wal_only())
        names = set()
        for f in wp.frames:
            t = wp.page_map.get(f.page_num)
            if t and t not in ("sqlite_master", "sqlite_sequence") and not t.startswith("page_"):
                names.add(t)
        self.table_combo.configure(values=[ALL] + [
            ("★ %s (WAL-only)" % t) if t in wal_only else t for t in sorted(names)])
        self._fill_stats()
        self._start_frame_counts()
        self._filters_changed()

    def _start_frame_counts(self):
        """Count the records (cells) on every frame's page on the worker thread (a large WAL
        has many pages to read); the frame list shows '…' until then and is filled again."""
        db, wp = self.db, self.db.wal
        self._rec_counts = None
        flag = self._count_stop = [False]

        def work():
            out = {}
            for f in wp.frames:
                if flag[0]:
                    return None
                out[f.index] = self._count_cells(wp, f)
            return out

        def done(result, error):
            if error is not None or result is None or self.db is not db or \
                    self.db.wal is not wp:
                return
            self._rec_counts = result
            if self.view_var.get() != "records":
                self._fill_frames()
        self._runner.submit("frame-counts", work, done)

    def _wal_only(self):
        try:
            return self.db.wal_tables()
        except Exception:               # noqa: BLE001 - none then
            return []

    # -- filters --------------------------------------------------------------------------------
    def _table_filter(self):
        t = self.table_var.get()
        if t.startswith("★ "):
            t = t[2:].rsplit(" (WAL-only)", 1)[0]
        return None if t == ALL else t

    def _status_filter(self):
        label = self.status_var.get()
        for key, v in WAL_STATES.items():
            if v[0] == label:
                return key
        return None

    def _page_filter(self):
        """(page number or None, error text)."""
        text = self.page_var.get().strip()
        if not text:
            return None, ""
        try:
            n = int(text)
        except ValueError:
            return None, "not a page number"
        return (n, "") if n > 0 else (None, "not a page number")

    def _filters_changed(self):
        page, err = self._page_filter()
        self.page_note.configure(text=err)
        try:
            self.page_entry.configure(foreground=C["red"] if err else C["text"])
        except tk.TclError:
            pass
        if self.view_var.get() == "records":
            self._show_records()
            return
        if not self.db.has_wal:
            return
        wp = self.db.wal
        table, state, ptype = self._table_filter(), self._status_filter(), self.type_var.get()
        frames = []
        for f in wp.frames:
            if table is not None and wp.page_map.get(f.page_num, "page_%d" % f.page_num) != table:
                continue
            if state is not None and f.category != state:
                continue
            if ptype != ALL and f.page_type != ptype:
                continue
            if err or (page is not None and f.page_num != page):
                if err:
                    frames = []
                    break
                continue
            frames.append(f)
        self._frames = frames
        self._fill_frames()

    # -- frames ---------------------------------------------------------------------------------
    def _records_in(self, f):
        """Records (cells) on a frame's page (counted on the worker; None until then or for a
        page that holds none)."""
        counts = self._rec_counts
        if counts is None:
            return None
        if f.index not in counts:           # e.g. an export asking for one not counted yet
            counts[f.index] = self._count_cells(self.db.wal, f)
        return counts[f.index]

    @staticmethod
    def _count_cells(wp, f):
        """The cell count of a b-tree page's header (None for other pages)."""
        if f.page_type_byte not in (TABLE_LEAF, INDEX_LEAF, INDEX_INTERIOR, 0x05):
            return None
        data = wp.get_page_data(f.index)
        off = 100 if f.page_num == 1 else 0
        return int.from_bytes(data[off + 3:off + 5], "big") if len(data) >= off + 5 else None

    @staticmethod
    def _checksum(f):
        return "ok" if f.checksum_ok else ("BAD" if f.checksum_ok is False else "n/a")

    @staticmethod
    def _commit(f):
        if f.commit_group is not None:
            return "commit %d" % (f.commit_group + 1)
        return wal_state_label(f.category).lower()

    def _sort_by(self, col):
        key, desc = self._sort
        self._sort = (col, not desc if key == col else False)
        self._fill_frames()

    def _fill_frames(self):
        wp = self.db.wal
        col, desc = self._sort
        keys = {"frame": lambda f: f.index,
                "table": lambda f: wp.page_map.get(f.page_num, "page_%d" % f.page_num),
                "status": lambda f: f.category, "type": lambda f: f.page_type,
                "records": lambda f: self._records_in(f) or -1,
                "checksum": lambda f: self._checksum(f),
                "commit": lambda f: (f.commit_group is None, f.commit_group or 0, f.index)}
        frames = sorted(self._frames, key=keys.get(col, keys["frame"]), reverse=desc)
        tree = self.tree
        tree.delete(*tree.get_children())
        counting = self._rec_counts is None
        for f in frames:
            n = self._records_in(f)
            tree.insert("", "end", iid=str(f.index), tags=(f.category,), values=(
                f.index, wp.page_map.get(f.page_num, "page_%d" % f.page_num),
                wal_state_label(f.category), f.page_type,
                "…" if counting else ("" if n is None else n),
                self._checksum(f), self._commit(f)))

    def navigate(self, frame_idx):
        """Show one frame (from a search result or a record window): filters cleared."""
        if not self.db.has_wal:
            return
        self.view_var.set("frames")
        self._switch_view()
        for var in (self.status_var, self.table_var, self.type_var):
            var.set(ALL)
        self.page_var.set("")
        self._filters_changed()
        iid = str(frame_idx)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
            self._on_frame()

    def _on_frame(self, _e=None):
        sel = self.tree.selection()
        if not sel or not self.db.has_wal:
            return
        wp = self.db.wal
        f = wp.frames[int(sel[0])]
        data = wp.get_page_data(f.index)
        self._selected_data = data
        table = wp.page_map.get(f.page_num, "page_%d" % f.page_num)
        n = self._records_in(f)
        lines = ["Frame %d  —  table %s" % (f.index, table), "",
                 "Status:       %s — %s" % (wal_state_label(f.category),
                                            WAL_STATES.get(f.category, ("", "", "", ""))[3]),
                 "Transaction:  %s%s" % (self._commit(f), " (this frame ends the commit)"
                                         if f.commit_size else ""),
                 "Checksum:     %s" % ("valid" if f.checksum_ok else
                                       "INVALID (the chain breaks here)" if f.checksum_ok is False
                                       else "not verifiable (after a break / other salt)"),
                 "Page:         %d (%s, %s bytes)" % (f.page_num, f.page_type,
                                                      format(len(data), ",")),
                 "Records:      %s" % ("-" if n is None else n),
                 "Salts:        0x%08X 0x%08X" % (f.salt1, f.salt2)]
        known = wp.col_map.get(table, [])
        if known:
            lines.append("Columns:      %s" % ", ".join(known))
        self._set_info("\n".join(lines))
        self.hex.set_data(bytes(data))
        # the records of the page, with their columns
        recs = [r for r in self._frame_records_of(f)]
        self._frame_records = recs
        cols = list(recs[0]["values_dict"].keys()) if recs else []
        same = all(list(r["values_dict"].keys()) == cols for r in recs)
        if not same:
            cols = []
        rows = []
        for i, r in enumerate(recs):
            vals = list(r["raw_values"])
            if cols:
                rows.append(([i + 1, r["rowid"]] + vals, r.get("flags") or ()))
            else:
                rows.append(([i + 1, r["rowid"], values_preview(
                    list(r["values_dict"].keys()), vals)], r.get("flags") or ()))
        head = [RID, "Row"] + (cols if cols else ["Values"])
        self.frame_grid.set_source(ListSource(head, rows, encoding=self.db.encoding))
        self.frame_note.configure(text=(
            "%d record(s) of %s on this page copy; double-click opens one, right-click tags it."
            % (len(recs), table)) if recs else
            "No records: a %s page holds no rows (index or tree pages, overflow or free pages)."
            % f.page_type)

    def _frame_records_of(self, f):
        try:
            return self.db.wal.records_of_frame(f.index)
        except Exception as e:          # noqa: BLE001 - said in the note
            self.frame_note.configure(text="The page's records could not be read: %s" % e)
            return []

    def _set_info(self, text):
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", text)
        self.info.configure(state="disabled")

    def _copy_hex(self):
        data = getattr(self, "_selected_data", None)
        if data:
            self.clipboard_clear()
            self.clipboard_append(bytes(data).hex().upper())

    def _copy_b64(self):
        import base64
        data = getattr(self, "_selected_data", None)
        if data:
            self.clipboard_clear()
            self.clipboard_append(base64.b64encode(bytes(data)).decode("ascii"))

    def _frame_record(self, values):
        try:
            return self._frame_records[int(values[0]) - 1]
        except (IndexError, TypeError, ValueError):
            return None

    def _open_frame_record(self, _row, values):
        rec = self._frame_record(values)
        if rec is not None:
            open_wal_record(self.app, rec)

    def _frame_menu(self, menu, row, _col):
        data = self.frame_grid.row_data(row)
        rec = self._frame_record(data[0]) if data else None
        if rec is not None:
            self._record_items(menu, rec)

    def _record_items(self, menu, rec):
        menu.add_separator()
        menu.add_command(label="Open row detail", command=lambda: open_wal_record(self.app, rec))
        loc = rec.get("locator")
        if getattr(loc, "kind", None) in ("rowid", "pk") and rec["table"] in self.db.tables():
            menu.add_command(label="Row history (every version)",
                             command=lambda: self.app.show_row_history(rec["table"], loc))
        self.app.tag_menu(menu, lambda: [entry_from_wal_record(rec)], label="Tag record")

    # -- per-table statistics -------------------------------------------------------------------
    def _toggle_stats(self):
        self.stats_open = not self.stats_open
        if self.stats_open:
            self.stats_box.pack(fill="x", padx=10, pady=(2, 4), after=self.stats_btn)
        else:
            self.stats_box.pack_forget()
        self.stats_btn.configure(text=("▼" if self.stats_open else "▶") +
                                 " Per-table statistics")

    def _fill_stats(self):
        for w in self.stats_box.winfo_children():
            w.destroy()
        try:
            stats = self.db.wal.table_stats()
        except Exception as e:          # noqa: BLE001 - said, not hidden
            ttk.Label(self.stats_box, text="Statistics could not be computed: %s" % e,
                      style="M.TLabel").pack(anchor="w")
            self.app.db.session.issues.add("wal_stats_failed", str(e), "WAL", "warning")
            return
        if not stats:
            ttk.Label(self.stats_box, text="No table rows in the WAL frames.",
                      style="M.TLabel").pack(anchor="w")
            return
        wal_only = set(self._wal_only())
        labels = tuple(v[0] for v in WAL_STATES.values())
        cols = ("Table", "Records") + labels + ("Frames", "Pages", "Notes")
        find = SearchBox(self.stats_box, placeholder="Find a table…", delay=100, width=24)
        find.pack(side="top", fill="x", pady=(0, 2))
        self.stats_find = find
        tree = ttk.Treeview(self.stats_box, columns=cols, show="headings",
                            height=min(len(stats) + 1, 8))
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=70 if c not in ("Table", "Notes") else 180,
                        stretch=c in ("Table", "Notes"))
        sb = ttk.Scrollbar(self.stats_box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="x", expand=True)
        for name, s in sorted(stats.items()):
            notes = []
            if name in wal_only:
                notes.append("WAL-only table")
            if s["uncommitted"]:
                notes.append("uncommitted data")
            if s["superseded"] + s["stale"]:
                notes.append("older versions")
            tree.insert("", "end", values=(name, s["total_records"],
                                           *[s[st] for st in WAL_STATES],
                                           s["frames"], len(s["pages"]), "; ".join(notes)))
        self.stats_tree = tree
        self.stats_filter = TreeFilter(tree, find, "table", "tables")

    # -- records view ---------------------------------------------------------------------------
    def _switch_view(self):
        if self.view_var.get() == "records":
            self.frames_pane.pack_forget()
            self.records_box.pack(fill="both", expand=True, padx=10, pady=(2, 10))
            self._show_records()
        else:
            self.records_box.pack_forget()
            self.frames_pane.pack(fill="both", expand=True, padx=10, pady=(2, 10))

    def load_records(self):
        """Read every record of the WAL frames (the Status / Table filters apply) and compare
        each with the database's current row, on a worker thread (progress, Stop)."""
        if not self.db.has_wal:
            return
        db = self.db
        table, state = self._table_filter(), self._status_filter()
        flag = self._stop = [False]
        progress = [0, max(1, len(db.wal.frames))]
        wal_only = set(self._wal_only())
        db_tables = set(db.tables())

        def work():
            out = []
            seen_frames = set()
            for rec in db.wal.recover_all_records(table_filter=table, category_filter=state,
                                                  cancel=lambda: flag[0]):
                if flag[0]:
                    break
                seen_frames.add(rec["frame_idx"])
                progress[0] = len(seen_frames)
                cols = list(rec["values_dict"].keys())
                status, diff, _now, reason = compare_with_db(
                    db, rec["table"], rec["locator"], cols, rec["raw_values"], db_tables,
                    wal_only)
                out.append((rec, status, diff, reason))
            return out, flag[0]

        def done(result, error):
            self._poll_bar(stop=True)
            self._rec_rbar.show(self._rec_stop_btn, False)
            if error is not None:
                self.rec_status.configure(text="Could not read the records: %s" % error)
                return
            self._records, stopped = result
            self._loaded_filters = (table, state)
            self._show_records(stopped)
        self.rec_status.configure(text="Reading and comparing the WAL records…")
        self._bar_progress = progress
        self._runner.submit("records", work, done)
        self._rec_rbar.show(self._rec_stop_btn, True)
        self._poll_bar()

    def _poll_bar(self, stop=False):
        aid = getattr(self, "_bar_after", None)
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass
            self._bar_after = None
        if stop:
            self.rec_bar.configure(value=0)
            return
        done, total = getattr(self, "_bar_progress", (0, 1))
        self.rec_bar.configure(maximum=total, value=done)
        self._bar_after = self.after(200, self._poll_bar)

    def destroy(self):
        self._poll_bar(stop=True)
        ttk.Frame.destroy(self)

    def _show_records(self, stopped=False):
        show = dict((t, k) for k, t in SHOW_CHOICES).get(self.show_var.get(), "all")
        page, err = self._page_filter()
        loaded = getattr(self, "_loaded_filters", None)
        lt, ls = loaded if loaded else (None, None)
        # the Table and Status filters apply to the compared records at once; asking for more
        # than was compared says so (Compare again)
        table, state = self._table_filter(), self._status_filter()
        wider = bool(self._records) and ((lt is not None and table != lt) or
                                         (ls is not None and state != ls))
        recs = [x for x in self._records
                if (show == "all" or x[1] == show)
                and (table is None or x[0]["table"] == table)
                and (state is None or x[0]["category"] == state)
                and (page is None or x[0]["page_num"] == page) and not err]
        self._view = recs
        # one table: its own columns; all tables: a 'Values' column (never another table's
        # headers over a record). The Note (why a record could not be compared) is always
        # there.
        cols = None
        if table is not None and recs:
            cols = list(recs[0][0]["values_dict"].keys())
            if any(list(r["values_dict"].keys()) != cols for r, _s, _d, _w in recs):
                cols = None
        rows = []
        for i, (rec, status, diff, reason) in enumerate(recs):
            mark = DIFF_MARKS.get(status, status)
            if status == "different":
                mark = "≠ %d differ" % len(diff)
            head = [i + 1, mark, rec["table"], rec["rowid"], wal_state_label(rec["category"]),
                    rec["frame_idx"], rec["page_num"]]
            if cols is not None:
                rows.append((head + list(rec["raw_values"]) + [reason],
                             rec.get("flags") or ()))
            else:
                rows.append((head + [values_preview(list(rec["values_dict"].keys()),
                                                    rec["raw_values"]), reason],
                             rec.get("flags") or ()))
        names = [RID, "Compared with DB", "Table", "Row", "Status", "Frame", "Page"] + (
            cols + ["Note"] if cols is not None else ["Values", "Note"])
        self.rec_grid.set_source(ListSource(names, rows,
                                            encoding=self.db.encoding if self.db.ok else "utf-8"))
        counts = OrderedDict((k, 0) for k, _t in SHOW_CHOICES[1:])
        for _r, st, _d, _w in self._records:
            counts[st] = counts.get(st, 0) + 1
        if not self._records:
            text = "Press 'Compare with the database' to read every record of the WAL frames " \
                   "(the Status and Table filters apply) and compare it with the database."
        else:
            text = "%s records compared%s: %s. Listed: %s%s." % (
                format(len(self._records), ","),
                (" (of %s)" % lt) if lt else " (of all tables)" + (
                    "" if table else "; one 'Values' column: choose a table to see its own "
                                     "columns"),
                ", ".join("%s %s" % (format(n, ","), dict(SHOW_CHOICES)[k].lower())
                          for k, n in counts.items() if n),
                format(len(recs), ","), " — STOPPED before the end" if stopped else "")
            if wider:
                text += (" The Table or Status filter now asks for records that were not "
                         "compared (compared: %s, %s): press 'Compare with the database' "
                         "again." % (lt or "all tables", wal_state_label(ls) if ls
                                     else "every status"))
        self.rec_status.configure(text=text)

    def _record(self, values):
        try:
            return self._view[int(values[0]) - 1]
        except (IndexError, TypeError, ValueError):
            return None

    def _open_record(self, _row, values):
        x = self._record(values)
        if x is not None:
            open_wal_record(self.app, x[0])

    def _record_menu(self, menu, row, _col):
        data = self.rec_grid.row_data(row)
        x = self._record(data[0]) if data else None
        if x is not None:
            self._record_items(menu, x[0])

    # -- exports --------------------------------------------------------------------------------
    def export_frames(self):
        if not self.db.has_wal:
            return
        from jobs import ask_path, export_options, export_rows
        wp = self.db.wal
        frames = list(self._frames) or list(wp.frames)
        opts = export_options(self.app, "Export WAL frames",
                              [("shown", "The frames listed (%s)" % format(len(self._frames), ",")),
                               ("all", "Every frame (%s)" % format(len(wp.frames), ","))],
                              blobs=False, spreadsheet_safe=True)
        if opts is None:
            return
        if opts["scope"] == "all":
            frames = list(wp.frames)
        path = ask_path(self.app, opts["fmt"], "wal_frames")
        if not write_allowed(path):
            return
        cols = ["Frame", "Page", "Table", "Status", "Page type", "Records", "Checksum",
                "Transaction", "Commit size", "Salt1", "Salt2", "Offset"]

        def rows():
            for f in frames:
                yield [f.index, f.page_num, wp.page_map.get(f.page_num, "page_%d" % f.page_num),
                       wal_state_label(f.category), f.page_type, self._records_in(f),
                       self._checksum(f), self._commit(f), f.commit_size,
                       "0x%08X" % f.salt1, "0x%08X" % f.salt2, f.offset]
        export_rows(self.app, "Export WAL frames", path, opts["fmt"], cols, rows,
                    "WAL frames of %s" % wp.path, [self.app.case.active],
                    scope=opts["scope"], total=len(frames),
                    spreadsheet_safe=opts.get("spreadsheet_safe", True))

    def export_records(self):
        if not self.db.has_wal:
            return
        from jobs import ask_path, export_options, export_rows
        scopes = []
        if self._view:
            scopes.append(("listed", "The records listed in Records (%s, with their comparison)"
                           % format(len(self._view), ",")))
        scopes.append(("all", "Every record of every WAL frame (not compared with the "
                              "database)"))
        opts = export_options(self.app, "Export WAL records", scopes, spreadsheet_safe=True)
        if opts is None:
            return
        path = ask_path(self.app, opts["fmt"], "wal_records")
        if not write_allowed(path):
            return
        wp = self.db.wal
        listed = list(self._view) if opts["scope"] == "listed" else None
        cols = ["Table", "Row", "Status", "Frame", "Page", "Compared with DB", "Differing columns",
                "Note", "Columns", "Values"]

        def rows():
            src = listed if listed is not None else (
                (r, "not compared", set(), "exported without comparing (Compare with the "
                                           "database, then export the records listed)")
                for r in wp.recover_all_records())
            for rec, status, diff, reason in src:
                yield [rec["table"], rec["locator"], wal_state_label(rec["category"]),
                       rec["frame_idx"], rec["page_num"], status, ", ".join(sorted(diff)),
                       reason, list(rec["values_dict"].keys()), list(rec["raw_values"])]
        export_rows(self.app, "Export WAL records", path, opts["fmt"], cols, rows,
                    "records of the WAL frames of %s" % wp.path, [self.app.case.active],
                    scope=dict(scopes)[opts["scope"]], blob_mode=opts["blob_mode"],
                    total=len(listed) if listed is not None else None,
                    spreadsheet_safe=opts.get("spreadsheet_safe", True))

    def export_blobs(self):
        if not self.db.has_wal:
            return
        from jobs import Job, blob_export_done, write_export_manifest
        folder = filedialog.askdirectory(title="Choose a folder for the WAL BLOBs",
                                         parent=self.app)
        if not write_allowed(folder):
            return
        wp = self.db.wal
        member = self.app.case.active

        def work(job):
            files, count, errors, first = [], 0, 0, ""
            for rec in wp.recover_all_records(cancel=lambda: job.cancelled):
                if job.cancelled:
                    break
                n_ok, n_err, f1 = export_row_blobs(
                    folder, "%s_f%d" % (rec["table"], rec["frame_idx"]),
                    ["_rid"] + list(rec["values_dict"].keys()),
                    [[rec["locator"]] + list(rec["raw_values"])], files=files)
                count, errors, first = count + n_ok, errors + n_err, first or f1
                job.done += 1
                job.status = "%s BLOBs written (%s records read)" % (format(count, ","),
                                                                      format(job.done, ","))
            manifest = write_export_manifest(self.app, folder, "BLOBs of the WAL frames of %s"
                                           % wp.path, [member], files, not job.cancelled)
            return count, errors, first, manifest

        def done(result, error, cancelled):
            blob_export_done(self.app, "BLOBs of the WAL", folder, result, error, cancelled)
        Job(self.app, "Export WAL BLOBs", work, done, unit="records", members=[member])

    # -- header ---------------------------------------------------------------------------------
    def show_header(self):
        if not self.db.has_wal:
            return
        from dialogs import TextWindow
        wp = self.db.wal
        h, s = wp.header, wp.summary()
        lines = ["WAL file: %s" % wp.path, "",
                 "Magic: 0x%08X (%s checksums)" % (h.magic, "big-endian"
                                                  if h.magic == 0x377f0683 else "little-endian"),
                 "Format version: %s" % h.version, "Page size: %s bytes" % format(h.page_size, ","),
                 "Checkpoint sequence: %s" % h.checkpoint_seq,
                 "Salt-1: 0x%08X (%d)" % (h.salt1, h.salt1),
                 "Salt-2: 0x%08X (%d)" % (h.salt2, h.salt2),
                 "Header checksum: 0x%08X 0x%08X (%s)" % (
                     h.checksum1, h.checksum2, "valid" if s.get("header_checksum_ok") else
                     "INVALID"), "",
                 "Frames: %s" % format(s["total_frames"], ",")]
        for k, v in WAL_STATES.items():
            lines.append("  %s: %s" % (v[0], format(s.get(k, 0), ",")))
        lines += ["Commits: %s" % s.get("commits", 0),
                  "Checksum failures: %s" % s.get("checksum_failures", 0),
                  "Pages changed: %s" % format(s["unique_pages"], ","),
                  "WAL size: %s" % fmtb(s["wal_size"]), "", "Page types:"]
        for pt, n in sorted(s.get("page_types", {}).items()):
            lines.append("  %s: %s frames" % (pt, format(n, ",")))
        TextWindow(self.app, "WAL technical details", "", "\n".join(lines), "560x520")


# -- the record window ------------------------------------------------------------------------
def open_wal_record(app, rec, match_col="", match_val=""):
    """The row detail of a WAL record dict (recover_all_records) or search hit."""
    cols = list((rec.get("values_dict") or rec.get("row_data") or {}).keys())
    values = list(rec.get("raw_values") or rec.get("row") or ())
    return WalRecordWindow(app, rec["table"], rec.get("rowid"), rec.get("category"),
                           rec.get("frame_idx"), rec.get("page_num"), rec.get("locator"),
                           cols, values, match_col, match_val)


class WalRecordWindow(tk.Toplevel):
    """One record of a WAL frame: every column's value in the WAL beside the database's
    current value, and whether they are the same. It says exactly what the database has:
    the same row, a row with other values (which columns), no such row, no such table, or
    that the database's row could not be read (and why) - a read error is never taken for
    'not in the database'."""

    def __init__(self, app, table, rowid, category, frame_idx, page_num, locator, columns,
                 values, match_col="", match_val=""):
        tk.Toplevel.__init__(self, app)
        self.app = app
        member = getattr(app.case, "active", None)
        self.title("Row detail (WAL) — %s, row %s" % (app.member_label(member, table), rowid))
        fit_geometry(self, 900, 620)
        self.configure(bg=C["bg"])
        self.transient(app)
        self.columns, self.values = list(columns), list(values)
        self.values += [None] * (len(self.columns) - len(self.values))
        self.table, self.rowid, self.locator = table, rowid, locator
        self.frame_idx, self.page_num, self.category = frame_idx, page_num, category
        db = app.db
        status, diff, now, reason = "error", set(), None, "no database"
        if db.ok:
            status, diff, now, reason = compare_with_db(
                db, table, locator, self.columns, self.values, set(db.tables()),
                set(db.wal_tables()) if db.has_wal else set())
            if locator is None:
                status, reason = "error", "the record has no key to look it up by"
        self.status, self.diff, self.now = status, diff, now
        _l, fg, bg, desc = WAL_STATES.get(category, ("", C["text"], C["bg2"], ""))
        head = tk.Frame(self, bg=bg, padx=10, pady=6)
        head.pack(fill="x")
        tk.Label(head, text="WAL frame %s (%s), page %s  |  table %s  |  row %s" % (
            frame_idx, wal_state_label(category) if category else "?", page_num, table, rowid),
            bg=bg, fg=fg, font=F["heading"], anchor="w").pack(fill="x")
        what = {"same": "The database has this row with the same values.",
                "different": "The database has this row now with other values in %d "
                             "column(s): %s (marked ≠)." % (len(diff), ", ".join(sorted(diff))),
                "not_in_db": "Not in the database now: %s. The WAL still holds it." % reason,
                "wal_table": "Its table exists only in the WAL (not in the database's "
                             "current schema).",
                "error": "Could not compare with the database: %s." % reason}.get(status, "")
        tk.Label(head, text=what + ("  (%s)" % desc if desc else ""), bg=bg, fg=C["text2"],
                 anchor="w", justify="left", wraplength=860,
                 font=F["body"]).pack(fill="x")
        if match_col:
            tk.Label(self, text="Matched in %s: %s" % (match_col, str(match_val)[:300]),
                     bg=C["bg"], fg=C["text2"], anchor="w", wraplength=860,
                     font=F["body"]).pack(fill="x", padx=10, pady=(4, 0))
        fbar = ttk.Frame(self)
        fbar.pack(fill="x", padx=8, pady=(6, 0))
        self.find = SearchBox(fbar, placeholder="Find a column or value…", delay=100,
                              primary=True, width=30)
        self.find.pack(side="left", fill="x", expand=True)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree = ttk.Treeview(box, columns=("col", "wal", "db", "same"), show="headings")
        for c, text, w in (("col", "Column", 160), ("wal", "Value in the WAL", 320),
                           ("db", "Value in the database now", 320), ("same", "", 70)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, stretch=c in ("wal", "db"))
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("diff", foreground=K["danger_text"])
        self.tree.tag_configure("match", background=C["hl"])
        for i, (c, v) in enumerate(zip(self.columns, self.values)):
            if now is not None and c in now:
                dbv, mark = vb(now[c]), ("same" if c not in diff else "≠")
            else:
                dbv, mark = "", ""
            tags = (("diff",) if c in diff and now is not None else ()) + \
                (("match",) if c == match_col else ())
            self.tree.insert("", "end", iid=str(i), tags=tags, values=(c, vb(v), dbv, mark))
        self.filter = TreeFilter(self.tree, self.find, "column", "columns")
        self.tree.bind("<Double-1>", self._open_value)
        ttk.Label(self, text="Double-click a BLOB to open it in the BLOB Inspector.",
                  style="M.TLabel").pack(anchor="w", padx=10)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Copy as JSON", command=self._copy_json).pack(side="left")
        if frame_idx is not None:
            ttk.Button(bar, text="Go to frame %s" % frame_idx,
                       command=lambda: (self.destroy(), app.show_wal_frame(frame_idx))).pack(
                side="left", padx=4)
        if getattr(locator, "kind", None) in ("rowid", "pk") and db.ok and \
                table in db.tables():
            ttk.Button(bar, text="Row history",
                       command=lambda: (self.destroy(), app.show_row_history(table, locator))
                       ).pack(side="left", padx=4)
        tag_btn = ttk.Button(bar, text="Tag ▾")
        tag_btn.configure(command=lambda: self._tag(tag_btn))
        tag_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")

    def _open_value(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        v = self.values[i] if i < len(self.values) else None
        if isinstance(v, (bytes, bytearray)):
            self.app.open_blob(bytes(v), self.columns[i], "%s.%s (WAL frame %s)" % (
                self.table, self.columns[i], self.frame_idx))

    def record(self):
        return {"table": self.table, "rowid": self.rowid, "locator": self.locator,
                "values_dict": OrderedDict((c, vb(v)) for c, v in zip(self.columns,
                                                                      self.values)),
                "raw_values": list(self.values), "flags": set(), "frame_idx": self.frame_idx,
                "page_num": self.page_num, "category": self.category}

    def _tag(self, button):
        menu = tk.Menu(self, tearoff=0)
        self.app.tag_menu(menu, lambda: [entry_from_wal_record(self.record())],
                          label="Tag this record")
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def _copy_json(self):
        import json
        from engine.export import json_cell
        d = OrderedDict([("tool", "SQLite GUI Analyzer %s" % VERSION),
                         ("source", "WAL frame %s (%s), page %s" % (
                             self.frame_idx, self.category, self.page_num)),
                         ("table", self.table), ("row", json_cell(self.locator)),
                         ("compared_with_database", self.status),
                         ("differing_columns", sorted(self.diff)),
                         ("values", OrderedDict((c, json_cell(v)) for c, v in zip(
                             self.columns, self.values)))])
        if self.now is not None:
            d["database_values"] = OrderedDict((c, json_cell(v)) for c, v in self.now.items())
        self.clipboard_clear()
        self.clipboard_append(json.dumps(d, indent=2, ensure_ascii=False, default=str))
