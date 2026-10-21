"""Forensics tab: deleted records, freed pages, row history, dropped tables, the rollback
journal and audit.

Everything here reads the evidence the same way as the rest of the tool (read-only, never
copied). Long jobs (carving, history, recovery, audit, reports) run on a worker thread;
results are handed back to the Tk thread, and each job can be stopped.

Freed Pages (shown only when the database has freed pages) lists the pages of the freelist and
the records still on them as the Recovered Records carver recovers them: one confidence scale,
with its reasons, everywhere (Search's 'Recovered records' uses the same carver).
"""

import re
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from browse_sources import ListSource
from combobox import SearchableCombobox
from constants import C, VERSION
from tokens import COLOR as K, FONT as F
from database import RID
from date_reading import date_reading
from engine import limits
from engine.search import value_type
from engine.fileformat.record import InvalidText
from engine.forensics import CARVE_SOURCES, CONFIDENCES
from engine.tags import entry_from_record
from grid import DataGrid, Runner, grid_search
from hexview import HexView
from recordui import (BytesWindow, RecordWindow, STATE_STYLE, record_state,  # noqa: F401
                      where_text)
from utils import fmtb, vb, write_allowed
from widgets import (FlowFrame, SearchBox, ToolTip, TreeFilter, TreeviewTooltip, fit_geometry,
                     menu_button, wrap_to_width)

SOURCE_LABELS = (("freeblock", "Freeblocks"), ("unallocated", "Unallocated space"),
                 ("freelist", "Freed pages"), ("orphan", "Unreferenced pages"),
                 ("wal", "WAL frames"), ("replaced", "Replaced pages"),
                 ("journal", "Rollback journal"))
MIN_CONFIDENCE = (("All", CONFIDENCES), ("Medium and high", ("high", "medium")), ("High", ("high",)))
TIME_LIMITS = (("1 minute", 60), ("5 minutes", 300), ("No limit", None))
LEVEL_COLOURS = {"error": (K["danger_soft"], K["danger_text"]),
                 "warning": (K["warning_soft"], K["warning"]),
                 "info": (K["primary_soft"], K["primary"])}
PREVIEW_CHARS = 160
HOVER_CHARS = 600            # characters of a value in the record panel's hover (the rest: Open…)
# why a recovery stopped before the end (engine.forensics.carve stats['stopped'])
STOP_REASONS = {"cancelled": "Stop was pressed",
                "time limit reached": "the time limit was reached",
                "record limit reached": "the limit carve_max_records (%s records) was reached"}
CONFIDENCE_TEXT = ("Confidence says how sure the match of a record to its table is: high (the "
                   "record's header and sizes fit the table exactly), medium (it fits, with "
                   "something rebuilt or guessed), low (bytes that decode, table uncertain). "
                   "Each record lists its reasons.")


STATE_TEXT = ("State: deleted (a row the table no longer has), older version (the row is live "
              "with other values), inserted again (deleted, and the same values are live "
              "under another rowid), unreferenced page (on a page nothing references: "
              "deleted, or a live row a damaged tree hides), index entry (a deleted entry of an index).")


def stop_text(reason):
    """Why a recovery stopped early, in words."""
    text = STOP_REASONS.get(reason, reason or "")
    return text % format(limits.get("carve_max_records"), ",") if "%s" in text else text


def live_check_text(stats):
    """What a recovery could not compare with the live rows (tables or indexes over the limits
    live_hash_rows / live_index_entries), in words; '' when everything was compared."""
    parts = []
    for key, what, name in (("live_check_skipped", "rows", "live_hash_rows"),
                            ("live_index_check_skipped", "entries", "live_index_entries")):
        names = stats.get(key) or ()
        if names:
            parts.append("%s of %s were not compared with the live ones (over %s %s: limit "
                         "%s), so copies of live %s and older versions are not told apart "
                         "there" % (what.capitalize(), ", ".join(names),
                                    format(limits.get(name), ","), what, name, what))
    return "".join(" — " + p for p in parts)


def values_preview(columns, values, limit=PREVIEW_CHARS):
    """'col=value, col=value, ...' for a grid cell."""
    parts = []
    for c, v in zip(columns, values):
        parts.append("%s=%s" % (c, vb(v)))
        if sum(len(p) + 2 for p in parts) > limit:
            break
    text = ", ".join(parts)
    return text if len(text) <= limit else text[:limit] + "…"


def parse_key(text):
    """Row id text typed by the user: an int for rowid tables, else the text itself."""
    s = text.strip()
    try:
        return int(s)
    except ValueError:
        return s


