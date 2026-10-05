"""Timeline tab: every dated row of the database in time order.

The date columns are found by engine.timeline.detect() (by name and by sampled values) when the
tab is first shown; the list on the left shows each one with its kind, confidence and reason,
and a tick includes it. Right-click a column to read it as another kind or leave it out; these
choices are kept per database with the tags (never next to the evidence). Build Timeline reads
the events on a worker thread (SQL for the tables SQLite serves, natively otherwise), capped
per column, optionally only between two dates, and optionally with the WAL row versions and the
records the Forensics tab recovered. Events show in the virtual grid; double-click opens the
row, right-click tags it, and the shown events export to CSV, JSON or HTML.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
import tkinter as tk
from collections import OrderedDict
from tkinter import filedialog, messagebox, ttk

from constants import C, VERSION, wal_state_label
from database import RID
from engine import limits
from engine import timeline as tl
from engine.backends import Filter
from engine.filters import parse_words
from engine.session import sort_key
from relations_view import value_text
from engine.tags import entry_from_db_row, entry_from_record, entry_from_wal_record
from grid import DataGrid, Runner
from combobox import SearchableCombobox
from datepicker import DateRangePicker
from density import DensityChart
from parts import HoverCard, StatusLine, Toolbar
from scope import ScopePicker
from tokens import COLOR as K, FONT as F, XS, S, M
from utils import write_allowed
from widgets import ElideLabel, SearchBox, ToolTip, menu_button, next_line

SECTION = "timeline"


def format_event_time(dt, fmt="iso", custom=""):
    """Format a naive UTC datetime for the timeline grid (engine.timeline.format_dt)."""
    return tl.format_dt(dt, fmt, custom)
CAPS = (("10,000", 10000), ("50,000", 50000), ("200,000", 200000), ("1,000,000", 1000000))


def _unset(v):
    """0, negative numbers and empty values are 'not set' in date columns, never dates."""
    if v is None or v == "" or v == b"":
        return True
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return v <= 0
    if isinstance(v, str):
        try:
            return float(v.strip()) <= 0
        except ValueError:
            return False
    return False


def caps():
    """The 'Max events per column' choices: the usual ones and the limit timeline_column_events
    (the default), in order."""
    default = limits.get("timeline_column_events")
    out = dict(CAPS)
    out[format(default, ",")] = default
    return sorted(out.items(), key=lambda kv: kv[1])
# the zone choices: 'UTC', then every offset with the places that use it ('UTC+05:30 ·
# India, Sri Lanka'), this computer's marked
OFFSETS = ["UTC"] + [tl.zone_label(m) for m in
                     sorted(set(list(range(-12 * 60, 15 * 60, 60)) + [-210, 210, 270, 330, 345,
                                                                     390, 570, 630]))
                     if m]
CHECK, UNCHECK, PARTIAL = "☑", "☐", "◪"
COLS_BREAKPOINT = 820           # a tab narrower than this folds the date-column panel


class EventSource(object):
    """DataGrid source over a list of engine.timeline Events (rows made when drawn, so a
    million events cost no more than the events themselves). The first column is the event's
    number in the list; the global and column filters use the engine's rules.

    Threaded: sort() and set_filters() only note what is asked; the view is rebuilt when rows
    are next read, on the grid's worker thread, so sorting or filtering a million events never
    holds the Tk thread (row_count() is None until then)."""

    threaded = True

    def __init__(self, events, offset=0, encoding="utf-8", databases=False,
                 time_format="iso", time_custom=""):
        self.events = events
        self.offset = offset
        self.encoding = encoding
        self.databases = databases      # events of several databases: a Database column
        self.time_format = time_format  # 'iso', '12h' or 'custom' (format_event_time)
        self.time_custom = time_custom  # the strftime string when 'custom'
        self.note = ""
        self._cols = [RID, "Time (UTC)"] + (["Time (%s)" % tl.offset_label(offset)]
                                             if offset else []) + \
            (["Database"] if databases else []) + \
            ["Table", "Column", "Kind", "Row", "Source", "Description", "Raw"]
        self.order, self.desc, self.flt = None, False, None
        self.time_range = None          # (start, end): the density chart's selection
        self._view = range(len(events))
        self._lock = threading.Lock()
        self._asked = self._built = 0    # generations of the view asked for / built

    def columns(self):
        return list(self._cols)

    def values(self, i):
        e = self.events[i]
        fmt = lambda dt: format_event_time(dt, self.time_format, self.time_custom)
        out = [i + 1, fmt(e.when)]
        if self.offset:
            out.append(fmt(e.when + timedelta(minutes=self.offset)))
        if self.databases:
            out.append(e.database)
        out.extend([e.table, e.column, tl.SHORT.get(e.kind, e.kind), e.row, e.source,
                    e.description, e.raw])
        return out

    def row_count(self):
        return len(self._view) if self._built == self._asked else None

    def rows(self, start, count):
        view = self._current()
        return [(self.values(i), ()) for i in view[start:start + count]]

    def sort(self, column, desc):
        self.order = None if column in (None, RID) else column
        self.desc = bool(desc)
        self._asked += 1

    def set_filters(self, col_exprs, global_text):
        flt = Filter(col_exprs=col_exprs, words=parse_words(global_text))
        self.flt = flt if flt else None
        self._asked += 1

    def _current(self):
        """The view for the sort and filters asked for (rebuilt here when they changed)."""
        with self._lock:
            want = self._asked
            if self._built != want:
                view = self._rebuild_view()
                self._view, self._built = view, want
            return self._view

    def set_time_range(self, start, end):
        """Keep only the events from start to end (None, None: all), as the density chart's
        drag selection asks."""
        self.time_range = None if start is None and end is None else (start, end)
        self._asked += 1

    @property
    def filtered(self):
        return self.flt is not None or self.time_range is not None

    @property
    def total(self):
        return len(self.events)

    def count_rows(self):
        return self.flt, len(self._current())

    def set_count(self, flt, n):
        pass

    def iter_rows(self):
        for i in self._current():
            yield self.values(i)

    def iter_all(self):
        for i in range(len(self.events)):
            yield self.values(i)

    def view_events(self):
        """The events the grid shows now, in its order (filters and sort applied)."""
        return [self.events[i] for i in self._current()]

    def event_at(self, values):
        try:
            return self.events[int(values[0]) - 1]
        except (IndexError, TypeError, ValueError):
            return None

    def _rebuild_view(self):
        view = range(len(self.events))
        if self.time_range is not None:
            lo, hi = self.time_range
            ev = self.events
            view = [i for i in view if (lo is None or ev[i].when >= lo) and
                    (hi is None or ev[i].when <= hi)]
        if self.flt is not None:
            data_cols, flt, enc = self._cols[1:], self.flt, self.encoding
            view = [i for i in view if flt.matches(data_cols, self.values(i)[1:], enc)]
        if self.order in self._cols:
            ci = self._cols.index(self.order)
            if ci == 1 or (self.offset and ci == 2):
                key = lambda i: self.events[i].when         # noqa: E731
            else:
                key = lambda i: sort_key(self.values(i)[ci])  # noqa: E731
            view = sorted(view, key=key, reverse=self.desc)
        elif self.desc:
            view = list(reversed(view))
        return view


class CaseDetection(object):
    """The date columns of several databases as one list: each TimeColumn names its database
    (a case member's uid); per database, its own engine.timeline Detection."""

    def __init__(self, parts):
        self.parts = list(parts)         # [(member, Detection)]
        self.columns, self.notes = [], []
        self.tables, self.cancelled, self.seconds = 0, False, 0.0
        for m, d in self.parts:
            for c in d.columns:
                c.database = m.uid
            self.columns.extend(d.columns)
            self.notes.extend("%s: %s" % (m.name, n) for n in d.notes)
            self.tables += d.tables
            self.cancelled = self.cancelled or d.cancelled
            self.seconds = max(self.seconds, d.seconds)

    def detected(self):
        return [c for c in self.columns if c.kind is not None]

    def enabled(self):
        return [c for c in self.columns if c.enabled]

    def get(self, table, column, database=None):
        for c in self.columns:
            if c.table == table and c.column == column and \
                    (database is None or c.database == database):
                return c
        return None

    def of(self, member):
        for m, d in self.parts:
            if m is member:
                return d
        return None


class TimelineTab(ttk.Frame):
    """The Timeline tab of App (app gives db, tags, the Forensics tab's records and the row
    windows). In a case of several databases it covers the databases the Search tab's
    'Databases:' button ticks, detects and reads them side by side, and merges their events
    (a Database column says where each one is from)."""

    def __init__(self, parent, app):
        ttk.Frame.__init__(self, parent)
        self.app = app
        self._runner = Runner(self, "timeline", release=app._release_worker_connection)
        self._stop = [False]             # the running job's cancel flag
        self._gen = 0
        self._busy = None                # 'detect' / 'build' while a job runs
        self._progress = (0, 0)
        self._pool_threads = []          # threads reading databases side by side
        st = self.app.tags.state_section(SECTION)
        self.timefmt_var = tk.StringVar(value=st.get("time_format", "iso") or "iso")
        if self.timefmt_var.get() not in tl.TIME_FORMAT_KEYS:
            self.timefmt_var.set("iso")
        self._time_custom = st.get("time_custom", "")
        self._preview_open = st.get("preview_open", True)
        self._sample_cache = {}
        self._preview_after = None
        self._preview_ev = None
        self._applied_timefmt = self.timefmt_var.get()  # last format actually in effect
        self.detection = None
        self.members = []                # the databases detected (a case)
        self.result = None               # the last EventSet (events of every source)
        self._shown_iids = []
        self._has_wal = True
        self._build_after_detect = False
        self._group_iids = {}            # database uid -> its group line in the column list
        self._cols_auto_hidden = False
        self._build()
        self.bind("<Configure>", self._on_resize, add="+")
        self.bind("<Map>", lambda e: self._on_shown())

    @property
    def db(self):
        return self.app.db

    def multi(self):
        return len(self.members) > 1

    def _scope(self):
        """The databases the timeline covers: the active one, or in a case those its scope
        picker covers (the global scope unless it has its own)."""
        case = getattr(self.app, "case", None)
        if case is None or len(case) <= 1:
            return [case.active] if case is not None and case.active is not None else []
        scopes = getattr(self.app, "scopes", None)
        if scopes is None:
            return self.app._search_members()
        return [m for m in scopes.members("timeline") if m.db.ok]

    def refresh_scope(self):
        """The case changed: the scope picker shows only for a case of several databases."""
        picker = getattr(self, "scope_picker", None)
        if picker is None:
            return
        multi = len(getattr(self.app, "case", ()) or ()) > 1
        self.top.show(picker, multi)
        self.top.show(self._scope_rule, multi)
        picker.refresh()

    def _scope_changed(self):
        """The scope changed: with nothing shown yet, look again (when shown); with a
        timeline shown, keep it and say that Build reads the new scope."""
        if self.detection is None and self.result is None:
            self.on_open()
            return
        self._stale_scope = True
        self.status.set("The scope is now %s: Build timeline reads them (the events shown "
                        "are those of the previous scope)" % self.app.scopes.text("timeline"))

    def _member(self, uid):
        for m in self.members:
            if m.uid == uid:
                return m
        return None

    def member_of_event(self, ev):
        """The case member an event came from (None: the active database)."""
        if not ev.database:
            return None
        return next((m for m in self.members if m.name == ev.database), None)

    def _each(self, members, fn):
        """fn(i, member) for each database, case_timeline_parallel at once (each on its own
        thread, which closes its connections when done); the results in order."""
        if len(members) <= 1:
            return [fn(i, m) for i, m in enumerate(members)]
        release = self.app._release_worker_connection
        threads = self._pool_threads = []

        def task(i, m):
            threads.append(threading.current_thread())
            try:
                return fn(i, m)
            finally:
                try:
                    release()
                except Exception:       # noqa: BLE001 - nothing to report it to
                    pass
        with ThreadPoolExecutor(max_workers=min(limits.get("case_timeline_parallel"),
                                                len(members))) as ex:
            futures = [ex.submit(task, i, m) for i, m in enumerate(members)]
            return [f.result() for f in futures]

    # -- lifecycle -------------------------------------------------------------------------------
    def reset(self):
        """Forget the database shown (closing, or opening another)."""
        self.stop()
        self._gen += 1                   # a job still finishing is ignored
        self._busy = None
        self.detection = self.result = None
        self.members = []
        self._build_after_detect = False
        self._stale_scope = False
        self.grid.set_source(None)
        self.col_tree.delete(*self.col_tree.get_children())
        self._shown_iids = []
        self.status.set("")
        self.col_status.configure(text="")
        self.cols_head.configure(text="Date columns")
        self.chart.set_data([])
        self.sources.configure(text="")
        self.bar.configure(value=0)

    def on_open(self):
        """A database was opened: restore its saved choices; detection runs when the tab is
        shown."""
        self.reset()
        st = self.app.tags.state_section(SECTION)
        off = st.get("offset") if isinstance(st.get("offset"), int) else 0
        self.offset_var.set(tl.zone_label(off) if off else "UTC")
        self.wal_var.set(bool(st.get("wal", True)))
        self.rec_var.set(bool(st.get("recovered", True)))
        scope = self._scope() or [None]
        has_wal = any((m.db if m is not None else self.db).has_wal for m in scope)
        self._has_wal = has_wal
        self._update_recovered_label()
        self.refresh_scope()
        if self.winfo_ismapped():
            self._on_shown()

    def reads(self, member):
        """Whether a detection or build running now reads this database."""
        return self.busy() and (member in self.members or member in self._scope())

    def forget_member(self, member, old_name, renamed):
        """A database left the case (its detection or build was stopped before, if one was
        reading it). What the tab shows of the others stays: the database's date columns and
        events are dropped, the other databases' events keep theirs (renamed: {old name: new
        name} when the display names changed), and the status says what went. Nothing shown
        yet: the tab starts over for the databases that stay."""
        if member not in self.members or (self.detection is None and self.result is None):
            self.on_open()
            return
        self.members = [m for m in self.members if m is not member]
        det = self.detection
        if isinstance(det, CaseDetection):
            det.parts = [(m, d) for m, d in det.parts if m is not member]
            det.columns = [c for c in det.columns if c.database != member.uid]
        res = self.result
        dropped = 0
        if res is not None:
            kept = [e for e in res.events if e.database != old_name]
            dropped = len(res.events) - len(kept)
            for e in kept:
                e.database = renamed.get(e.database, e.database)
            res.events = kept
            res.per_database = [(m, n) for m, n in getattr(res, "per_database", [])
                                if m is not member]
        self._fill_columns()
        self._show_events()
        self.status.configure(text="%s left the case: %s removed; the other databases' %s "
                                   "stay." % (old_name, ("its %s and date columns were" % (
                                       "1 event" if dropped == 1 else
                                       "%s events" % format(dropped, ",")))
                                   if res is not None else "its date columns were",
                                   "events" if res is not None else "columns"))

    def stop(self):
        """Stop the running detection or build (its SQL statement is interrupted)."""
        self._stop[0] = True
        self._build_after_detect = False  # a queued build is cancelled too
        self._runner.cancel()
        running = self._runner.running_thread()
        if running is None:
            self._busy = None           # a queued job was dropped: it never reports back
            self.top.show(self.stop_btn, False)
        else:
            case = getattr(self.app, "case", None)
            for th in [running] + [t for t in self._pool_threads if t.is_alive()]:
                if case is not None:
                    case.interrupt(th)  # a thread may read any database of the case
                elif self.db.ok:
                    self.db.interrupt(th)

    def worker_threads(self):
        return self._runner.threads() + [t for t in self._pool_threads if t.is_alive()]

    def busy(self):
        return self._busy is not None or self._runner.busy()

    def _on_shown(self):
        self._update_recovered_label()
        if self.db.ok and self.detection is None and self._busy is None:
            self.start_detect(again=False)

    def _save_state(self, **changes):
        st = self.app.tags.state_section(SECTION)
        st.update(changes)
        self.app.tags.set_state_section(SECTION, st)

    # -- layout ----------------------------------------------------------------------------------
    def _build(self):
        # the top bar: scope | date range | time zone | Build (primary), Stop, Options ▾
        top = self.top = Toolbar(self)
        top.pack(fill="x", padx=M, pady=(M, XS))
        self.scope_picker = top.add(ScopePicker(top, self.app, "timeline",
                                                on_change=self._scope_changed), visible=False)
        self._scope_rule = top.add(ttk.Separator(top, orient="vertical"), gap=S, visible=False)
        self.range = top.add(DateRangePicker(top, reference_time=self._reference_time,
                                             width=19, show_zone=False), gap=S)
        # the range picker's two fields hold the From / To text (UTC) the build reads
        self.from_var, self.to_var = self.range.start.var, self.range.end.var
        for e in (self.range.start, self.range.end):
            e.entry.bind("<Return>", lambda ev: self.start_build(), add="+")
            ToolTip(e.entry, "YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS], in UTC. Empty: no limit.\n"
                       "Only this range is read (SQLite filters the raw values), so a large\n"
                       "database answers quickly and the per-column cap applies to the range.\n"
                       "The presets count back from the newest date found.")
        top.group()
        self.offset_var = tk.StringVar(value="UTC")
        # the label and its list are one item, so a narrow tab never wraps them apart
        zone_box = ttk.Frame(top)
        ttk.Label(zone_box, text="Timezone:").pack(side="left", padx=(0, XS))
        self.offset_combo = SearchableCombobox(zone_box, textvariable=self.offset_var,
                                               values=OFFSETS, width=26, state="readonly")
        self.offset_combo.pack(side="left")
        top.add(zone_box, gap=0)
        self.offset_combo.bind("<<ComboboxSelected>>", lambda e: self._offset_changed())
        ToolTip(self.offset_combo,
                "How times are shown. Timestamps are always read as UTC - this only\n"
                "changes the display, never the data. UTC is the safe forensic default.\n"
                "Picking a zone adds a second column with the time in that zone.")
        self.timefmt_btn, self.timefmt_menu = menu_button(top, "Format ▾")
        top.add(self.timefmt_btn)
        for _k, _label, _b, _a in tl.TIME_FORMATS:
            if _k == "custom":
                self.timefmt_menu.add_radiobutton(label=_label,
                                                  variable=self.timefmt_var, value=_k,
                                                  command=self._custom_time_format)
            else:
                self.timefmt_menu.add_radiobutton(label=_label,
                                                  variable=self.timefmt_var, value=_k,
                                                  command=self._time_format_changed)
        ToolTip(self.timefmt_btn, "How the Time column shows timestamps")
        self._update_timefmt_button()
        top.group()
        self.build_btn = top.add(ttk.Button(top, text="Build timeline", style="Primary.TButton",
                                            command=self.start_build))
        self.stop_btn = top.add(ttk.Button(top, text="Stop", command=self.stop),
                                      visible=False)
        self.opts_btn, opts = menu_button(top, "Options ▾",
                                        postcommand=self._update_recovered_label)
        self.opts_menu = opts
        top.add(self.opts_btn, gap=S)
        self.wal_var = tk.BooleanVar(value=True)
        self.rec_var = tk.BooleanVar(value=True)
        opts.add_checkbutton(label="Include WAL row versions", variable=self.wal_var,
                             command=lambda: self._save_state(wal=self.wal_var.get()))
        opts.add_checkbutton(label="Include recovered records", variable=self.rec_var,
                             command=lambda: self._save_state(recovered=self.rec_var.get()))
        self.cap_var = tk.StringVar(value=format(limits.get("timeline_column_events"), ","))
        capm = tk.Menu(opts, tearoff=0)
        for label, _n in caps():
            capm.add_radiobutton(label=label, value=label, variable=self.cap_var)
        opts.add_cascade(label="Max events per column", menu=capm)
        opts.add_separator()
        opts.add_command(label="Look for date columns again", command=self.start_detect)
        ToolTip(self.opts_btn, "Include WAL row versions (older versions and deleted rows "
                               "held by WAL frames), include the records recovered in "
                               "Forensics, the newest events read per date column (limit "
                               "timeline_column_events), look for date columns again.")
        self.bar = top.add(ttk.Progressbar(top, mode="determinate", length=120), gap=S,
                             visible=False)
        self.status = StatusLine(self)
        self.status.pack(fill="x", padx=M, pady=(0, XS))
        self._status_tip = self.status.label._tip

        pane = self.pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=M, pady=(0, S))
        try:        # the panel grows with the display scaling (Tk's is 1.33 at 100%)
            f = max(1.0, float(self.tk.call("tk", "scaling")) / (96 / 72.0))
        except (tk.TclError, ValueError):
            f = 1.0
        left = self.left = ttk.Frame(pane, width=int(380 * f))
        pane.add(left, weight=0)
        right = ttk.Frame(pane)
        pane.add(right, weight=1)
        self._cols_open = True

        head = ttk.Frame(left)
        head.pack(fill="x", pady=(0, XS))
        self.cols_head = ElideLabel(head, text="Date columns", style="Heading.TLabel")
        self.cols_hide = ttk.Button(head, text="◂", width=2, style="Icon.TButton",
                                    command=self._toggle_columns_by_hand)
        self.cols_hide.pack(side="right")
        self.cols_head.pack(side="left", fill="x", expand=True)
        ToolTip(self.cols_hide, "Hide the date columns (the events get the room)")
        self.col_search = SearchBox(
            left, placeholder="Find a database, table or column…", delay=0, find_button=False,
            width=16, count_below=True, on_change=lambda t: self._fill_columns(),
            on_next=lambda forward: next_line(self.col_tree, forward),
            tooltip="Show only the columns whose database, table or column name contains "
                    "this text; Select all listed / Clear listed then act on the columns listed.")
        self.col_search.pack(fill="x", pady=(0, XS))
        self.col_filter = self.col_search.var
        box = ttk.Frame(left)
        box.pack(fill="both", expand=True)
        self.col_tree = ttk.Treeview(box, columns=("on", "db", "table", "column", "kind", "conf"),
                                     show="headings", selectmode="extended")
        for c, text, w in (("on", "", 28), ("db", "Database", 90), ("table", "Table", 110),
                           ("column", "Column", 110), ("kind", "Kind", 110), ("conf", "Conf.", 80)):
            self.col_tree.heading(c, text=text)
            self.col_tree.column(c, width=w, stretch=c in ("table", "column"))
        self.col_tree.configure(displaycolumns=("on", "table", "column", "kind", "conf"))
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.col_tree.yview)
        self.col_tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.col_tree.pack(fill="both", expand=True)
        self.col_tree.tag_configure("off", foreground=C["text2"])
        self.col_tree.tag_configure("override", foreground=C["purple"])
        self.col_tree.tag_configure("dbgroup", font=F["body_bold"], foreground=K["heading"])
        self.col_tree.tag_configure("empty", foreground=C["text2"])
        self.col_tree.bind("<Button-1>", self._on_col_click)
        self.col_tree.bind("<space>", lambda e: self._toggle_selected())
        self.col_tree.bind("<Button-3>", self._on_col_menu)
        self.col_tree.bind("<<TreeviewSelect>>", lambda e: self._show_reason())
        HoverCard(self.col_tree, self._col_hover)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(XS, 0))
        ttk.Button(btns, text="Select all listed", style="Small.TButton",
                   command=lambda: self.set_shown(True)).pack(side="left")
        ttk.Button(btns, text="Clear listed", style="Small.TButton",
                   command=lambda: self.set_shown(False)).pack(side="left", padx=XS)
        self.col_status = ElideLabel(left, text="", style="Muted.TLabel")
        self.col_status.pack(fill="x", pady=(XS, 0))

        # the columns panel folded away: a thin strip to bring it back
        self.cols_show = ttk.Button(right, text="▸ Date columns", style="Link.TButton",
                                    command=self._toggle_columns_by_hand)
        chart_row = ttk.Frame(right)
        chart_row.pack(fill="x", pady=(0, XS))
        self.chart = DensityChart(chart_row, on_range=self._range_selected, height=92)
        self.chart.pack(fill="x")
        srow = ttk.Frame(right)
        srow.pack(fill="x", pady=(0, XS))
        self.sources = ElideLabel(srow, text="", style="Muted.TLabel")
        self.split_var = tk.BooleanVar(value=True)
        self.split_cb = ttk.Checkbutton(srow, text="Colour by database", variable=self.split_var,
                                        command=lambda: self.chart.set_split(
                                            self.split_var.get()))
        self.split_cb.pack(side="right")
        self.sources.pack(side="left", fill="x", expand=True)
        self._split_shown = True

        frow = ttk.Frame(right)
        frow.pack(fill="x", pady=(0, 2))
        self.search = SearchBox(
            frow, placeholder="Find events (words, all must match)…", delay=400,
            find_button=False, primary=True, width=30,
            on_change=lambda t: self.grid.set_global_filter(t, True),
            on_next=lambda forward: self.grid.focus_set(),
            tooltip="Keeps the events where every word appears, as you type (sorting and "
                    "filtering run on a worker). Column filters are under the headers.")
        self.filter_var = self.search.var
        self.export_btn = ttk.Button(frow, text="Export ▾")
        self.export_menu = tk.Menu(self, tearoff=0)
        for fmt in ("HTML", "JSON", "CSV"):
            self.export_menu.add_command(label="Export %s…" % fmt,
                                         command=lambda f=fmt.lower(): self.export_dialog(f))
        self.export_btn.configure(command=lambda: self.app._post_menu(self.export_menu,
                                                                      self.export_btn))
        self.export_btn.pack(side="right", padx=2)
        # packed after the button: a narrow window shrinks the search field, not the button
        self.search.pack(side="left", fill="x", expand=True, padx=4)
        self.grid = DataGrid(right, frozen=1, on_open_row=self._open_row,
                             on_context_menu=self._grid_menu, row_style=self._row_style,
                             on_selection=self._on_grid_selection)
        self.grid.use_search_box(self.search)       # the tab's own search field, not two
        self.grid.pack(fill="both", expand=True)
        # the inline preview: the selected event, without opening its row window
        pv = self.preview = ttk.Frame(right)
        pv.pack(fill="x", pady=(XS, 0))
        prow = ttk.Frame(pv)
        prow.pack(fill="x")
        self.preview_toggle = ttk.Button(prow, text="", style="Link.TButton",
                                         command=self._toggle_preview)
        self.preview_toggle.pack(side="left")
        self.preview_head = ElideLabel(prow, text="", style="Muted.TLabel")
        self.preview_head.pack(side="left", fill="x", expand=True, padx=(XS, 0))
        ttk.Button(prow, text="Open row", style="Small.TButton",
                   command=self._open_previewed).pack(side="right")
        self.preview_browse = ttk.Button(prow, text="Browse table", style="Small.TButton",
                                         command=lambda: self.browse_event(self._preview_ev))
        self.preview_browse.pack(side="right", padx=(0, XS))
        ToolTip(self.preview_browse, "Open the selected event's table in Browse")
        self.preview_text = tk.Text(pv, height=5, wrap="word", bd=0, highlightthickness=0,
                                    font=F["small"], bg=C["bg"], fg=C["text"],
                                    state="disabled")
        self.preview_text.pack(fill="x", pady=(2, 0))
        self._layout_preview()

    # -- the top bar and panels -------------------------------------------------------------------
    def _reference_time(self):
        """What the date presets count back from: the newest event built, else the newest
        date the detection sampled, else now (evidence is rarely from today)."""
        res = self.result
        if res is not None and res.events:
            return max(e.when for e in res.events)
        det = self.detection
        if det is not None:
            lasts = [c.last for c in det.columns if c.kind is not None and c.last is not None]
            if lasts:
                return max(lasts)
        return tl.utc_now().replace(tzinfo=None) if hasattr(tl, "utc_now") else None


    def set_zone(self, local):
        """Kept for saved state: True restores the last used offset, False is UTC."""
        if local and self.offset_var.get() in ("", "UTC"):
            st = self.app.tags.state_section(SECTION)
            last = st.get("last_offset")
            if not isinstance(last, int) or not last:
                import time as _t
                # this computer's offset (a computer on UTC: +01:00, to be changed)
                last = -(_t.altzone if _t.daylight and _t.localtime().tm_isdst
                         else _t.timezone) // 60 or 60
            self.offset_var.set(tl.zone_label(last))
        elif not local:
            self.offset_var.set("UTC")
        self._offset_changed()

    def _on_resize(self, e):
        """A narrow tab folds the date-column panel to its '▸ Date columns' link (and brings
        it back when there is room, unless it was folded by hand)."""
        if e.widget is not self:
            return
        if e.width < COLS_BREAKPOINT and self._cols_open:
            self.toggle_columns()
            self._cols_auto_hidden = True
        elif e.width >= COLS_BREAKPOINT and not self._cols_open and self._cols_auto_hidden:
            self.toggle_columns()
            self._cols_auto_hidden = False

    def _toggle_columns_by_hand(self):
        self._cols_auto_hidden = False
        self.toggle_columns()

    def toggle_columns(self):
        """Fold the date-column panel away (the events get the room) or bring it back."""
        if self._cols_open:
            self.pane.forget(self.left)
            self.cols_show.pack(anchor="w", before=self.chart.master)
        else:
            self.cols_show.pack_forget()
            self.pane.insert(0, self.left, weight=0)
        self._cols_open = not self._cols_open

    def _range_selected(self, start, end):
        """A drag across the density chart keeps only the events of that range in the
        grid (the build is not read again)."""
        src = self.grid.source
        if src is None or not hasattr(src, "set_time_range"):
            return
        src.set_time_range(start, end)
        self.grid.refresh()
        self._update_sources()

    def build_when_ready(self):
        """Build now, or as soon as the date columns are known (the command palette)."""
        if self.detection is not None and self._busy is None:
            self.start_build()
        elif self._busy == "detect":
            # a detect is running: its done() starts the build
            self._build_after_detect = True
        elif self._busy is None:
            self._build_after_detect = True
            self.start_detect(again=False)
        # else a build is already running: nothing to queue

    def _col_hover(self, iid):
        if iid in self._group_iids.values():
            uid = next(u for u, g in self._group_iids.items() if g == iid)
            m = self._member(uid)
            det = self.detection.of(m) if isinstance(self.detection, CaseDetection) else None
            if m is None or det is None:
                return None
            lines = [m.name, "%d date columns found in %d tables (%.1fs)" % (
                len(det.detected()), det.tables, det.seconds)]
            lines += det.notes[:12]
            if len(det.notes) > 12:
                lines.append("… and %d more notes (Details under the status line)"
                             % (len(det.notes) - 12))
            return "\n".join(lines)
        c = self._column(iid)
        if c is None:
            return None
        db = self._db_name(c)
        lines = ["%s%s.%s" % (db + " › " if db else "", c.table, c.column), c.reason]
        samples = self._column_samples(c)
        if samples:
            lines.append("samples:")
            lines.extend("  %s → %s" % (raw, when) for raw, when in samples)
        return "\n".join(lines)

    def _column_samples(self, c, n=3):
        """A few raw values of the column and how they read as dates (cached): so a
        detection can be checked at a glance before it is trusted."""
        key = (c.table, c.column, c.effective_kind)
        if key not in self._sample_cache:
            self._sample_cache[key] = self._read_samples(c, n)
        return self._sample_cache[key]

    def _read_samples(self, c, n):
        kind = c.effective_kind
        if kind is None or kind not in tl.KINDS:
            return []
        try:
            m = self._member_for_column(c)
            session = m.db.session if m is not None else self.db.session
            if session is None:
                return []
            vals = tl.sample_column(session, c.table, c.column)
        except Exception:
            return []
        # set values first: 0, -1 and empty text are "not set", never 1601 or 1970
        out, unset = [], []
        for v in vals:
            if _unset(v):
                unset.append(v)
                continue
            dt = tl.to_utc(v, kind)
            if dt is not None and len(out) < n:
                out.append((value_text(v, 30), tl.format_dt(
                    dt, self.timefmt_var.get(), self._time_custom)))
        if unset and len(out) < n:
            shown = ", ".join(sorted(set(value_text(v, 12) or "''" for v in unset))[:3])
            out.append((shown, "not set (%d of %d sampled)" % (len(unset), len(vals))))
        return out

    def _member_for_column(self, c):
        db = getattr(c, "database", None)
        if db:
            for m in self.members:
                if m.name == db:
                    return m
        return None

    # -- jobs ------------------------------------------------------------------------------------
    def _run(self, what, fn, done):
        """fn(cancel, progress) on the worker thread, then done(result, seconds) here."""
        flag = self._stop = [False]
        self._progress = (0, 0)
        self._busy = what
        gen = self._gen
        started = time.time()
        self.bar.configure(value=0, maximum=1)
        self.top.show(self.bar, True)
        self.top.show(self.stop_btn, True)

        def finished(result, error):
            if gen != self._gen:
                # the database was closed or changed meanwhile: drop the result,
                # but always leave the tab idle (a stuck busy state blocks everything)
                self._busy = None
                self.bar.configure(value=0)
                self.top.show(self.bar, False)
                self.top.show(self.stop_btn, False)
                return
            self._busy = None
            self.bar.configure(value=0)
            self.top.show(self.bar, False)
            self.top.show(self.stop_btn, False)
            if error is not None:
                self.status.configure(text="%s failed: %s" % (what.title(), error))
                return
            done(result, time.time() - started)
        self._runner.submit(what, lambda: fn(lambda: flag[0], self._set_progress), finished)
        if getattr(self, "_progress_after", None) is None:
            self._poll_progress()

    def _set_progress(self, done, total):
        self._progress = (done, total)

    def _poll_progress(self):
        self._progress_after = None
        if self._busy is None:
            return
        done, total = self._progress
        if total:
            self.bar.configure(maximum=total, value=done)
        self._progress_after = self.after(150, self._poll_progress)

    def destroy(self):
        aid = getattr(self, "_progress_after", None)
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass
        self._progress_after = None
        ttk.Frame.destroy(self)

    # -- detection -------------------------------------------------------------------------------
    def _overrides(self, member):
        tags = self.app.tags
        if len(self.members) > 1 and hasattr(tags, "member_section"):
            return tags.member_section(member, SECTION).get("overrides") or {}
        return tags.state_section(SECTION).get("overrides") or {}

    def start_detect(self, again=True):
        """Look for the date columns of the databases in scope (those the Overview already
        looked at are reused unless again: 'Look for date columns again')."""
        if not self.db.ok or self._busy is not None:
            return
        self._stale_scope = False
        members = self._scope() or [None]
        self.members = [m for m in members if m is not None]
        sessions = [(m.db.session if m is not None else self.db.session) for m in members]
        overrides = [self._overrides(m) for m in members]
        known = [None if again or m is None else getattr(m, "date_detection", None)
                 for m in members]
        if len(members) > 1:
            self.status.set("Looking for date columns in %d databases…" % len(members),
                            ["Databases: " + ", ".join(m.name for m in members)])
        else:
            self.status.set("Looking for date columns…")
        # the current case (a job that finishes after the databases changed is dropped)
        case_key = [id(s) for s in sessions]

        def work(cancel, progress):
            totals = [len(tl.timeline_tables(s)) for s in sessions]
            done_n = [0] * len(sessions)

            def one(i, _m):
                if known[i] is not None and getattr(known[i], "more_tables", 0) == 0:
                    known[i].apply_overrides(overrides[i])
                    done_n[i] = totals[i]
                    return known[i]

                def prog(d, _t):
                    done_n[i] = d
                    progress(sum(done_n), sum(totals))
                try:
                    return tl.detect(sessions[i], cancel=cancel, progress=prog,
                                     overrides=overrides[i])
                except Exception as e:  # noqa: BLE001 - one database must not stop the rest
                    det = tl.Detection()
                    det.notes.append("could not be read: %s" % e)
                    return det
            return self._each(members, one)

        def done(dets, seconds):
            if [id(m.db.session if m is not None else self.db.session)
                    for m in members] != case_key:
                self._build_after_detect = False  # stale detect: drop the queued build
                return
            for m, d in zip(members, dets):
                if m is not None and not d.cancelled:
                    m.date_detection = d     # the Overview shows what was found too
            if len(members) > 1:
                det = CaseDetection(zip(members, dets))
            else:
                det = dets[0]
            self.detection = det
            self._fill_columns()
            found = det.detected()
            text = "%d date columns in %d of %d tables (%.1fs)%s" % (
                len(found), len(set((c.database, c.table) for c in found)), det.tables,
                seconds, " - stopped" if det.cancelled else "")
            details = []
            if len(members) > 1:
                # every database looked in: those with dates, then one line for the rest
                with_dates = [(m, len(d.detected())) for m, d in zip(members, dets)
                              if d.detected()]
                none = [m.name for m, d in zip(members, dets) if not d.detected()]
                text += " · %d of %d databases" % (len(with_dates), len(members))
                for m, k in sorted(with_dates, key=lambda x: -x[1]):
                    details.append("%s: %d date column%s" % (m.name, k, "" if k == 1 else "s"))
                if none:
                    details.append("%d database%s: no date columns found (%s)" % (
                        len(none), "" if len(none) == 1 else "s", ", ".join(none)))
            if det.notes:
                text += " · %d tables could not be read" % len(det.notes)
                details.extend(det.notes)
            self.status.set(text, details)
            if self._build_after_detect:
                self._build_after_detect = False
                self.after_idle(self.start_build)
        self._run("detect", work, done)

    def _kind_text(self, c):
        k = c.effective_kind
        if c.override == tl.OFF:
            return "(%s) off" % tl.SHORT.get(c.kind, "?") if c.kind else "off"
        if k is None:
            return "?"
        return tl.SHORT[k] + (" *" if c.override in tl.KINDS else "")

    def _fill_columns(self):
        """The date columns: flat for one database; in a case, grouped under their database
        (a tri-state check per group, its count; databases without date columns share one
        line)."""
        tree = self.col_tree
        tree.delete(*tree.get_children())
        self._shown_iids = []
        self._group_iids = {}
        det = self.detection
        if det is None:
            return
        multi = self.multi() and isinstance(det, CaseDetection)
        tree.configure(displaycolumns=("on", "table", "column", "kind", "conf"))
        q = self.col_filter.get().strip().lower()
        groups = {}
        if multi:
            order = [m for m, _d in det.parts]
            for m in order:
                d = det.of(m)
                if d is None or not d.columns:
                    continue
                gid = tree.insert("", "end", open=True, tags=("dbgroup",),
                                  values=("", m.name, "", "", "", ""))
                groups[m.uid] = gid
                self._group_iids[m.uid] = gid
            empty = [m.name for m in order if det.of(m) is not None and not det.of(m).columns]
            if empty:
                eid = tree.insert("", "end", tags=("empty",), values=(
                    "", "", "%d database%s: no date columns (%s)" % (
                        len(empty), "" if len(empty) == 1 else "s", ", ".join(empty)),
                    "", "", ""))
                self._group_iids[None] = eid
        for i, c in enumerate(det.columns):
            db = self._db_name(c)
            if q and q not in c.table.lower() and q not in c.column.lower() and \
                    q not in db.lower():
                continue
            tags = ("override",) if c.override in tl.KINDS else ("off",) if not c.enabled else ()
            tree.insert(groups.get(c.database, ""), "end", iid=str(i), tags=tags, values=(
                CHECK if c.enabled else UNCHECK, db, c.table, c.column, self._kind_text(c),
                c.confidence or "-"))
            self._shown_iids.append(str(i))
        for uid, gid in groups.items():
            kids = tree.get_children(gid)
            if not kids:
                tree.delete(gid)
                del self._group_iids[uid]
        self._update_build_btn()
        self._refresh_groups()
        self.col_search.set_count(len(self._shown_iids), len(det.columns), "column", "columns",
                                  "table or column")
        self._fill_status_counts()

    def _refresh_groups(self):
        """Each database line: its tri-state check and 'name  (3 of 5)'."""
        det, tree = self.detection, self.col_tree
        if det is None:
            return
        for uid, gid in self._group_iids.items():
            if uid is None or not tree.exists(gid):
                continue
            cols = [c for c in det.columns if c.database == uid]
            on = sum(1 for c in cols if c.enabled)
            mark = CHECK if on == len(cols) else UNCHECK if not on else PARTIAL
            m = self._member(uid)
            tree.item(gid, values=(mark, "", "%s  (%d of %d)" % (m.name if m else "?", on,
                                                                 len(cols)), "", "", ""))

    def _column(self, iid):
        try:
            return self.detection.columns[int(iid)]
        except (AttributeError, IndexError, TypeError, ValueError):
            return None

    def _refresh_row(self, iid):
        c = self._column(iid)
        if c is None or not self.col_tree.exists(iid):
            return
        tags = ("override",) if c.override in tl.KINDS else ("off",) if not c.enabled else ()
        self.col_tree.item(iid, tags=tags, values=(
            CHECK if c.enabled else UNCHECK, self._db_name(c), c.table, c.column,
            self._kind_text(c), c.confidence or "-"))

    def _db_name(self, c):
        m = self._member(c.database) if c.database is not None else None
        return m.name if m is not None else ""

    def _on_col_click(self, e):
        if self.col_tree.identify_column(e.x) != "#1":
            return None
        iid = self.col_tree.identify_row(e.y)
        group = next((u for u, g in self._group_iids.items() if g == iid and u is not None),
                     None)
        if group is not None:
            self.toggle_group(group)
            return "break"
        if iid:
            self.toggle(iid)
            return "break"
        return None

    def _toggle_selected(self):
        for iid in self.col_tree.selection():
            self.toggle(iid)
        return "break"

    def toggle(self, iid):
        """Tick or untick a column (a column without a detected kind needs one chosen)."""
        c = self._column(iid)
        if c is None:
            return
        if c.enabled:
            self.set_override(c, tl.OFF)
        elif c.kind is not None:
            self.set_override(c, None)
        else:
            self.col_status.configure(text="%s.%s has no detected kind: right-click it and "
                                           "choose how to read it." % (c.table, c.column))

    def set_shown(self, on):
        """Tick or untick every column listed now (the table filter picks them)."""
        for iid in self._shown_iids:
            c = self._column(iid)
            if c is None:
                continue
            if on and not c.enabled and c.kind is not None:
                c.override = None
            elif not on and c.enabled:
                c.override = tl.OFF
        self._persist_overrides()
        self._fill_columns()
        self._update_build_btn()

    def _update_build_btn(self):
        """Enable Build timeline only when at least one date column is ticked (P1-17)."""
        det = getattr(self, "detection", None)
        n = sum(1 for c in det.columns if c.enabled) if det is not None else 0
        try:
            self.build_btn.configure(state="normal" if n else "disabled")
        except Exception:
            pass
        # tooltip explains why disabled
        try:
            self.build_btn._tip_text = ("Build the timeline from the ticked date columns"
                if n else "Tick at least one date column to build the timeline")
        except Exception:
            pass

    def set_override(self, column, kind):
        """Read a column as `kind` (a kind, tl.OFF to leave it out, None for the detected one)."""
        column.override = kind if kind in tl.KINDS or kind == tl.OFF else None
        if column.override == column.kind:
            column.override = None
        self._persist_overrides()
        for iid in self._shown_iids:
            if self._column(iid) is column:
                self._refresh_row(iid)
        self._refresh_groups()
        self._fill_status_counts()

    def _fill_status_counts(self):
        """'64 of 100 ticked' beside the heading: '100 columns in 12 databases'."""
        det = self.detection
        if det is None:
            return
        on = sum(1 for c in det.columns if c.enabled)
        dbs = len(set(c.database for c in det.columns)) if self.multi() else 1
        self.cols_head.configure(text="%d date column%s%s" % (
            len(det.columns), "" if len(det.columns) == 1 else "s",
            " in %d databases" % dbs if self.multi() else ""))
        self.col_status.configure(text="%d of %d ticked (%d detected, %d only named like "
                                       "dates) · right-click: read as another kind" % (
                                           on, len(det.columns), len(det.detected()),
                                           len(det.columns) - len(det.detected())))

    def toggle_group(self, uid):
        """Tick every column of a database (untick all when all are ticked)."""
        det = self.detection
        cols = [c for c in det.columns if c.database == uid] if det is not None else []
        if not cols:
            return
        on = not all(c.enabled for c in cols)
        for c in cols:
            if on and not c.enabled and c.kind is not None:
                c.override = None
            elif not on and c.enabled:
                c.override = tl.OFF
        self._persist_overrides()
        self._fill_columns()

    def _persist_overrides(self):
        """Keep the choices of each database with its own tags."""
        det = self.detection
        if det is None:
            return
        parts = det.parts if isinstance(det, CaseDetection) else [(None, det)]
        for member, d in parts:
            saved = self._overrides(member) if member is not None else \
                (self.app.tags.state_section(SECTION).get("overrides") or {})
            for c in d.columns:
                per = saved.setdefault(c.table, {})
                if c.override is None:
                    per.pop(c.column, None)
                else:
                    per[c.column] = c.override
                if not per:
                    saved.pop(c.table, None)
            if member is None:
                self._save_state(overrides=saved)
            else:
                st = self.app.tags.member_section(member, SECTION)
                st["overrides"] = saved
                self.app.tags.set_member_section(member, SECTION, st)

    def _show_reason(self):
        sel = self.col_tree.selection()
        c = self._column(sel[0]) if sel else None
        if c is not None:
            db = self._db_name(c)
            self.col_status.configure(text="%s%s.%s: %s%s" % (
                db + " › " if db else "", c.table, c.column, c.reason,
                "" if c.override is None else " | your choice: %s" % (
                    "left out" if c.override == tl.OFF else tl.LABELS[c.override])))

    def _on_col_menu(self, e):
        iid = self.col_tree.identify_row(e.y)
        if not iid:
            return
        if iid not in self.col_tree.selection():
            self.col_tree.selection_set(iid)
        menu = self.column_menu(iid)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def column_menu(self, iid):
        """Right-click menu of a listed column: include / leave out, read as a kind."""
        c = self._column(iid)
        m = tk.Menu(self, tearoff=0)
        if c is None:
            return m
        m.add_command(label="Leave out" if c.enabled else "Include",
                      state="normal" if c.enabled or c.kind is not None else "disabled",
                      command=lambda: self.set_override(c, tl.OFF if c.enabled else None))
        sub = tk.Menu(m, tearoff=0)
        var = tk.StringVar(master=sub, value=c.effective_kind or "")
        sub.var = var
        # one stored value read as every kind, beside each choice: the right reading is seen
        # before it is picked (a kind that gives no date is marked so)
        raw = self._first_raw(c)

        def label(k, text):
            if raw is None:
                return text
            dt = tl.to_utc(raw, k)
            return "%s   → %s" % (text, tl.format_dt(dt, self.timefmt_var.get(),
                                                         self._time_custom)
                                       if dt is not None else "not a date")
        if raw is not None:
            sub.add_command(label="Sample value: %s" % value_text(raw, 40), state="disabled")
            sub.add_separator()
        if c.kind is not None:
            sub.add_radiobutton(label=label(c.kind, "As detected (%s)" % tl.LABELS[c.kind]),
                                variable=var, value=c.kind,
                                command=lambda: self.set_override(c, None))
            sub.add_separator()
        for k in tl.KINDS:
            sub.add_radiobutton(label=label(k, tl.LABELS[k]), variable=var, value=k,
                                command=lambda k=k: self.set_override(c, k))
        m.add_cascade(label="Read as", menu=sub)
        m.add_separator()
        m.add_command(label="Browse table %s" % c.table,
                      command=lambda: self.browse_column(c))
        return m

    def _first_raw(self, c):
        """A stored value of the column that is not NULL, 0 or empty (cached), or None."""
        key = ("raw", c.table, c.column, getattr(c, "database", None))
        if key not in self._sample_cache:
            raw = None
            try:
                m = self._member_for_column(c)
                session = m.db.session if m is not None else self.db.session
                for v in tl.sample_column(session, c.table, c.column, rows=30) or ():
                    if v not in (None, 0, "", b"") and not isinstance(v, bytes):
                        raw = v
                        break
            except Exception:           # noqa: BLE001 - no sample: plain labels
                raw = None
            self._sample_cache[key] = raw
        return self._sample_cache[key]

    # -- building --------------------------------------------------------------------------------
    def _range(self):
        start = tl.parse_when(self.from_var.get())
        end = tl.parse_when(self.to_var.get(), end=True)
        if start is not None and end is not None and end < start:
            raise ValueError("'To' is before 'From'")
        return start, end

    def _cap(self):
        return dict(caps()).get(self.cap_var.get(), limits.get("timeline_column_events"))

    def _update_recovered_label(self):
        """The Options menu says how many recovered records there are, and offers the WAL
        row versions only when a database in scope has a WAL."""
        n = len(getattr(self.app._forensics, "_records", None) or ())
        try:
            self.opts_menu.entryconfigure(1, label="Include recovered records (%s)" % (
                format(n, ",") if n else "none yet: Forensics › Deleted Records"))
            self.opts_menu.entryconfigure(0, state="normal" if self._has_wal else "disabled",
                                          label="Include WAL row versions" + (
                                              "" if self._has_wal else " (no WAL in scope)"))
        except tk.TclError:
            pass

    def start_build(self):
        """Read the events of the ticked columns (on the worker thread)."""
        if getattr(self, "_stale_scope", False) and self._busy is None and self.db.ok:
            # the scope changed since: look in the databases it covers now, then build
            self._stale_scope = False
            self.detection = None
            self.build_when_ready()
            return
        if not self.db.ok or self.detection is None:
            return
        if self._busy is not None:
            if self._busy == "detect":
                # date columns are still being found: build as soon as they are known
                self._build_after_detect = True
                self.status.set("Looking for date columns\u2026 the timeline builds when they are known.")
            else:
                self.status.set("Still working\u2026 please wait for the current job to finish.")
            return
        try:
            start, end = self._range()
        except ValueError as e:
            self.status.configure(text=str(e))
            return
        columns = [c for c in self.detection.columns if c.enabled]
        if not columns:
            self.status.configure(text="Tick at least one date column.")
            return
        # P1-4: drop the old results/placeholder so the build state is unambiguous
        self.result = None
        self._show_events()
        self.status.set("Reading events from %d column%s\u2026" % (
            len(columns), "" if len(columns) == 1 else "s"))
        det = self.detection
        parts = det.parts if isinstance(det, CaseDetection) else [(None, det)]
        cap = self._cap()
        total_cap = limits.get("timeline_total_events")
        active = getattr(getattr(self.app, "case", None), "active", None)
        multi = len(parts) > 1
        jobs = []
        for member, d in parts:
            db = member.db if member is not None else self.db
            cols = [c for c in d.columns if c.enabled]
            use_wal = self.wal_var.get() and db.has_wal
            # the Forensics tab's recovered records are those of the active database
            records = list(getattr(self.app._forensics, "_records", None) or ()) \
                if self.rec_var.get() and (member is None or member is active) else []
            jobs.append((member, db.session, cols, dict(d.descriptions),
                         db.wal if use_wal else None, records))
        self._update_recovered_label()
        self.status.set("Reading events%s…" % (" of %d databases" % len(jobs) if multi else ""))
        sessions = [j[1] for j in jobs]

        def work(cancel, progress):
            totals = [len(j[2]) for j in jobs]
            done_n = [0] * len(jobs)

            def one(i, _m):
                member, session, cols, desc, wal, records = jobs[i]

                def prog(d, _t):
                    done_n[i] = d
                    progress(sum(done_n), sum(totals))

                def live(table, loc):
                    row = session.row(table, loc)
                    return row.values if row is not None else None
                if not cols:
                    return tl.EventSet()
                res = tl.build_events(session, cols, desc, start, end, cap,
                                      total_cap * 2, cancel, prog)
                if wal is not None and not res.cancelled:
                    try:
                        evs, notes = tl.wal_events(wal.recover_all_records(cancel=cancel), cols,
                                                   desc, start, end, live, cancel, cap)
                        res.events.extend(evs)
                        res.notes.extend(notes)
                    except tl.Cancelled:
                        res.cancelled = True
                if records and not res.cancelled:
                    evs, notes = tl.carved_events(records, cols, desc, start, end, cap)
                    res.events.extend(evs)
                    res.notes.extend(notes)
                if multi:
                    for e in res.events:
                        e.database = member.name
                    res.notes = ["%s: %s" % (member.name, n) for n in res.notes]
                return res
            got = self._each([j[0] for j in jobs], one)
            res = tl.EventSet()
            res.per_database = []
            for (member, _s, _c, _d, _w, _r), r in zip(jobs, got):
                res.events.extend(r.events)
                res.notes.extend(r.notes)
                res.cancelled = res.cancelled or r.cancelled
                res.per_database.append((member, len(r.events)))
            if multi or len(res.events) > total_cap or any(j[4] or j[5] for j in jobs):
                res.events = tl.finish(res.events, total_cap, res.notes)
            return res

        def done(res, seconds):
            if [j[1] for j in jobs] != sessions or any(
                    (m.db.session if m is not None else self.db.session) is not s
                    for (m, s, _c, _d, _w, _r) in jobs):
                self.status.set("The databases changed while reading events; the timeline was not built.")
                return
            self.result = res
            self._show_events()
            # P1-4: explicit end state, never an ambiguous 'Reading…'
            if not res.events and not res.cancelled:
                text = ("No events found in the ticked columns" +
                        (" (%.1fs)" % seconds))
            else:
                text = "%s events from %d columns in %.1fs%s" % (
                    format(len(res.events), ","), len(columns), seconds,
                    " - stopped" if res.cancelled else "")
            details = []
            if multi:
                # the databases with events, then one line for those without
                with_ev = sorted([(m, n) for m, n in res.per_database if n],
                                 key=lambda x: -x[1])
                none = [m.name for m, n in res.per_database if not n]
                text += " · %d of %d databases" % (len(with_ev), len(res.per_database))
                for m, n in with_ev:
                    details.append("%s: %s events" % (m.name, format(n, ",")))
                if none:
                    details.append("%d database%s: no dated rows in the ticked columns (%s)" % (
                        len(none), "" if len(none) == 1 else "s", ", ".join(none)))
            if res.notes:
                text += " · %d note%s" % (len(res.notes), "" if len(res.notes) == 1 else "s")
                details.extend(res.notes)       # every note, none left out
            self.status.set(text, details)
        self._run("build", work, done)

    def _offset(self):
        m = tl.parse_offset(self.offset_var.get())
        return m if m is not None else 0

    def _offset_changed(self):
        m = tl.parse_offset(self.offset_var.get())
        if m is None:
            self.status.set("Offset not understood: use UTC+05:30, UTC-08:00 ...")
            return
        self.offset_var.set(tl.zone_label(m) if m else "UTC")
        if m:
            self._save_state(offset=m, last_offset=m)
        else:
            self._save_state(offset=m)
        if self.result is not None:
            self._show_events()

    # -- time display ----------------------------------------------------------------------
    def _update_timefmt_button(self):
        label = dict((k, l) for k, l, _b, _a in tl.TIME_FORMATS).get(
            self.timefmt_var.get(), "ISO 8601 (24-hour)")
        # short button text: the preset name without its example
        short = {"ISO 8601 (24-hour)": "ISO", "12-hour": "12-hour",
                 "Excel style": "Excel", "US style": "US", "Custom\u2026": "Custom"}
        self.timefmt_btn.configure(text="Time: %s \u25be" % short.get(label, "ISO"))

    def _time_format_changed(self):
        """The Time menu choice: reformat the live source's timestamps in place (filters,
        sort and selection stay)."""
        fmt = self.timefmt_var.get()
        if fmt == "custom" and not self._time_custom:
            self._custom_time_format()
            return
        self._applied_timefmt = fmt
        self._save_state(time_format=fmt, time_custom=self._time_custom)
        self._update_timefmt_button()
        src = self.grid.source
        if src is not None:
            src.time_format, src.time_custom = fmt, self._time_custom
            self.grid.refresh()
        self._update_preview()

    def _custom_time_format(self):
        """Ask for a strftime format, with a live preview; cancel keeps the old one."""
        prev_custom = self._time_custom
        win = tk.Toplevel(self)
        win.title("Custom time format")
        win.transient(self.winfo_toplevel())
        ttk.Label(win, text="strftime format:").pack(anchor="w", padx=12, pady=(12, 2))
        var = tk.StringVar(value=self._time_custom or "%Y-%m-%d %H:%M:%S")
        entry = ttk.Entry(win, textvariable=var, width=40)
        entry.pack(fill="x", padx=12)
        from datetime import datetime
        sample = datetime(2026, 10, 2, 14, 30, 45, 123456)
        preview = ElideLabel(win, text="", style="Muted.TLabel")
        preview.pack(fill="x", padx=12, pady=6)

        def show(*_a):
            try:
                preview.configure(text="Preview: " + sample.strftime(var.get()))
            except (ValueError, TypeError):
                preview.configure(text="Preview: (not a valid format)")
        var.trace_add("write", show)
        show()
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        closed = [False]

        def ok(*_e):
            fmt = var.get().strip()
            if not fmt:
                return
            try:
                sample.strftime(fmt)
            except (ValueError, TypeError):
                preview.configure(text="Preview: (not a valid format)")
                return
            self._time_custom = fmt
            self.timefmt_var.set("custom")
            closed[0] = True
            win.destroy()
            self._time_format_changed()

        def cancel(*_e):
            closed[0] = True
            win.destroy()
        ttk.Button(btns, text="OK", style="Primary.TButton", command=ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=cancel).pack(side="right", padx=(0, 6))
        win.bind("<Return>", ok)
        win.bind("<Escape>", cancel)
        win.protocol("WM_DELETE_WINDOW", cancel)
        entry.focus_set()
        entry.selection_range(0, "end")
        self.wait_window(win)
        # cancel (or the window closed): back to the format in effect before
        if self.timefmt_var.get() == "custom" and not self._time_custom:
            self._time_custom = prev_custom
            self.timefmt_var.set(self._applied_timefmt)
            self._update_timefmt_button()

    # -- the inline preview ------------------------------------------------------------------
    def _on_grid_selection(self, _sel):
        """The grid's selection moved: refresh the preview shortly (debounced, so holding
        an arrow key glides through events instead of rebuilding per step)."""
        if self._preview_after is not None:
            try:
                self.after_cancel(self._preview_after)
            except tk.TclError:
                pass
        self._preview_after = self.after(150, self._update_preview)

    def _toggle_preview(self):
        self._preview_open = not self._preview_open
        self._save_state(preview_open=self._preview_open)
        self._layout_preview()
        if self._preview_open:
            self._update_preview()

    def _layout_preview(self):
        self.preview_toggle.configure(text="\u25be Preview" if self._preview_open
                                      else "\u25b8 Preview")
        if self._preview_open:
            self.preview_text.pack(fill="x", pady=(2, 0))
        else:
            self.preview_text.pack_forget()

    def _open_previewed(self):
        if self._preview_ev is not None:
            self.open_event(self._preview_ev)

    def browse_event(self, ev):
        """Open the table an event came from in Browse (its database made active)."""
        if ev is None or not ev.table:
            return
        self._browse(self.member_of_event(ev), ev.table)

    def browse_column(self, c):
        """Open the table of a listed date column in Browse."""
        if c is not None:
            self._browse(self._member_for_column(c), c.table)

    def _browse(self, member, table):
        app = self.app
        if member is None:
            member = getattr(getattr(app, "case", None), "active", None)
        if member is not None and hasattr(app, "browse_member_table"):
            app.browse_member_table(member, table)

    def _update_preview(self):
        """Show the selected event under the grid: its time (as displayed), source and the
        row behind it."""
        self._preview_after = None
        sel = self.grid.selected_rows()
        ev = self.event_at_row(sel[0]) if sel else None
        self._preview_ev = ev
        if not self._preview_open:
            return
        head, body = "", "Select an event to preview it here (double-click opens its row)."
        if ev is not None:
            when = format_event_time(ev.when, self.timefmt_var.get(), self._time_custom)
            if self._offset():
                when += "  /  " + format_event_time(
                    ev.when + timedelta(minutes=self._offset()),
                    self.timefmt_var.get(), self._time_custom)
            where = ".".join(p for p in (ev.table, ev.column) if p)
            head = "%s  \u00b7  %s%s  \u00b7  %s" % (
                when, where, " (%s)" % ev.database if ev.database else "",
                ev.source)
            raw = ev.raw
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8", "replace")
                except Exception:
                    raw = repr(raw)
            raw = str(raw)
            if len(raw) > 1000:
                raw = raw[:1000] + "\u2026"
            body = "\n".join(p for p in (
                "Row: %s" % ev.row if ev.row else "",
                "Description: %s" % ev.description if ev.description else "",
                "Raw: %s" % raw if raw else "") if p) or "(no details)"
        self.preview_head.configure(text=head)
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", body)
        self.preview_text.configure(state="disabled")

    def _show_events(self):
        res = self.result
        if res is None:
            self.grid.set_source(None)
            self.chart.set_data([])
            self.sources.configure(text="")
            return
        src = EventSource(res.events, self._offset(), self.db.encoding if self.db.ok else "utf-8",
                          databases=self.multi(), time_format=self.timefmt_var.get(),
                          time_custom=self._time_custom)
        self.grid.set_source(src)
        self.filter_var.set("")
        # the density chart (the bars in the databases' colours in a case)
        colours = dict((m.name, m.color) for m in self.members) if self.multi() else {}
        self.chart.set_data([e.when for e in res.events],
                            [e.database for e in res.events] if self.multi() else None, colours)
        self.chart.set_split(self.split_var.get())
        if self.multi() != self._split_shown:
            self._split_shown = self.multi()
            if self._split_shown:
                self.split_cb.pack(side="right", before=self.sources)
            else:
                self.split_cb.pack_forget()
        self._update_sources()

    def _update_sources(self):
        """One line under the chart: how many events, from where, of which sources, their
        span, and how many the chart's selection keeps."""
        res = self.result
        if res is None:
            self.sources.configure(text="")
            return
        by = OrderedDict()
        for e in res.events:
            k = e.source.split(" ")[0]
            by[k] = by.get(k, 0) + 1
        parts = ["%s events" % format(len(res.events), ",")]
        if self.multi():
            dbs = len(set(e.database for e in res.events))
            parts.append("from %d database%s" % (dbs, "" if dbs == 1 else "s"))
        parts.append(", ".join("%s %s" % (format(n, ","), k) for k, n in by.items()) or "none")
        if res.events:
            parts.append("%s – %s" % (tl.fmt_time(res.events[0].when)[:16],
                                      tl.fmt_time(res.events[-1].when)[:16]))
        src = self.grid.source
        rng = getattr(src, "time_range", None) if src is not None else None
        if rng is not None:
            n = sum(1 for e in res.events if rng[0] <= e.when <= rng[1])
            parts.append("%s in the selected range (drag again, or click the chart to clear)"
                         % format(n, ","))
        self.sources.configure(text=" · ".join(parts))

    def _row_style(self, _row, values, _flags):
        """In a case, each event's row is marked with its database's colour."""
        if not self.multi() or not values:
            return None, None
        src = self.grid.source
        try:
            i = src.columns().index("Database")
        except (AttributeError, ValueError):
            return None, None
        name = values[i] if i < len(values) else None
        for m in self.members:
            if m.name == name:
                return None, m.color
        return None, None

    # -- events: open, tag, export -----------------------------------------------------------------
    def event_at_row(self, row):
        d = self.grid.row_data(row)
        src = self.grid.source
        return src.event_at(d[0]) if d is not None and src is not None else None

    def _open_row(self, _row, values):
        src = self.grid.source
        ev = src.event_at(values) if src is not None else None
        if ev is not None:
            self.open_event(ev)

    def open_event(self, ev):
        """Show the row behind an event: the row window, the WAL record or the recovered
        record."""
        app = self.app
        member = self.member_of_event(ev)
        if ev.source == "DB":
            from dialogs import RowWin
            db = member.db if member is not None else app.db
            return RowWin.show(app, db, ev.table, ev.locator) if ev.locator else None
        if member is not None and hasattr(app, "activate_member"):
            app.activate_member(member)     # WAL and recovered records: that database's tabs
        if ev.source.startswith("WAL"):
            from wal_tab import open_wal_record
            return open_wal_record(app, ev.ref, ev.column, ev.raw)
        from forensics_tab import RecordWindow
        return RecordWindow(self, ev.ref, app)

    def entry_for(self, ev):
        """Tag entry of an event's row (a database row is read now), of its database."""
        member = self.member_of_event(ev)
        mark = getattr(self.app.tags, "mark", None) if member is not None else None
        if ev.source == "DB":
            s = member.db.session if member is not None else self.db.session
            row = s.row(ev.table, ev.locator) if s is not None and ev.locator else None
            if row is None:
                return None
            snap = row.locator.snapshot
            cols = list(snap[0]) if snap is not None else s.visible_columns(ev.table)
            e = entry_from_db_row(ev.table, row.locator, cols, list(row.values), row.flags)
        elif ev.source.startswith("WAL"):
            e = entry_from_wal_record(ev.ref)
        else:
            e = entry_from_record(ev.ref)
        return mark(e, member) if mark is not None else e

    def selected_events(self, row):
        lo, hi = self.grid.selected_rows() or (row, row)
        most = limits.get("timeline_tag_rows")
        if hi - lo + 1 > most:
            self.status.configure(text="Only the first %s of the %s selected events are used "
                                       "(limit timeline_tag_rows)." % (
                                           format(most, ","), format(hi - lo + 1, ",")))
            hi = lo + most - 1
        src = self.grid.source
        out = []
        for values, _flags in self.grid.fetch_rows(lo, hi):
            ev = src.event_at(values)
            if ev is not None:
                out.append(ev)
        return out

    def _grid_menu(self, menu, row, _col):
        evs = self.selected_events(row)
        menu.add_separator()
        one = evs[0] if len(evs) == 1 else None
        menu.add_command(label="Open row", state="normal" if one else "disabled",
                         command=lambda: self.open_event(one))
        tables = sorted(set((e.database or "", e.table) for e in evs if e.table))
        if len(tables) == 1:
            menu.add_command(label="Browse table %s" % tables[0][1],
                             command=lambda: self.browse_event(evs[0]))
        if one is not None and one.source == "DB" and one.locator is not None:
            member = self.member_of_event(one)

            def history():
                if member is not None and hasattr(self.app, "activate_member"):
                    self.app.activate_member(member)    # Forensics works on the active one
                self.app.show_row_history(one.table, one.locator)
            menu.add_command(label="Row history (every version)", command=history)
        seen, unique = set(), []
        for e in evs:
            key = (e.database, e.table, e.source, id(e.ref), e.locator)
            if key not in seen:
                seen.add(key)
                unique.append(e)
        self.app.tag_menu(menu, lambda: [self.entry_for(e) for e in unique],
                          label="Tag %s" % ("row" if len(unique) == 1 else
                                            "%d rows" % len(unique)))

    def shown_events(self):
        src = self.grid.source
        return src.view_events() if src is not None else []

    def export_info(self):
        """What a timeline export says about the events beyond the common provenance (the
        tool, versions and every evidence file with its SHA-256 come from engine.export):
        the date range, the columns read, the filters and every note of the build. Read on
        the Tk thread."""
        info = OrderedDict()
        info["range_utc"] = "%s .. %s" % (self.from_var.get().strip() or "start",
                                          self.to_var.get().strip() or "end")
        if self.detection is not None:
            info["columns"] = ", ".join("%s%s.%s (%s)" % (
                self._db_name(c) + " › " if self._db_name(c) else "", c.table, c.column,
                tl.SHORT[c.effective_kind]) for c in self.detection.columns if c.enabled)
        flt = self.grid.filter_texts()
        if flt or self.grid.source is not None and self.grid.source.filtered:
            info["filters"] = "; ".join("%s: %s" % kv for kv in sorted(flt.items())) or \
                "global filter"
        if self.result is not None and self.result.notes:
            info["notes"] = list(self.result.notes)         # every note, none left out
        offset = self._offset()
        if offset:
            info["local_offset"] = tl.offset_label(offset)
        return info

    def export_snapshot(self):
        """Everything an export needs, read on the Tk thread (the worker never reads Tk
        variables or the grid): the events shown, their columns, the extra provenance, the
        databases they come from, the scope and the filters in words."""
        evs = list(self.shown_events())
        offset = self._offset()
        info = self.export_info()
        members = [m for m in self.members if m is not None] or \
            [getattr(self.app.case, "active", None) or self.db]
        return SimpleNamespace(
            events=evs, offset=offset, fields=tl.export_fields(evs, offset), info=info,
            members=members, filters=info.get("filters", ""),
            scope="the %s shown (of %s built)" % (
                format(len(evs), ","), format(len(self.result.events), ",")
                if self.result is not None else "?"))

    def export(self, fmt, path):
        """Write the shown events to path now, on this thread, with the one export writer
        (engine.export: provenance, manifest); refused inside the evidence folder (ValueError).
        Returns the number written. The Export ▾ menu runs the same on a Job."""
        from engine import export as ex
        from jobs import evidence_records, export_protected
        snap = self.export_snapshot()
        protected = export_protected(self.app) or self.db.is_protected
        if protected is not None and protected(path):
            raise ValueError("refusing to write %s: it is inside the evidence folder" % path)
        info = ex.provenance(VERSION, evidence_records(snap.members), "Timeline", snap.scope,
                             snap.filters, snap.fields, extra=snap.info)
        rows = (tl.event_row(e, snap.fields, snap.offset) for e in snap.events)
        return ex.write_rows(path, fmt, snap.fields, rows, info, "hex",
                             protected=protected).rows

    def export_dialog(self, fmt):
        snap = self.export_snapshot()
        if not snap.events:
            messagebox.showinfo("Export", "Build the timeline first.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, initialfile="timeline." + fmt, defaultextension="." + fmt,
            filetypes=[(fmt.upper(), "*." + fmt)])
        if not path or not write_allowed(path):
            return
        from jobs import export_rows
        fields, offset = snap.fields, snap.offset

        def rows_fn():
            return (tl.event_row(e, fields, offset) for e in snap.events)
        export_rows(self.app, "Export the timeline", path, fmt, fields, rows_fn, "Timeline",
                    snap.members, scope=snap.scope, filters=snap.filters,
                    total=len(snap.events), extra=snap.info, unit="events")