def _clock(seconds):
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    m, sec = divmod(rest, 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


def job_text(what, stage, elapsed):
    """'Recovering deleted records - Reading the pages: 1,234 of 3,922 · 0:42, about 1:10
    left' (the time left once the step has run a few seconds and has a total)."""
    parts = [what]
    if stage is not None:
        name, done, total, since = stage
        step = name
        if done is not None and total:
            step += ": %s of %s" % (format(done, ","), format(total, ","))
        elif done is not None:
            step += ": %s" % format(done, ",")
        parts.append(step)
        ran = time.time() - since
        if done and total and ran > 3 and done < total:
            parts.append("about %s left in this step" % _clock(ran * (total - done) / done))
    text = " \u2014 ".join(parts)
    return "%s \u00b7 %s elapsed" % (text, _clock(elapsed))


class ForensicsTab(ttk.Frame):
    """The Forensics tab of App. app gives db, BlobViewer and RowWin access."""

    def __init__(self, parent, app):
        ttk.Frame.__init__(self, parent)
        self.app = app
        self._runner = Runner(self, "forensics", release=app._release_worker_connection)
        self._stop = False
        self._progress = (0, 0)
        self._records = []
        self._record_view = []           # the records listed in the grid (its '#' column)
        self._dropped = []
        self._findings = None
        self._history = None
        self._summary_keys, self._summary_shown = [], []
        self._version_values = []
        self._busy = False
        self._scanned = False            # a recovery ran on this database
        self._freed = []                 # Records recovered from freed pages
        self._freed_pages = ([], [])     # (trunk, leaf) page numbers of the freelist
        self._build()

    # -- lifecycle -------------------------------------------------------------
    @property
    def db(self):
        return self.app.db

    def results_shown(self):
        """The status lines of the sub-tabs that show results now (recovered records, freed
        pages, row history, dropped tables, the journal's rows, the audit): a note goes there
        when they are cleared because another database became the active one."""
        out = []
        if self._records:
            out.append(self.carve_status)
        if self._freed:
            out.append(self.freed_status)
        if self._history is not None or self.keys_tree.get_children():
            out.append(self.history_status)
        if self._dropped or self.drop_tree.get_children():
            out.append(self.drop_status)
        if self.journal_grid.source is not None:
            out.append(self.journal_status)
        if self._findings is not None or self.audit_tree.get_children():
            out.append(self.audit_status)
        return out

    def reset(self):
        """Forget everything shown (another database is being opened, or none)."""
        self.stop()
        self._records, self._dropped, self._findings, self._history = [], [], None, None
        self.carve_grid.set_source(None)
        if hasattr(self, "rec_tree"):
            self._show_record_panel(None)
            self.carve_overview.delete(*self.carve_overview.get_children())
        self.drop_grid.set_source(None)
        self.journal_grid.set_source(None)
        for tree in (self.keys_tree, self.versions_tree, self.values_tree, self.drop_tree,
                     self.audit_tree):
            tree.delete(*tree.get_children())
        for lbl in (self.carve_status, self.history_status, self.drop_status, self.journal_status,
                    self.audit_status, self.freed_status):
            lbl.configure(text="")
        self._freed, self._freed_pages = [], ([], [])
        self._history_summary_for = None
        self.freed_tree.delete(*self.freed_tree.get_children())
        self.freed_grid.set_source(None)
        self.freed_hex.set_data(b"")
        self._set_text(self.drop_sql, "")
        self._set_text(self.journal_text, "")

    def on_open(self):
        """A database was opened: fill the table choices and the journal summary."""
        self.reset()
        self._scanned = False
        self._overview_table = None
        self._fill_overview()
        self._schedule_auto_recover()
        self._schedule_auto_history()
        tables = self.db.tables() if self.db.ok else []
        self.carve_table.configure(values=["All tables"] + tables)
        self.carve_table.set("All tables")
        self.history_table.configure(values=tables)
        self._hist_labels = {}              # combobox label -> table (with its counts)
        self._hist_values = (list(tables), [])     # (the lists of the database shown)
        self._all_history = []
        self._hist_overview_for = None
        self.journal_table.configure(values=tables)
        if tables:
            self.history_table.set(tables[0])
            self.journal_table.set(tables[0])
        self._show_freed_page()
        info = self.db.journal_info() if self.db.ok else None
        if info is None:
            self.nb.hide(self._journal_page)        # nothing to show: no page for it
            self._set_text(self.journal_text, "No rollback journal next to this database.")
            self.journal_show.configure(state="disabled")
        else:
            self.nb.add(self._journal_page)         # shown again at its place
            self.journal_show.configure(state="normal")
            self._set_text(self.journal_text, self._journal_summary(info))

    def stop(self):
        """Stop the running job (its statement is interrupted by the app's worker shutdown)."""
        aid = getattr(self, "_auto_after", None)
        hid = getattr(self, "_auto_hist_after", None)
        if hid is not None:
            try:
                self.after_cancel(hid)
            except tk.TclError:
                pass
            self._auto_hist_after = None
        if aid is not None:                 # a recovery planned on opening: not any more
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass
            self._auto_after = None
        self._stop = True
        self._runner.cancel()

    def worker_threads(self):
        return self._runner.threads()

    def _run(self, key, fn, done, status, what="Working"):
        """Run fn(cancel, progress) on the worker thread, then done(result) on the Tk thread.
        what: the job's name beside the progress bar ('Recovering deleted records')."""
        self._stop = False
        self._progress = (0, 0)
        started = time.time()
        self._job_what, self._job_started = what, started
        status.configure(text="%s…" % what)
        self.job_lbl.configure(text="%s…" % what)
        self.bar.configure(value=0, maximum=1)
        self._topbar.show(self.job_lbl, True)
        self._topbar.show(self.bar, True)

        def job():
            return fn(lambda: self._stop, self._set_progress)

        def finished(result, error):
            self._busy = False
            self.bar.configure(value=0)
            self._topbar.show(self._stop_btn, False)
            self._topbar.show(self.bar, False)
            self._topbar.show(self.job_lbl, False)
            self.job_lbl.configure(text="")
            if error is not None:
                status.configure(text="Failed: %s (logged under Issues)" % error)
                session = getattr(self.db, "session", None)
                if session is not None:
                    session.issues.add("forensics_failed", str(error), what, "error")
                refresh = getattr(self.app, "_refresh_issue_btn", None)
                if refresh is not None:
                    refresh()
                return
            done(result, time.time() - started)
        self._busy = True
        self._topbar.show(self._stop_btn, True)
        self._runner.submit(key, job, finished)
        if getattr(self, "_progress_after", None) is None:
            self._poll_progress()

    def _set_progress(self, done, total):
        self._progress = (done, total)          # read by the Tk thread's poll

    def _poll_progress(self):
        self._progress_after = None
        if not getattr(self, "_busy", False):
            return
        done, total = self._progress
        stage = None
        try:
            fx = self.db.session.__dict__.get("_forensics") if self.db.ok else None
            stage = fx.stage if fx is not None else None
        except Exception:               # noqa: BLE001 - the job line keeps its name then
            stage = None
        if stage is not None and stage[2]:
            self.bar.configure(maximum=stage[2], value=min(stage[1] or 0, stage[2]))
        elif total:
            self.bar.configure(maximum=total, value=done)
        what = getattr(self, "_job_what", "")
        if what:
            self.job_lbl.configure(text=job_text(what, stage, time.time() -
                                                 getattr(self, "_job_started", time.time())))
        self._progress_after = self.after(250, self._poll_progress)

    def destroy(self):
        aid = getattr(self, "_progress_after", None)
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass
        self._progress_after = None
        ttk.Frame.destroy(self)

    # -- layout ----------------------------------------------------------------
    def _build(self):
        top = FlowFrame(self)
        top.pack(fill="x", padx=8, pady=(6, 2))
        top.add(ttk.Label(top, text="Recover what the database no longer shows. Everything is "
                                    "read-only.", style="M.TLabel"))
        # the running job's name, its progress and Stop
        self.job_lbl = top.add(ttk.Label(top, text="", style="M.TLabel"), gap=16,
                               visible=False)
        self.bar = top.add(ttk.Progressbar(top, mode="determinate", length=160), gap=6,
                           visible=False)
        self._stop_btn = ttk.Button(top, text="Stop", style="D.TButton", command=self.stop)
        top.add(self._stop_btn, gap=4, visible=False)
        self._topbar = top
        # one Export ▾ for the tab: a report of everything found, or the records listed
        self.export_menu = tk.Menu(self, tearoff=0)
        self.export_menu.add_command(label="Forensic report (everything found so far)…",
                                     command=self._report_dialog)
        self.export_menu.add_command(label="Recovered records listed…",
                                     command=self._export_records)
        self.export_btn = top.add(ttk.Button(top, text="Export ▾"), gap=8)
        self.export_btn.configure(command=lambda: self.app._post_menu(self.export_menu,
                                                                      self.export_btn))
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=4)
        self._carve_page = self._page("Recovered Records")
        self._build_carve(self._carve_page)
        # Freed Pages: shown only for a database with freed pages
        self._freed_page = self._page("Freed Pages")
        self._build_freed(self._freed_page)
        self._history_page = self._page("Row History")
        self._build_history(self._history_page)
        self._build_dropped(self._page("Dropped Tables"))
        # Rollback Journal: shown only for a database with a -journal file
        self._journal_page = self._page("Rollback Journal")
        self._build_journal(self._journal_page)
        self._audit_page = self._page("Audit")
        self._build_audit(self._audit_page)
        self.nb.hide(self._freed_page)
        self.nb.bind("<<NotebookTabChanged>>", self._on_page)

    def _page(self, title):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  %s  " % title)
        return f

    @staticmethod
    def _set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _tree(self, parent, columns, widths, height=8, noun="line"):
        """A list with its search field above it (the one SearchBox: live, N of M)."""
        box = ttk.Frame(parent)
        search = SearchBox(box, placeholder="Find in this list…", delay=0, find_button=False,
                           width=24)
        search.pack(fill="x", pady=(0, 2))
        inner = ttk.Frame(box)
        inner.pack(fill="both", expand=True)
        tree = ttk.Treeview(inner, columns=[c for c, _t in columns], show="headings",
                            height=height, selectmode="browse")
        for (c, title), w in zip(columns, widths):
            tree.heading(c, text=title)
            tree.column(c, width=w, stretch=w > 200)
        ysb = ttk.Scrollbar(inner, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        TreeviewTooltip(tree)
        tree.search_filter = TreeFilter(tree, search, noun)
        return box, tree

    # -- deleted records -------------------------------------------------------
    def _build_carve(self, f):
        row = FlowFrame(f)
        row.pack(fill="x", pady=(6, 2), padx=4)
        row.add(ttk.Label(row, text="Table:"))
        self.carve_table = row.add(SearchableCombobox(row, state="readonly", width=28,
                                                values=["All tables"]), gap=2)
        self.carve_table.set("All tables")
        row.add(ttk.Label(row, text="Show:"), gap=10)
        self.carve_conf = row.add(SearchableCombobox(row, state="readonly", width=16,
                                               values=[m[0] for m in MIN_CONFIDENCE]), gap=2)
        self.carve_conf.set(MIN_CONFIDENCE[0][0])
        self.carve_conf.bind("<<ComboboxSelected>>", lambda e: self._show_records())
        row.add(ttk.Label(row, text="Time limit:"), gap=10)
        self.carve_time = row.add(SearchableCombobox(row, state="readonly", width=10,
                                               values=[t[0] for t in TIME_LIMITS]), gap=2)
        self.carve_time.set(TIME_LIMITS[1][0])
        # where to look: every place by default, the choice behind one menu button that
        # says how many are ticked
        self.source_vars = {}
        self.look_btn, look = menu_button(row, "Look in ▾")
        row.add(self.look_btn, gap=10)
        for key, label in SOURCE_LABELS:
            var = tk.BooleanVar(value=True)
            self.source_vars[key] = var
            look.add_checkbutton(label=label, variable=var, command=self._update_look_label)
        look.add_separator()
        self.index_entries_var = tk.BooleanVar(value=True)
        look.add_checkbutton(label="Index entries (deleted entries of the indexes)",
                             variable=self.index_entries_var,
                             command=self._update_look_label)
        ToolTip(self.look_btn, "Where to look: %s. Index entries: also recover deleted "
                               "entries of the tables' indexes (indexed values + rowid), from "
                               "the same places; they often outlive the row itself and are "
                               "listed as 'table [index name]'." % ", ".join(
                                   l for _k, l in SOURCE_LABELS))
        self._update_look_label()
        row.add(ttk.Button(row, text="Recover deleted records", style="P.TButton",
                           command=self.start_carve), gap=10)
        frow = ttk.Frame(f)
        frow.pack(fill="x", padx=4, pady=2)
        self.carve_status = wrap_to_width(ttk.Label(f, text="No scan yet \u2014 press \u2018Recover deleted records\u2019 to carve the free pages for deleted rows.",
                                                    style="M.TLabel"))
        self.carve_status.pack(fill="x", padx=4)
        ToolTip(self.carve_conf, CONFIDENCE_TEXT)
        # overview of the tables | the records | the selected record, side by side
        pane = ttk.Panedwindow(f, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=4, pady=4)
        left = ttk.Frame(pane)
        pane.add(left, weight=0)
        box, self.carve_overview = self._tree(
            left, (("table", "Table"), ("live", "Live rows"), ("rec", "Recovered"),
                   ("high", "High")), (150, 72, 72, 48), height=12, noun="table")
        box.pack(fill="both", expand=True)
        self.carve_overview.tag_configure("found", foreground=K["danger_text"])
        self.carve_overview.tag_configure("empty", foreground=K["muted_text"])
        self.carve_overview.bind("<<TreeviewSelect>>", self._on_overview)
        ToolTip(self.carve_overview, "Every table with its live rows and the records recovered "
                                     "for it (High: of them, high confidence). Click one to "
                                     "list only its records, in its own columns.")
        self._overview_table = None
        mid = ttk.Frame(pane)
        pane.add(mid, weight=3)
        self.carve_grid = DataGrid(mid, frozen=1, on_open_row=self._open_record,
                                   on_context_menu=self._carve_menu,
                                   row_style=self._carve_row_style,
                                   on_selection=lambda _s: self._show_selected_record())
        self.carve_grid.pack(fill="both", expand=True)
        right = ttk.Frame(pane)
        pane.add(right, weight=1)
        self._build_record_panel(right)
        # the panes start at what they ask for, and the record panel asks for much (its
        # columns and buttons): the records - the tab's subject - were left ~190 px. Once
        # shown, the records get the room and the record panel a readable third at most
        self._carve_pane = pane
        self._carve_left = left
        self._carve_dragged = False     # a sash moved by hand: kept as it is
        pane.bind("<Configure>", self._size_carve_panes, add="+")
        pane.bind("<ButtonRelease-1>", lambda e: setattr(self, "_carve_dragged", True),
                  add="+")
        self.carve_search = grid_search(frow, self.carve_grid, width=36,
                                        placeholder="Find records (words, all must match)…")
        self.carve_search.pack(side="left", padx=4, fill="x", expand=True)
        self.carve_filter = self.carve_search.var

    def _size_carve_panes(self, _e=None):
        """The tables list as wide as its columns (Table, Live rows, Recovered, High) up to
        a third, the record panel up to 400 px, the records the rest; again when the pane's
        width changes, until a sash is moved by hand. Only on a new width: setting the
        sashes re-wraps the record panel's buttons, its requested size changes and the pane
        is laid out again - re-setting the sashes then would never end."""
        pane = self._carve_pane
        if self._carve_dragged:
            return
        w = pane.winfo_width()
        if w < 600:
            return                      # not laid out yet: the next resize does it
        if w == getattr(self, "_carve_sized_w", None):
            return                      # the panes' own re-layout, not a new width: setting
        self._carve_sized_w = w         # the sashes again would loop (see the docstring)
        try:
            left = min(self._carve_left.winfo_reqwidth(), int(w * 0.34))
            right = min(400, int(w * 0.30))
            pane.sashpos(0, left)
            pane.sashpos(1, max(left + 280, w - right))
        except tk.TclError:
            pass

    # -- recovery on open ----------------------------------------------------------------
    AUTO_DELAY_MS = 3000            # after opening: the first screens first

    def _schedule_auto_recover(self):
        """Recover the deleted records in the background soon after a database opens (when
        it is no larger than the limit auto_recover_bytes), so Browse, Search and this tab can
        show them without anyone pressing a button."""
        old = getattr(self, "_auto_after", None)
        if old is not None:
            try:
                self.after_cancel(old)
            except tk.TclError:
                pass
        self._auto_after = None
        if not self.db.ok:
            return
        size = self._evidence_size()
        cap = limits.get("auto_recover_bytes")
        if not cap or size is None or size > cap:
            if cap and size is not None:
                self.carve_status.configure(
                    text="Not recovered on opening: the database is %s, more than the limit "
                         "auto_recover_bytes (%s) \u2014 press \u2018Recover deleted "
                         "records\u2019." % (fmtb(size), fmtb(cap)))
            return
        session = self.db.session
        self.carve_status.configure(text="Deleted records are recovered in the background "
                                         "in a moment (limit auto_recover_bytes)\u2026")
        self._auto_after = self.after(self.AUTO_DELAY_MS, lambda: self._auto_recover(session))

    AUTO_HISTORY_RETRY_MS = 4000    # the overview waits for the auto recovery to finish

    def _schedule_auto_history(self):
        """Count the rows with history (older versions in the WAL / journal) by itself soon
        after a database opens that has a WAL or a rollback journal, so the navigator can
        mark the tables and the Row History page is ready; it waits while another job runs."""
        old = getattr(self, "_auto_hist_after", None)
        if old is not None:
            try:
                self.after_cancel(old)
            except tk.TclError:
                pass
        self._auto_hist_after = None
        if not self.db.ok:
            return
        try:
            wanted = self.db.has_wal or self.db.journal_info() is not None
        except Exception:               # noqa: BLE001 - no journal then
            wanted = self.db.has_wal
        if not wanted:
            return
        session = self.db.session
        self._auto_hist_after = self.after(self.AUTO_DELAY_MS + 1500,
                                           lambda: self._auto_history(session))

    def _auto_history(self, session):
        self._auto_hist_after = None
        if not self.db.ok or self.db.session is not session:
            return
        if getattr(self, "_hist_overview_for", None) is session:
            return                      # done (or running) already
        if self._busy:
            self._auto_hist_after = self.after(self.AUTO_HISTORY_RETRY_MS,
                                               lambda: self._auto_history(session))
            return
        self.start_history_overview()

    def _auto_recover(self, session):
        self._auto_after = None
        if not self.db.ok or self.db.session is not session or self._busy or self._scanned:
            return
        self.start_carve()

    def _evidence_size(self):
        try:
            import os
            total = 0
            base = getattr(self.db, "_path", None)
            for p in (base, base + "-wal", base + "-journal"):
                if os.path.exists(p):
                    total += os.path.getsize(p)
            return total
        except (OSError, TypeError, AttributeError):
            return None

    # -- the table overview and the record panel ----------------------------------------
    def _fill_overview(self):
        """Every table: live rows (the row counts), records recovered (and how many high)."""
        tree = self.carve_overview
        keep = self._overview_table
        tree.delete(*tree.get_children())
        if not self.db.ok:
            return
        counts = getattr(self.app, "_count_cache", None) or {}
        by = {}
        for r in self._records:
            n, high = by.get(r.table or "(unknown)", (0, 0))
            by[r.table or "(unknown)"] = (n + 1, high + (r.confidence == "high"))
        total = len(self._records)
        tree.insert("", "end", iid="__all__", values=(
            "All tables", "", format(total, ",") if self._records or self._scanned else "-",
            format(sum(h for _n, h in by.values()), ",") if self._records else ""))
        names = list(self.db.tables())
        names += sorted(t for t in by if t not in names)
        # the tables with recovered records first, then the others
        names.sort(key=lambda t: (-by.get(t, (0, 0))[0], t.lower()))
        for t in names:
            live = counts.get(t)
            n, high = by.get(t, (0, 0))
            tags = ("found",) if n else (("empty",) if live == 0 else ())
            tree.insert("", "end", iid="t:" + t, tags=tags, values=(
                t, format(live, ",") if isinstance(live, int) else (live or ""),
                format(n, ",") if n else ("0" if self._scanned else "-"),
                format(high, ",") if high else ""))
        found = [t for t in by if t != "(unknown)"]
        if keep is None and len(found) == 1 and len(by) == 1 and (
                getattr(self, "_auto_table_for", None) is not self._records):
            # one table holds every record: show it in its own columns at once (once per
            # recovery: choosing All tables afterwards stays)
            self._auto_table_for = self._records
            keep = self._overview_table = found[0]
            self.after_idle(self._show_records)
        want = "__all__" if keep is None else "t:" + keep
        if tree.exists(want):
            tree.selection_set(want)
            tree.see(want)

    def _on_overview(self, _e=None):
        sel = self.carve_overview.selection()
        table = None
        if sel and sel[0].startswith("t:"):
            table = sel[0][2:]
        if table != self._overview_table:
            self._overview_table = table
            self._show_records()

    def _build_record_panel(self, f):
        """The selected record: its columns and values as a table, where it was found, and
        what can be done with it."""
        self.rec_title = wrap_to_width(ttk.Label(f, text="Select a record", style="B.TLabel"))
        self.rec_title.pack(fill="x", padx=4, pady=(0, 2))
        self.rec_where = wrap_to_width(ttk.Label(f, text="", style="M.TLabel"))
        self.rec_where.pack(fill="x", padx=4)
        # what an index entry is, and what the database's triggers do to this table's rows:
        # shown only when it applies
        self.rec_note = wrap_to_width(ttk.Label(f, text="", style="M.TLabel"))
        self._rec_note_shown = False
        self._rec_where_widget = self.rec_where
        # the buttons first, at the bottom: a narrow panel wraps them to two lines and the
        # column list above gives way (packed after them), so no button is ever cut off
        btns = FlowFrame(f)
        btns.pack(side="bottom", fill="x", padx=2, pady=(2, 0))
        self.rec_buttons = {}
        for key, text, cmd in (("copy_value", "Copy value", self._rec_copy_value),
                               ("copy_row", "Copy row", self._rec_copy_row),
                               ("open", "Open\u2026", self._rec_open),
                               ("bytes", "Bytes\u2026", self._rec_bytes),
                               ("history", "Row history", self._rec_history)):
            b = btns.add(ttk.Button(btns, text=text, style="Small.TButton", command=cmd), gap=2)
            b.state(["disabled"])
            self.rec_buttons[key] = b
        box, self.rec_tree = self._tree(f, (("col", "Column"), ("value", "Value"),
                                            ("date", "Date (UTC)")), (120, 260, 200),
                                        height=12, noun="column")
        box.pack(fill="both", expand=True, padx=2, pady=2)
        from parts import HoverCard
        HoverCard(self.rec_tree, self._rec_hover)   # a narrow panel cuts values: whole here
        self.rec_tree.bind("<Double-1>", self._rec_open_value)
        self.rec_tree.bind("<Button-3>", self._rec_menu)
        self._panel_record = None

    def _show_selected_record(self):
        sel = self.carve_grid.selected_rows()
        rec = None
        if sel:
            data = self.carve_grid.row_data(sel[0])
            rec = self._record_at(data[0]) if data else None
        self._show_record_panel(rec)

    def _show_record_panel(self, rec):
        self._panel_record = rec
        tree = self.rec_tree
        tree.delete(*tree.get_children())
        for b in self.rec_buttons.values():
            b.state(["!disabled"] if rec is not None else ["disabled"])
        if rec is None:
            self.rec_title.configure(text="Select a record")
            self.rec_where.configure(text="")
            self._set_rec_note("")
            return
        self._set_rec_note(self._record_note(rec))
        if rec.table is None or rec.rowid is None:
            self.rec_buttons["history"].state(["disabled"])
        self.rec_title.configure(text="%s \u2014 %s, %s confidence" % (
            getattr(rec, "label", rec.table) or "(unknown table)", record_state(rec),
            rec.confidence))
        bits = [where_text(self.db, rec)]
        if rec.rowid is not None:
            bits.append("rowid %s" % rec.rowid)
        if rec.reasons:
            bits.append("; ".join(rec.reasons))
        self.rec_where.configure(text=" \u00b7 ".join(bits))
        for i, (c, v) in enumerate(zip(rec.columns, rec.values)):
            tree.insert("", "end", iid=str(i), values=(c, vb(v),
                                                       date_reading(self.db, rec.table, c, v)))
        self._fit_rec_columns()

    def _rec_hover(self, iid):
        """A line of the record panel, whole: column, value (up to HOVER_CHARS), date."""
        if not iid:
            return None
        col, value, date = (self.rec_tree.item(iid, "values") or ("", "", ""))[:3]
        text = str(value)
        if len(text) > HOVER_CHARS:
            text = text[:HOVER_CHARS] + "… (%s characters: Open… shows all)" % format(
                len(text), ",")
        return "%s\n%s%s" % (col, text, "\n" + date if date else "")

    REC_COLUMN_BASE = {"col": 70, "value": 80, "date": 90}   # px; Tk stretches them to fit

    def _fit_rec_columns(self):
        """The record's columns fill the panel: the Date column only when a value reads as a
        date (else Value takes its room). Small base widths that Tk stretches - never sized
        from the panel's width on <Configure> (that re-lays the panel without end), and wide
        fixed widths put the Date column past a narrow panel's edge."""
        tree = self.rec_tree
        dated = any(tree.set(i, "date") for i in tree.get_children())
        want = ("col", "value", "date") if dated else ("col", "value")
        if tuple(tree["displaycolumns"]) != want:
            tree.configure(displaycolumns=want)
        if not getattr(self, "_rec_base_set", False):
            for c, w in self.REC_COLUMN_BASE.items():
                tree.column(c, width=w, minwidth=40, stretch=True)
            self._rec_base_set = True

    def _set_rec_note(self, text):
        self.rec_note.configure(text=text)
        if text and not self._rec_note_shown:
            self.rec_note.pack(fill="x", padx=4, pady=(2, 0), after=self._rec_where_widget)
        elif not text and self._rec_note_shown:
            self.rec_note.pack_forget()
        self._rec_note_shown = bool(text)

    def _trigger_map(self):
        """The database's triggers (live, and dropped ones once recovered), kept until the
        recovered schema changes."""
        session = getattr(self.db, "session", None)
        if session is None:
            return None
        fx = session.__dict__.get("_forensics")
        key = (id(session), id((getattr(fx, "_cache", None) or {}).get("dropped")))
        if getattr(self, "_tmap_key", None) != key:
            from engine.triggers import for_session
            try:
                self._tmap = for_session(session)
            except Exception:           # noqa: BLE001 - no note then
                self._tmap = None
            self._tmap_key = key
        return self._tmap

    def _record_note(self, rec):
        """Plain words on what this record is (an index entry) and on what the database's
        triggers do to its table's rows; '' when there is nothing to say."""
        parts = []
        if getattr(rec, "index", None) is not None:
            parts.append("An entry of index %s: it shows that a row of %s with these values "
                         "existed%s. An index keeps only its own columns, so the row's other "
                         "columns are not in it." % (
                             rec.index, rec.table or "its table",
                             " (rowid %s)" % rec.rowid if rec.rowid is not None else ""))
        tmap = self._trigger_map() if rec.table else None
        if tmap is not None:
            from engine.triggers import DELETES, INSERTS, action_text
            dels = tmap.changing(rec.table, DELETES)
            if dels:
                txt = "; ".join("%s (%s, it %s)" % (t.name, t.event_text(), action_text(a, t))
                                for t, a in dels[:2])
                more = " +%d more" % (len(dels) - 2) if len(dels) > 2 else ""
                parts.append("The database deletes rows of %s by itself - %d trigger%s: %s%s. "
                             "This row may have gone that way, not by a direct delete." % (
                                 rec.table, len(dels), "" if len(dels) == 1 else "s", txt, more))
            logs = [(t, a) for t in tmap.on(rec.table) if t.event in ("DELETE", "UPDATE")
                    for a in t.actions if a.verb == INSERTS and a.uses_old and a.table and
                    a.table.lower() != rec.table.lower()]
            if logs:
                where = sorted(set(a.table for _t, a in logs))
                names = sorted(set(t.name for t, _a in logs))
                shown = ", ".join(names[:3]) + (" +%d more" % (len(names) - 3)
                                                if len(names) > 3 else "")
                parts.append("When rows of %s are deleted or changed, trigger%s %s add%s a row "
                             "to %s with the old values: a copy or a log line of this row may be "
                             "live there." % (
                                 rec.table, "" if len(names) == 1 else "s", shown,
                                 "s" if len(names) == 1 else "", ", ".join(where)))
        return "\n".join(parts)

    def _rec_value(self):
        rec = self._panel_record
        sel = self.rec_tree.selection()
        if rec is None or not sel:
            return None, None
        i = int(sel[0])
        return rec.columns[i], rec.values[i]

    def _rec_copy_value(self):
        col, v = self._rec_value()
        if col is None and self._panel_record is not None and self._panel_record.values:
            col, v = self._panel_record.columns[0], self._panel_record.values[0]
        if col is not None:
            from engine.export import csv_cell
            self.clipboard_clear()
            self.clipboard_append(csv_cell(v))

    def _rec_copy_row(self):
        rec = self._panel_record
        if rec is None:
            return
        from engine.export import csv_cell
        self.clipboard_clear()
        self.clipboard_append("\n".join("%s\t%s" % (c, csv_cell(v))
                                         for c, v in zip(rec.columns, rec.values)))

    def _rec_open(self):
        if self._panel_record is not None:
            RecordWindow(self, self._panel_record, self.app)

    def _rec_history(self):
        rec = self._panel_record
        if rec is not None and rec.table and rec.rowid is not None:
            self.app.show_row_history(rec.table, rec.rowid)

    def _rec_bytes(self):
        if self._panel_record is not None:
            BytesWindow(self, self.app, self.db, self._panel_record)

    def _rec_open_value(self, _e=None):
        col, v = self._rec_value()
        if col is None:
            return
        if isinstance(v, (bytes, bytearray)) and not isinstance(v, InvalidText):
            self.app.open_blob(bytes(v), col, "%s (recovered, %s)" % (
                col, self._panel_record.prov.where()))
        else:
            from value_viewer import ValueViewer
            ValueViewer(self, v, "%s \u2014 recovered record, %s" % (
                col, self._panel_record.prov.where()))

    def _rec_menu(self, e):
        iid = self.rec_tree.identify_row(e.y)
        if not iid:
            return
        self.rec_tree.selection_set(iid)
        rec = self._panel_record
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Copy value", command=self._rec_copy_value)
        m.add_command(label="Copy column name", command=lambda: (
            self.clipboard_clear(), self.clipboard_append(self._rec_value()[0] or "")))
        m.add_command(label="Copy row (column, value per line)", command=self._rec_copy_row)
        m.add_command(label="Copy where (page, byte offset, file)", command=lambda: (
            self.clipboard_clear(), self.clipboard_append(where_text(self.db, rec))))
        m.add_separator()
        v = self._rec_value()[1]
        blob = isinstance(v, (bytes, bytearray)) and not isinstance(v, InvalidText)
        m.add_command(label="Inspect BLOB\u2026" if blob else "View value\u2026",
                      command=self._rec_open_value)
        m.add_command(label="Open the record\u2026", command=self._rec_open)
        m.add_command(label="Bytes\u2026", command=self._rec_bytes)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _carve_row_style(self, _row, values, _flags):
        """Each record's row in the colour of its state."""
        src = self.carve_grid.source
        try:
            i = src.columns().index("State")
        except (AttributeError, ValueError):
            return None, None
        bg, mark = STATE_STYLE.get(values[i] if i < len(values) else None, (None, None))
        return (K.get(bg) if bg else None), (K.get(mark) if mark else None)

    def _update_look_label(self):
        """'Look in ▾' says how many places are ticked when not all of them are."""
        n = sum(1 for v in self.source_vars.values() if v.get())
        text = "Look in ▾" if n == len(self.source_vars) else "Look in (%d of %d) ▾" % (
            n, len(self.source_vars))
        if not self.index_entries_var.get():
            text = text.replace(" ▾", ", no index entries ▾")
        self.look_btn.configure(text=text)

    def _carve_menu(self, menu, row, _col):
        """Right-click on recovered records: open, row history, tag them or every shown one."""
        lo, hi = self.carve_grid.selected_rows() or (row, row)
        picked = []
        for r in range(lo, hi + 1):
            data = self.carve_grid.row_data(r)
            rec = self._record_at(data[0]) if data else None
            if rec is not None:
                picked.append(rec)
        menu.add_separator()
        if len(picked) == 1:
            rec = picked[0]
            # (the grid's own menu already offers Open row detail)
            if rec.table and rec.rowid is not None:
                menu.add_command(label="Row history (every version)",
                                 command=lambda: self.app.show_row_history(rec.table, rec.rowid))
        self.app.tag_menu(menu, lambda: [entry_from_record(r) for r in picked],
                          label="Tag %s" % ("record" if len(picked) == 1 else "%d records" % len(picked)))
        store = getattr(self.app.tags, "store", None)
        shown = sub = None
        if store is not None:
            sub = tk.Menu(menu, tearoff=0)
            for d in store.defs:
                sub.add_command(label=d.name, command=lambda name=d.name: self._tag_all_shown(name))
            shown = len(getattr(self, "_record_view", []))
        menu.add_cascade(label="Tag all shown records", menu=sub if sub is not None else tk.Menu(menu),
                         state="normal" if shown else "disabled")

    def _tag_all_shown(self, tag):
        recs = self.filtered_records()
        if len(recs) > 5000 and not messagebox.askyesno(
                "Tag records", "Tag %d records '%s'?" % (len(recs), tag), parent=self):
            return
        n = self.app.tag_entries([entry_from_record(r) for r in recs], tag)
        self.carve_status.configure(text="%d records tagged '%s'" % (n, tag))

    def start_carve(self):
        if not self.db.ok:
            return
        table = self.carve_table.get()
        tables = None if table in ("", "All tables") else [table]
        sources = [k for k, v in self.source_vars.items() if v.get()]
        if not sources:
            self.carve_status.configure(text="Choose at least one place to look in.")
            return
        limit = dict(TIME_LIMITS).get(self.carve_time.get(), 300)
        db = self.db
        index_entries = self.index_entries_var.get()

        def work(cancel, progress):
            return db.carve_records(tables, sources, cancel, progress, limit,
                                    index_entries=index_entries)

        def done(result, seconds):
            self._records = list(result)
            st = getattr(result, "stats", {}) or {}
            complete = getattr(result, "complete", True)
            by = dict((c, sum(1 for r in self._records if r.confidence == c)) for c in CONFIDENCES)
            entries = sum(1 for r in self._records if getattr(r, "index", None) is not None)
            self.carve_status.configure(text="%d records (%d high, %d medium, %d low%s) from %s "
                                             "pages in %.1fs%s" % (
                len(self._records), by["high"], by["medium"], by["low"],
                "; %d index entries" % entries if entries else "",
                st.get("pages", "?"), seconds,
                "" if complete else " — STOPPED EARLY: %s; the records found until then are "
                                    "listed" % stop_text(st.get("stopped"))) + live_check_text(st))
            self._scanned = True
            if tables is None and complete:
                db.set_recovered(self._records, sources)     # Search reuses them
            self._fill_overview()
            self._show_records()
            refresh = getattr(self.app, "_refresh_browse_recovered", None)
            if refresh is not None:
                refresh()               # Browse offers the table's recovered records
            query = getattr(self.app, "_query", None)
            if query is not None:
                query._fill_tables()    # the Query tab lists them as recovered_<table>
        self._run("carve", work, done, self.carve_status, "Recovering deleted records")

    def _shown_records(self):
        allowed = dict(MIN_CONFIDENCE).get(self.carve_conf.get(), CONFIDENCES)
        return [r for r in self._records if r.confidence in allowed]

    def _show_records(self):
        """The records listed: every table's with a Values column, or with one table chosen
        (in the overview) that table's own columns, each value in its column."""
        recs = self._shown_records()
        table = self._overview_table
        if table is not None:
            recs = [r for r in recs if (r.table or "(unknown)") == table]
        rows = []
        if table is not None and recs:
            cols = []
            for r in sorted(recs, key=lambda r: -len(r.columns)):
                for c in r.columns:
                    if c not in cols:
                        cols.append(c)
            head = [RID, "State", "Confidence", "Row ID", "Source", "Where", "Byte offset"]
            for i, r in enumerate(recs):
                vals = dict(zip(r.columns, r.values))
                rows.append(([i + 1, record_state(r), r.confidence,
                              "" if r.rowid is None else r.rowid, r.prov.source,
                              r.prov.where(), self._byte_offset(r)]
                             + [vals.get(c) for c in cols], ()))
            src = ListSource(head + cols, rows, note="")
        else:
            for i, r in enumerate(recs):
                p = r.prov
                rows.append(([i + 1, record_state(r), r.confidence,
                              getattr(r, "label", r.table) or "(unknown)",
                              "" if r.rowid is None else r.rowid, p.source, p.where(),
                              self._byte_offset(r), ", ".join(sorted(r.flags)),
                              values_preview(r.columns, r.values)], ()))
            src = ListSource([RID, "State", "Confidence", "Table", "Row ID", "Source", "Where",
                              "Byte offset", "Flags", "Values"], rows, note="")
        self._record_view = recs
        self.carve_grid.set_source(src)
        # the record's own columns must be in view: the columns about the record stay narrow
        for name, w in (("State", 96), ("Confidence", 80), ("Table", 150), ("Row ID", 60),
                        ("Source", 86), ("Where", 150), ("Byte offset", 92), ("Flags", 110)):
            if name in src.columns():
                self.carve_grid.set_column_width(src.columns().index(name), w)
        self._show_record_panel(None)
        if recs:                        # the first record is selected: its values show at once
            self.after_idle(self._select_first_record)

    def _byte_offset(self, rec):
        """The record's byte offset in its file (the Byte offset column), or None."""
        from recordui import file_offset
        return file_offset(self.db, rec.prov)

    def _select_first_record(self, tries=20):
        g = self.carve_grid
        try:
            if g.selected_rows() or self._panel_record is not None:
                return                  # a choice made meanwhile stays
            if g.row_count() > 0:
                g.set_current_cell(0, None)
            elif tries:
                self.after(50, lambda: self._select_first_record(tries - 1))
        except (tk.TclError, AttributeError):
            pass

    def _record_at(self, values):
        try:
            return self._record_view[int(values[0]) - 1]
        except (AttributeError, IndexError, TypeError, ValueError):
            return None

    def _open_record(self, _row, values):
        rec = self._record_at(values)
        if rec is not None:
            RecordWindow(self, rec, self.app)

    def filtered_records(self):
        """The recovered records the grid shows now (its filters and sort applied)."""
        src = getattr(self.carve_grid, "_source", None)
        if src is None:
            return []
        out = []
        for values in src.iter_rows():
            rec = self._record_at(values)
            if rec is not None:
                out.append(rec)
        return out

    def _export_records(self):
        recs = self.filtered_records()
        if not recs:
            messagebox.showinfo("Export", "Recover deleted records first.", parent=self)
            return
        self._write_report({"records": recs}, "recovered_records")

    # -- freed pages ---------------------------------------------------------------
    def _build_freed(self, f):
        self.freed_intro = wrap_to_width(ttk.Label(f, text="", style="M.TLabel"))
        self.freed_intro.pack(fill="x", padx=4, pady=(6, 2))
        row = ttk.Frame(f)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Button(row, text="Recover from freed pages", style="P.TButton",
                   command=self.start_freed).pack(side="left")
        self.freed_status = wrap_to_width(ttk.Label(f, text="", style="M.TLabel"))
        self.freed_status.pack(fill="x", padx=4)
        pane = ttk.Panedwindow(f, orient="vertical")
        pane.pack(fill="both", expand=True, padx=4, pady=4)
        top = ttk.Frame(pane)
        pane.add(top, weight=1)
        box, self.freed_tree = self._tree(top, (("page", "Page"), ("kind", "Kind"),
                                                ("records", "Records"), ("conf", "Confidence"),
                                                ("tables", "Tables")),
                                          (70, 90, 70, 200, 320))
        box.pack(fill="both", expand=True)
        self.freed_tree.bind("<<TreeviewSelect>>", self._on_freed_page)
        low = ttk.Notebook(pane)
        pane.add(low, weight=2)
        recs = ttk.Frame(low)
        low.add(recs, text="  Records on the page  ")
        self.freed_grid = DataGrid(recs, frozen=1, on_open_row=self._open_freed_record,
                                   on_context_menu=self._freed_menu)
        grid_search(recs, self.freed_grid, placeholder="Find records on the page…").pack(
            fill="x", pady=(2, 2))
        self.freed_grid.pack(fill="both", expand=True)
        hexf = ttk.Frame(low)
        low.add(hexf, text="  Page bytes  ")
        self.freed_hex = HexView(hexf)
        self.freed_hex.pack(fill="both", expand=True)
        self._freed_view = []            # the Records the grid lists

    def _show_freed_page(self):
        """Show the Freed Pages page only for a database with freed pages, with what it is."""
        n = self.db.freelist_count() if self.db.ok else 0
        if not n:
            self.nb.hide(self._freed_page)
            return
        self.nb.add(self._freed_page)       # shows it again at its place
        self.freed_intro.configure(
            text="The database has %s freed page%s (the freelist). SQLite leaves the rows that "
                 "were on them until the pages are reused. 'Recover from freed pages' reads "
                 "the records still there with the same carver as Recovered Records, so each has "
                 "one confidence with its reasons. %s" % (
                     format(n, ","), "" if n == 1 else "s", CONFIDENCE_TEXT))

    def start_freed(self):
        if not self.db.ok:
            return
        db = self.db

        def work(cancel, progress):
            trunks, leaves = db.freelist_page_numbers()
            return trunks, leaves, db.freed_page_records(cancel)

        def done(result, seconds):
            trunks, leaves, recs = result
            self._freed_pages = (trunks, leaves)
            self._freed = list(recs)
            by_page = {}
            for r in self._freed:
                by_page.setdefault(r.prov.page, []).append(r)
            tree = self.freed_tree
            tree.delete(*tree.get_children())
            kinds = [(n, "trunk") for n in trunks] + [(n, "leaf") for n in leaves]
            for n, kind in sorted(kinds):
                rs = by_page.get(n, [])
                conf = ", ".join("%d %s" % (sum(1 for r in rs if r.confidence == c), c)
                                 for c in CONFIDENCES if any(r.confidence == c for r in rs))
                tables = ", ".join(sorted(set(r.table or "(unknown)" for r in rs)))
                tree.insert("", "end", iid=str(n), values=(n, kind, len(rs), conf or "-",
                                                            tables or "-"))
            with_recs = len(by_page)
            by = dict((c, sum(1 for r in self._freed if r.confidence == c)) for c in CONFIDENCES)
            self.freed_status.configure(
                text="%s freed pages (%d trunk, %d leaf); %s record%s on %d of them (%d high, "
                     "%d medium, %d low) in %.1fs%s" % (
                         format(len(kinds), ","), len(trunks), len(leaves),
                         format(len(self._freed), ","), "" if len(self._freed) == 1 else "s",
                         with_recs, by["high"], by["medium"], by["low"], seconds,
                         "" if self._freed else " — nothing to recover: the freed pages hold "
                                                "no records (rows identical to live rows are "
                                                "not repeated)"))
        self._run("freed", work, done, self.freed_status, "Recovering from freed pages")

    def _on_freed_page(self, _e=None):
        sel = self.freed_tree.selection()
        if not sel:
            return
        n = int(sel[0])
        recs = [r for r in self._freed if r.prov.page == n]
        self._freed_view = recs
        rows = []
        for i, r in enumerate(recs):
            rows.append(([i + 1, r.confidence, getattr(r, "label", r.table) or "(unknown)",
                          "" if r.rowid is None else r.rowid, r.prov.where(),
                          "; ".join(r.reasons), values_preview(r.columns, r.values)], ()))
        self.freed_grid.set_source(ListSource([RID, "Confidence", "Table", "Row ID", "Where",
                                               "Reasons", "Values"], rows))
        try:
            self.freed_hex.set_data(self.db.page_bytes(n))
        except Exception as e:          # noqa: BLE001 - said instead of the bytes
            self.freed_hex.set_data(b"")
            self.freed_status.configure(text="Page %d could not be read: %s" % (n, e))

    def _freed_record(self, values):
        try:
            return self._freed_view[int(values[0]) - 1]
        except (IndexError, TypeError, ValueError):
            return None

    def _open_freed_record(self, _row, values):
        rec = self._freed_record(values)
        if rec is not None:
            RecordWindow(self, rec, self.app)

    def _freed_menu(self, menu, row, _col):
        data = self.freed_grid.row_data(row)
        rec = self._freed_record(data[0]) if data else None
        if rec is None:
            return
        menu.add_separator()
        self.app.tag_menu(menu, lambda: [entry_from_record(rec)], label="Tag record")

    # -- row history -------------------------------------------------------------
    def _build_history(self, f):
        row = FlowFrame(f)
        row.pack(fill="x", padx=4, pady=(6, 2))
        row.add(ttk.Label(row, text="Table:"))
        self.history_table = row.add(SearchableCombobox(row, state="readonly", width=28,
                                                cycle_on_arrows=True), gap=2)
        # picking a table lists its changed rows at once (no extra button)
        self.history_table.bind("<<ComboboxSelected>>",
                                lambda e: self.start_history_summary())
        self._history_summary_for = None
        row.add(ttk.Label(row, text="or row id / key:"), gap=12)
        self.history_key = tk.StringVar()
        ke = row.add(ttk.Entry(row, textvariable=self.history_key, width=16), gap=2)
        ToolTip(ke, "Row id (e.g. 42) or primary-key value — shows every version of that row")
        ke.bind("<Return>", lambda e: self.show_history(self._hist_table(),
                                                        parse_key(self.history_key.get())))
        row.add(ttk.Button(row, text="Show history", command=lambda: self.show_history(
            self._hist_table(), parse_key(self.history_key.get()))), gap=2)
        self.history_show_all = tk.BooleanVar(value=False)
        cb = row.add(ttk.Checkbutton(row, text="Show all rows",
                                     variable=self.history_show_all,
                                     command=lambda: self.start_history_summary()), gap=8)
        ToolTip(cb, "List every row in the table with its history status, not just changed rows")
        self.history_only_with = tk.BooleanVar(value=True)
        self._hist_values = ([], [])        # (every table's label, those with history)
        ob = row.add(ttk.Checkbutton(row, text="Only tables with history",
                                     variable=self.history_only_with,
                                     command=self._hist_list_choice), gap=8, visible=False)
        self._hist_only_cb = ob
        ToolTip(ob, "The table list shows only the tables that have rows with older versions "
                    "(counted when this page opens); untick to pick any table")
        self.history_status = wrap_to_width(ttk.Label(f, text="", style="M.TLabel"))
        self.history_status.pack(fill="x", padx=4)
        pane = ttk.Panedwindow(f, orient="vertical")
        pane.pack(fill="both", expand=True, padx=4, pady=4)
        top = ttk.Frame(pane)
        pane.add(top, weight=1)
        box, self.keys_tree = self._tree(top, (("table", "Table"), ("key", "Row"),
                                                ("versions", "Versions"),
                                                ("deleted", "Deleted"), ("reuse", "Key reused"),
                                                ("walonly", "Only in WAL"), ("span", "Seen in")),
                                         (160, 110, 70, 70, 160, 90, 260))
        self.keys_tree.column("versions", anchor="e", stretch=False)
        self.keys_tree.column("span", stretch=True)
        box.pack(fill="both", expand=True)
        wrap_to_width(ttk.Label(top, style="M.TLabel", text=(
            "Seen in: the oldest \u2192 newest place a version of the row was found - journal "
            "(before the last transaction), stale (an earlier use of the WAL file), main (the "
            "main file), committed (the WAL up to its last commit), uncommitted (written "
            "after it; SQLite never shows these)."))).pack(fill="x", padx=4, pady=(2, 0))
        self.keys_tree.bind("<<TreeviewSelect>>", self._on_key)
        mid = ttk.Frame(pane)
        pane.add(mid, weight=1)
        box, self.versions_tree = self._tree(mid, (("n", "#"), ("era", "Version"), ("where", "Where"),
                                                   ("commit", "Commit"), ("changed", "Changed"),
                                                   ("note", "Note")),
                                             (40, 110, 220, 70, 260, 260))
        box.pack(fill="both", expand=True)
        self.versions_tree.tag_configure("gone", foreground=C["red"])
        self.versions_tree.tag_configure("current", background=K["success_soft"])
        self.versions_tree.bind("<<TreeviewSelect>>", self._on_version)
        low = ttk.Frame(pane)
        pane.add(low, weight=1)
        box, self.values_tree = self._tree(low, (("col", "Column"), ("value", "Value"),
                                                 ("type", "Type"), ("date", "Date (UTC)")),
                                           (180, 420, 80, 240))
        box.pack(fill="both", expand=True)
        self.values_tree.tag_configure("changed", background=K["highlight"])
        self.values_tree.bind("<Double-1>", self._on_history_value)
        self.values_tree.bind("<Button-3>", self._history_value_menu)
        vbar = FlowFrame(low)
        vbar.pack(fill="x", pady=(2, 0))
        for text, cmd in (("Copy value", self._history_copy_value),
                          ("Copy this version", self._history_copy_version),
                          ("Copy every version (CSV)", self._history_copy_all),
                          ("Save versions…", self._history_save_versions),
                          ("Save changed rows…", self._history_save_list),
                          ("Open row", self._history_open_row)):
            vbar.add(ttk.Button(vbar, text=text, style="Small.TButton", command=cmd), gap=2)

    def _hist_list(self):
        """The table list as chosen: only the tables with history, or every table."""
        every, with_hist = self._hist_values
        return with_hist if self.history_only_with.get() and with_hist else every

    def _hist_list_choice(self):
        values = self._hist_list()
        self.history_table.configure(values=values)
        if self.history_table.get() not in values and values:
            self.history_table.set(values[0])
            self.start_history_summary()

    def show_table_history(self, table):
        """Row History with this table chosen (its changed rows listed); counts the tables
        first when that has not happened yet."""
        if not self.db.ok:
            return
        self.nb.select(self._history_page)
        labels = getattr(self, "_hist_labels", {})
        label = next((lbl for lbl, t in labels.items() if t == table), None)
        if label is None:
            if getattr(self, "_hist_overview_for", None) is not self.db.session and \
                    not self._busy:
                self._hist_want = table
                self.start_history_overview()
                return
            label = table
        self.history_only_with.set(False) if label not in self._hist_list() else None
        self.history_table.configure(values=self._hist_list())
        self.history_table.set(label)
        self.start_history_summary()

    def _hist_table(self):
        """The table chosen in Row History (its list shows counts beside the names);
        '__all__' for All tables."""
        label = self.history_table.get()
        known = getattr(self, "_hist_labels", {}).get(label)
        if known is not None:
            return known
        # a label with counts that is not (or no longer) in the list of the database shown:
        # read as what it names, never passed on as a table name
        if label.startswith("All tables"):
            return "__all__"
        m = re.match(r"^(.+?)  \([\d,]+ changed\)$", label)
        return m.group(1) if m else label

    def _row_table(self, iid):
        """The table of a line of the changed-rows list (its own in All tables)."""
        vals = self.keys_tree.item(iid, "values")
        return vals[0] if vals and vals[0] else self._hist_table()

    def start_history_overview(self):
        """Which tables have rows with more than one version (or deleted / reused keys):
        counted for every table at once, so nobody has to try the tables one by one. The
        list then shows the counts, the tables with history first, and the first of them is
        listed."""
        if not self.db.ok:
            return
        db = self.db
        self._hist_overview_for = db.session
        tables = db.tables()
        self._hist_no_sources = not db.has_wal and db.journal_info() is None
        if self._hist_no_sources:
            # no WAL and no rollback journal: only the main file, one version per row
            self.history_status.configure(
                text="No WAL or rollback journal next to this database: every row has one "
                     "version (deleted rows are under Recovered Records). Type a row id to "
                     "see a row's stored values.")
            return

        def work(cancel, progress):
            out = []
            for i, t in enumerate(tables):
                if cancel():
                    break
                progress(i, len(tables))
                try:
                    sm = db.history_summary(t, cancel)
                except Exception:       # noqa: BLE001 - a table that cannot be read
                    sm = None
                keys = list(sm.keys) if sm is not None else []
                changed = [k for k in keys if k.versions > 1 or k.deleted or k.reuse]
                out.append((t, len(changed), changed))
            return out

        def done(counts, seconds):
            if counts is None:
                return
            if self.db is not db or getattr(self, "_hist_overview_for", None) is not db.session:
                return                  # counted for a database no longer shown
            self._all_history = [(t, k) for t, _n, ks in counts for k in ks]
            counts = [(t, n) for t, n, _ks in counts]
            with_hist = sorted([c for c in counts if c[1]], key=lambda c: (-c[1], c[0]))
            nav, member = getattr(self.app, "_navigator", None), self.app.case.active
            if nav is not None and member is not None and member.db is db:
                nav.set_history(member.uid, dict(with_hist))    # the tables get a mark
            without = [c for c in counts if not c[1]]
            labels, values = {}, []
            if with_hist:
                label = "All tables  (%s changed)" % format(len(self._all_history), ",")
                labels[label] = "__all__"
                values.append(label)
            for t, n in with_hist:
                label = "%s  (%s changed)" % (t, format(n, ","))
                labels[label] = t
                values.append(label)
            for t, _n in without:
                labels[t] = t
                values.append(t)
            self._hist_labels = labels
            self._hist_values = (values, values[:len(with_hist) + 1] if with_hist else [])
            row = self._hist_only_cb.master
            row.show(self._hist_only_cb, bool(with_hist) and len(with_hist) < len(counts))
            self.history_table.configure(values=self._hist_list())
            want = getattr(self, "_hist_want", None)
            self._hist_want = None
            if want and want in labels.values():
                self.show_table_history(want)
            elif with_hist:
                self.history_table.set(values[0])
                self.start_history_summary()
                self.history_status.configure(
                    text="Rows with history in %d of %d tables (listed first, with counts): "
                         "%s%s" % (len(with_hist), len(counts),
                                   ", ".join("%s %s" % (t, format(n, ","))
                                             for t, n in with_hist[:6]),
                                   " \u2026" if len(with_hist) > 6 else ""))
            else:
                self.history_status.configure(
                    text="No table has rows with history: nothing in the WAL or the journal "
                         "differs from the main file (%d tables checked)." % len(counts))
        self._run("history-overview", work, done, self.history_status,
                  "Finding the tables with row history")

    def start_history_summary(self):
        table = self._hist_table()
        if not table or not self.db.ok:
            return
        if table == "__all__":
            self._show_all_history()
            return
        self._history_summary_for = table
        db = self.db

        def work(cancel, progress):
            return db.history_summary(table, cancel)

        def done(summary, seconds):
            self._summary_keys = list(summary.keys) if summary is not None else []
            tree = self.keys_tree
            tree.delete(*tree.get_children())
            show_all = self.history_show_all.get()
            changed_map = dict((k.locator.display(), k) for k in self._summary_keys)
            if show_all:
                self._fill_all_rows(table, changed_map, tree)
                return
            interesting = [k for k in self._summary_keys if k.versions > 1 or k.deleted or k.reuse]
            cap = limits.get("forensics_history_keys")
            for i, k in enumerate(interesting[:cap]):
                tree.insert("", "end", iid=str(i), values=(
                    table, k.locator.display(), k.versions, "yes" if k.deleted else "",
                    "; ".join(k.reuse), "yes" if k.wal_only else "",
                    "%s → %s" % (k.first or "", k.last or "")))
            self._summary_shown = interesting[:cap]
            n_changed = len(summary.multi_version) if summary else 0
            n_deleted = len(summary.deleted) if summary else 0
            n_reused = len(summary.reused) if summary else 0
            if not interesting:
                text = ("No changed rows in \u2018%s\u2019 \u2014 nothing in the WAL/journal differs "
                        "from the main database. Try another table, or type a row id above "
                        "to inspect a specific row.") % table
            else:
                text = ("%s: %d changed rows (%d with multiple versions, %d deleted, %d with "
                        "reused keys) \u2014 click a row to see every version.") % (
                    table, len(interesting), n_changed, n_deleted, n_reused)
            if len(interesting) > cap:
                text += " — the first %s of %s changed rows are listed (limit " \
                        "forensics_history_keys)" % (format(cap, ","), format(len(interesting), ","))
            self.history_status.configure(text=text)
        self._run("history-summary", work, done, self.history_status, "Finding changed rows")

    def _fill_all_rows(self, table, changed_map, tree):
        """Unified view: every row in the table with its history status (versions,
        deleted flag). Changed rows come from the WAL/journal summary; the rest
        are listed as single-version current rows."""
        try:
            total = self.db.count(table)
        except Exception:
            total = None
        # Get all rowids via the session connection
        try:
            from engine.schema import quote_ident
            conn = self.db.session.conn()
            cur = conn.execute("SELECT rowid FROM %s ORDER BY rowid LIMIT %d" % (
                quote_ident(table), limits.get("forensics_history_keys")))
            rows = cur.fetchall()
        except Exception:
            rows = []
        self._summary_shown = []
        cap = limits.get("forensics_history_keys")
        # the live rows, plus the rows only the history knows (deleted, WAL only), in key order
        live = [str(r[0]) for r in rows]
        seen = set(live)
        keys = live + [d for d in changed_map if d not in seen]

        def order(d):
            try:
                return (0, int(d), "")
            except (TypeError, ValueError):
                return (1, 0, d)
        keys.sort(key=order)
        for i, disp in enumerate(keys[:cap]):
            k = changed_map.get(disp)
            if k is not None:
                self._summary_shown.append(k)
                vals = (table, k.locator.display(), k.versions, "yes" if k.deleted else "",
                        "; ".join(k.reuse), "yes" if k.wal_only else "",
                        "%s \u2192 %s" % (k.first or "", k.last or ""))
            else:
                # Unchanged row: single current version
                self._summary_shown.append(None)
                vals = (table, disp, 1, "", "", "", "current only")
            tree.insert("", "end", iid=str(i), values=vals)
        n_changed = len([k for k in changed_map.values() if k.versions > 1 or k.deleted or k.reuse])
        n_deleted = len([k for k in changed_map.values() if k.deleted])
        total_txt = format(total, ",") if total is not None else "?"
        text = ("%s: %s total rows, %d with changes (%d deleted) \u2014 click a row "
                "to see every version.") % (table, total_txt, n_changed, n_deleted)
        if len(rows) >= cap or len(keys) > cap:
            text += " (showing the first %s: limit forensics_history_keys)" % format(cap, ",")
        self.history_status.configure(text=text)

    def _show_all_history(self):
        """Every changed row of every table in one list (found by the overview)."""
        rows = getattr(self, "_all_history", None) or []
        tree = self.keys_tree
        tree.delete(*tree.get_children())
        cap = limits.get("forensics_history_keys")
        self._summary_shown = []
        for i, (t, k) in enumerate(rows[:cap]):
            self._summary_shown.append(k)
            tree.insert("", "end", iid=str(i), values=(
                t, k.locator.display(), k.versions, "yes" if k.deleted else "",
                "; ".join(k.reuse), "yes" if k.wal_only else "",
                "%s \u2192 %s" % (k.first or "", k.last or "")))
        tables = len(set(t for t, _k in rows))
        text = "%s changed rows in %d tables \u2014 click a row to see every version." % (
            format(len(rows), ","), tables)
        if len(rows) > cap:
            text += " The first %s are listed (limit forensics_history_keys)." % format(cap, ",")
        self.history_status.configure(text=text)

    def _on_key(self, _e=None):
        sel = self.keys_tree.selection()
        if sel:
            k = self._summary_shown[int(sel[0])]
            table = self._row_table(sel[0])
            if k is None:
                # Unchanged row in "show all" mode: look up by the displayed rowid
                vals = self.keys_tree.item(sel[0], "values")
                self.show_history(table, parse_key(vals[1]), keep_list=True)
            else:
                self.show_history(table, k.locator, keep_list=True)

    def show_history(self, table, key, keep_list=False):
        """Show every version of one row (key: a Locator, a rowid or a key text). keep_list:
        picked from the changed-rows list (All tables stays chosen)."""
        if not table or key in (None, "") or not self.db.ok:
            return
        self.nb.select(self._history_page)
        if self._hist_table() != table and not keep_list:
            self.history_table.set(table)
        db = self.db

        def work(cancel, progress):
            return db.row_history(table, key, cancel)

        def done(history, seconds):
            self._history = history
            tree = self.versions_tree
            tree.delete(*tree.get_children())
            self.values_tree.delete(*self.values_tree.get_children())
            if history is None:
                self.history_status.configure(
                    text="No such row in %s: %r is not a row id or key of this table" % (
                        table, key))
                return
            for i, v in enumerate(history.versions):
                tags = ("gone",) if not v.present else ("current",) if v.current else ()
                tree.insert("", "end", iid=str(i), tags=tags, values=(
                    i + 1, ("deleted" if not v.present else v.era), v.where(),
                    "" if v.commit_group is None else v.commit_group + 1,
                    ", ".join(v.changed), v.note))
            state = "deleted" if history.deleted else "present"
            notes = list(getattr(history, "notes", ()) or ())
            self.history_status.configure(text="%s row %s: %d versions, now %s%s%s" % (
                table, getattr(history.locator, "display", lambda: key)(), len(history.versions),
                state, ("; key reused: " + "; ".join(history.reuse)) if history.reuse else "",
                (" — NOTE: " + "; ".join(notes)) if notes else ""))
            if history.versions:
                tree.selection_set(str(len(history.versions) - 1))
        self._run("history", work, done, self.history_status, "Reading the row's history")

    def _on_version(self, _e=None):
        sel = self.versions_tree.selection()
        if not sel or self._history is None:
            return
        v = self._history.versions[int(sel[0])]
        tree = self.values_tree
        tree.delete(*tree.get_children())
        if v.values is None:
            tree.insert("", "end", values=("(row deleted here)", "", "", ""))
            return
        self._version_values = list(v.values)
        for i, (c, val) in enumerate(zip(self._history.columns, v.values)):
            tree.insert("", "end", iid=str(i), tags=("changed",) if c in v.changed else (),
                        values=(c, vb(val), value_type(val),
                                date_reading(self.db, self._history.table, c, val)))

    def _history_value_at(self):
        sel = self.values_tree.selection()
        if not sel or self._history is None:
            return None, None
        try:
            i = int(sel[0])
            return self._history.columns[i], self._version_values[i]
        except (AttributeError, IndexError, ValueError):
            return None, None

    def _history_copy_value(self):
        col, v = self._history_value_at()
        if col is not None:
            from engine.export import csv_cell
            self.clipboard_clear()
            self.clipboard_append(csv_cell(v))

    def _history_copy_version(self):
        if self._history is None or not getattr(self, "_version_values", None):
            return
        from engine.export import csv_cell
        self.clipboard_clear()
        self.clipboard_append("\n".join("%s\t%s" % (c, csv_cell(v)) for c, v in
                                         zip(self._history.columns, self._version_values)))

    def _history_rows(self):
        """(header, rows) of every version of the row: version, state, where, commit (from
        1, as the WAL tab), then the columns."""
        h = self._history
        from engine.export import csv_cell
        head = ["version", "state", "where", "commit"] + list(h.columns)
        rows = []
        for i, v in enumerate(h.versions):
            rows.append([i + 1, "deleted" if not v.present else v.era, v.where(),
                         "" if v.commit_group is None else v.commit_group + 1] +
                        [csv_cell(x) for x in (v.values or [None] * len(h.columns))])
        return head, rows

    @staticmethod
    def _csv_text(head, rows):
        import csv
        import io as _io
        out = _io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(head)
        w.writerows(rows)
        return out.getvalue()

    def _history_copy_all(self):
        """Every version of the row as CSV (version, where, commit, then the columns)."""
        if self._history is None:
            return
        self.clipboard_clear()
        self.clipboard_append(self._csv_text(*self._history_rows()))

    def _save_csv(self, name, head, rows, source):
        """Write a CSV the examiner chose, with its manifest (tool, evidence, what it holds);
        never inside an evidence folder. Returns the path, or None."""
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                            initialfile=name,
                                            filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not write_allowed(path):
            return None
        from engine.export import evidence_record, provenance, write_manifest
        protected = getattr(getattr(self.app, "case", None), "is_protected", None)
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(self._csv_text(head, rows))
            files = evidence_record(self.db.evidence, wait=False)
            info = provenance(VERSION, [(self.db.evidence.main, files)], source,
                              columns=head, rows=len(rows))
            write_manifest(path, info, [path], True, protected)
        except (OSError, ValueError) as e:
            self.history_status.configure(text="Could not save %s: %s" % (path, e))
            return None
        self.history_status.configure(text="Saved %s rows to %s (with its .manifest.json)"
                                           % (format(len(rows), ","), path))
        return path

    def _history_save_versions(self):
        h = self._history
        if h is None:
            self.history_status.configure(text="Choose a row first: its versions are saved.")
            return None
        head, rows = self._history_rows()
        key = getattr(h.locator, "display", lambda: "row")()
        return self._save_csv("%s_row_%s_history.csv" % (h.table, key), head, rows,
                              "Row history of %s row %s (every version)" % (h.table, key))

    def _history_save_list(self):
        """The changed-rows list as shown (table, key, versions, deleted, reuse, WAL only,
        span)."""
        tree = self.keys_tree
        head = [tree.heading(c, "text") or c for c in tree["columns"]]
        rows = [list(tree.item(i, "values")) for i in tree.get_children()]
        if not rows:
            self.history_status.configure(text="The list is empty: nothing to save.")
            return None
        return self._save_csv("row_history_changed_rows.csv", head, rows,
                              "Row history: rows with changes (%s)" % self.history_table.get())

    def _history_open_row(self):
        """The row's live version in the row window (a deleted row has none: said)."""
        h = self._history
        if h is None or h.locator is None:
            return
        if h.deleted:
            self.history_status.configure(text="This row is deleted: its versions are listed "
                                               "here; there is no live row to open.")
            return
        from dialogs import RowWin
        RowWin.show(self.app, self.db, h.table, h.locator)

    def _history_value_menu(self, e):
        iid = self.values_tree.identify_row(e.y)
        if not iid:
            return
        self.values_tree.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Copy value", command=self._history_copy_value)
        m.add_command(label="Copy this version", command=self._history_copy_version)
        m.add_command(label="Copy every version (CSV)", command=self._history_copy_all)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _on_history_value(self, _e=None):
        sel = self.values_tree.selection()
        if not sel or self._history is None:
            return
        try:
            val = self._version_values[int(sel[0])]
        except (AttributeError, IndexError, ValueError):
            return
        if isinstance(val, bytes):
            self.app.open_blob(val, self._history.columns[int(sel[0])],
                               "%s row %s (history)" % (self._history.table,
                                                        self._history.locator.display()))

    # -- dropped tables ---------------------------------------------------------
    def _build_dropped(self, f):
        row = ttk.Frame(f)
        row.pack(fill="x", padx=4, pady=(6, 2))
        ttk.Button(row, text="Find dropped tables", command=self.start_dropped).pack(side="left")
        self.drop_status = ttk.Label(row, text="", style="M.TLabel")
        self.drop_status.pack(side="left", padx=8)
        pane = ttk.Panedwindow(f, orient="vertical")
        pane.pack(fill="both", expand=True, padx=4, pady=4)
        top = ttk.Frame(pane)
        pane.add(top, weight=1)
        box, self.drop_tree = self._tree(top, (("type", "Type"), ("name", "Name"),
                                               ("status", "Status"), ("root", "Root page"),
                                               ("readable", "Rows readable"), ("where", "Found at")),
                                         (70, 200, 90, 220, 100, 260), height=6)
        box.pack(fill="both", expand=True)
        self.drop_tree.bind("<<TreeviewSelect>>", self._on_dropped)
        mid = ttk.Frame(pane)
        pane.add(mid, weight=0)
        self.drop_sql = tk.Text(mid, height=4, font=F["mono"], wrap="word", bg=C["bg2"],
                                state="disabled")
        self.drop_sql.pack(fill="both", expand=True)
        low = ttk.Frame(pane)
        pane.add(low, weight=2)
        self.drop_grid = DataGrid(low, frozen=1)
        grid_search(low, self.drop_grid, placeholder="Find rows of the dropped table…").pack(
            fill="x", pady=(2, 2))
        self.drop_grid.pack(fill="both", expand=True)

    def start_dropped(self):
        if not self.db.ok:
            return
        db = self.db

        def work(cancel, progress):
            return db.dropped_schema()

        def done(objects, seconds):
            self._dropped = list(objects or [])
            tree = self.drop_tree
            tree.delete(*tree.get_children())
            for i, o in enumerate(self._dropped):
                tree.insert("", "end", iid=str(i), values=(
                    o.type, o.name, o.status, "%s %s" % (o.rootpage, o.root_status),
                    "yes" if o.readable else "no", o.record.prov.where()))
            self.drop_status.configure(text="%d dropped or redefined schema objects (%.1fs)"
                                            % (len(self._dropped), seconds))
        self._run("dropped", work, done, self.drop_status, "Finding dropped tables")

    def _on_dropped(self, _e=None):
        sel = self.drop_tree.selection()
        if not sel:
            return
        o = self._dropped[int(sel[0])]
        self._set_text(self.drop_sql, o.sql or "")
        if not o.readable or o.info is None:
            self.drop_grid.set_source(None)
            return
        cols = [c.name for c in o.info.columns]
        cap = limits.get("forensics_dropped_rows")
        rows = [([loc] + list(row), flags) for loc, row, flags in o.rows(limit=cap)]
        self.drop_grid.set_source(ListSource([RID] + cols, rows))
        self.drop_status.configure(text="%s: %s rows readable%s" % (
            o.name, format(len(rows), ","),
            " — the first %s are shown, there may be more (limit forensics_dropped_rows)"
            % format(cap, ",") if len(rows) >= cap else ""))

    # -- rollback journal --------------------------------------------------------
    def _build_journal(self, f):
        self.journal_text = tk.Text(f, height=6, font=F["mono"], wrap="word", bg=C["bg2"],
                                    state="disabled")
        self.journal_text.pack(fill="x", padx=4, pady=(6, 2))
        row = ttk.Frame(f)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="Table before the journaled transaction:").pack(side="left")
        self.journal_table = SearchableCombobox(row, state="readonly", width=28)
        self.journal_table.pack(side="left", padx=4)
        self.journal_show = ttk.Button(row, text="Show rows", command=self.show_journal_rows)
        self.journal_show.pack(side="left")
        self.journal_status = ttk.Label(row, text="", style="M.TLabel")
        self.journal_status.pack(side="left", padx=8)
        self.journal_grid = DataGrid(f, frozen=1)
        grid_search(f, self.journal_grid, placeholder="Find rows…").pack(fill="x", padx=4)
        self.journal_grid.pack(fill="both", expand=True, padx=4, pady=4)

    @staticmethod
    def _journal_summary(info):
        lines = ["Rollback journal: %s (%s)" % (info.get("path"), fmtb(info.get("size") or 0)),
                 "Hot (an unfinished transaction): %s   Header zeroed: %s" % (
                     "yes" if info.get("hot") else "no", "yes" if info.get("header_zeroed") else "no"),
                 "Page size %s, database size before the transaction: %s pages" % (
                     info.get("page_size"), info.get("initial_pages")),
                 "%d page records (%d distinct pages), %d checksum failures, %d segments" % (
                     info.get("records", 0), info.get("pages", 0),
                     info.get("checksum_failures", 0), len(info.get("segments") or []))]
        if info.get("records_capped_at"):
            lines.append("Reading stopped at %s page records: limit journal_records (Limits…); "
                         "later records of the journal were not read."
                         % format(info["records_capped_at"], ","))
        return "\n".join(lines)

    def show_journal_rows(self):
        table = self.journal_table.get()
        if not table or not self.db.ok:
            return
        db = self.db

        cap = limits.get("forensics_journal_rows")

        def work(cancel, progress):
            return db.journal_rows(table, cap)

        def done(rows, seconds):
            cols = [RID] + [name for name, _t in db.columns(table)]
            data = [([loc] + list(row), flags) for loc, row, flags in rows]
            self.journal_grid.set_source(ListSource(cols, data))
            self.journal_status.configure(text="%d rows of %s as they were before the "
                                               "transaction (%.1fs)%s" % (
                len(data), table, seconds,
                " — the first %s are shown, there may be more (limit forensics_journal_rows)"
                % format(cap, ",") if len(data) >= cap else ""))
        self._run("journal", work, done, self.journal_status, "Reading the journal")

    # -- audit -------------------------------------------------------------------
    def _build_audit(self, f):
        row = ttk.Frame(f)
        row.pack(fill="x", padx=4, pady=(6, 2))
        ttk.Button(row, text="Run audit", command=self.start_audit).pack(side="left")
        self.audit_status = ttk.Label(row, text="", style="M.TLabel")
        self.audit_status.pack(side="left", padx=8)
        box, self.audit_tree = self._tree(f, (("level", "Level"), ("code", "Check"),
                                              ("message", "Finding")), (80, 200, 700), height=12)
        box.pack(fill="both", expand=True, padx=4, pady=4)
        for level, (bg, fg) in LEVEL_COLOURS.items():
            self.audit_tree.tag_configure(level, background=bg, foreground=fg)
        self.audit_tree.bind("<<TreeviewSelect>>", self._on_finding)
        self.audit_details = tk.Text(f, height=6, font=F["mono"], wrap="word", bg=C["bg2"],
                                     state="disabled")
        self.audit_details.pack(fill="x", padx=4, pady=(0, 4))

    def _on_page(self, _e=None):
        try:
            current = self.nb.index(self.nb.select())
        except tk.TclError:
            return
        if self.nb.select() == str(self._audit_page) and self._findings is None and \
                self.db.ok and not getattr(self, "_busy", False):
            self.start_audit()
        if self.nb.select() == str(self._history_page) and self.db.ok and \
                not getattr(self, "_busy", False):
            if getattr(self, "_hist_overview_for", None) is not self.db.session:
                self.start_history_overview()       # which tables have rows with history
            elif getattr(self, "_history_summary_for", None) != self._hist_table() and \
                    not getattr(self, "_hist_no_sources", False):
                self.start_history_summary()

    def start_audit(self):
        if not self.db.ok:
            return
        db = self.db

        def work(cancel, progress):
            return db.audit_findings()

        def done(findings, seconds):
            self._findings = list(findings or [])
            tree = self.audit_tree
            tree.delete(*tree.get_children())
            order = {"error": 0, "warning": 1, "info": 2}
            self._findings.sort(key=lambda x: order.get(x.level, 3))
            for i, fnd in enumerate(self._findings):
                tree.insert("", "end", iid=str(i), tags=(fnd.level,),
                            values=(fnd.level, fnd.code, fnd.message))
            counts = dict((lv, sum(1 for x in self._findings if x.level == lv)) for lv in order)
            self.audit_status.configure(text="%d errors, %d warnings, %d notes (%.1fs)" % (
                counts["error"], counts["warning"], counts["info"], seconds))
        self._run("audit", work, done, self.audit_status, "Auditing the file")

    def _on_finding(self, _e=None):
        sel = self.audit_tree.selection()
        if sel and self._findings:
            fnd = self._findings[int(sel[0])]
            details = fnd.details
            if isinstance(details, dict):
                details = "\n".join("%s: %s" % kv for kv in sorted(details.items()))
            self._set_text(self.audit_details, "%s\n\n%s" % (fnd.message, details or ""))

    # -- reports -------------------------------------------------------------
    def _report_dialog(self):
        if not self.db.ok:
            return
        parts = {}
        if self._records:
            parts["records"] = self.filtered_records()
        if self._findings is not None:
            parts["findings"] = self._findings
        if self._dropped:
            parts["dropped"] = self._dropped
        if not parts:
            if not messagebox.askyesno(
                    "Forensic report", "Nothing has been recovered yet. Write a report with the "
                    "evidence hashes and an audit only?", parent=self):
                return
        self._write_report(parts, "forensic_report", audit=("findings" not in parts))

    def _write_report(self, parts, stem, audit=False):
        from jobs import export_options
        opts = export_options(self.app, "Forensic report", [], formats=("html", "csv", "json"),
                              blobs=False,
                              note="The report names the tool, the time (UTC) and the evidence "
                                   "files with their SHA-256; it is never written into the "
                                   "evidence folder.", ok_text="Write…")
        if opts is None:
            return
        fmt = opts["fmt"]
        path = filedialog.asksaveasfilename(
            parent=self, initialfile="%s.%s" % (stem, fmt), defaultextension="." + fmt,
            filetypes=[(fmt.upper(), "*." + fmt)])
        if not path or not write_allowed(path):
            return
        db = self.db
        member = self.app.case.active
        status = self.carve_status if "records" in parts else self.audit_status
        what = ", ".join(sorted(parts)) or "evidence hashes and an audit"

        def work(cancel, progress):
            from jobs import write_export_manifest
            findings = parts.get("findings")
            if audit and findings is None:
                findings = db.audit_findings()
            written = db.write_forensic_report(path, fmt, records=parts.get("records"),
                                               findings=findings, dropped=parts.get("dropped"))
            manifest = write_export_manifest(
                self.app, written, "forensic report (%s)" % what, [member], [written], True,
                rows=len(parts.get("records") or ()))
            return written, manifest

        def done(result, seconds):
            from jobs import manifest_text
            written, (manifest, why) = result
            status.configure(text="Report written: %s" % written)
            self.app.activity("export", what="forensic report (%s)" % what, path=written,
                              format=fmt, manifest=manifest, manifest_error=why)
            (messagebox.showinfo if manifest else messagebox.showwarning)(
                "Forensic report", "Written to:\n%s\n\n%s" % (written,
                                                              manifest_text(manifest, why)),
                parent=self)
        self._run("report", work, done, status, "Writing the report")
