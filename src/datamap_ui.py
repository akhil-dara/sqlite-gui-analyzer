"""'Copy with related' and 'Export Database Map…' in the UI (engine.related_copy and
engine.datamap do the work).

DataMapUI           the App's entry point (App.datamap): the 'Copy with related' menu items and
                    buttons (Browse - the selected rows, or all the rows a filter keeps -, Search
                    results, the row detail, the Related window), offered only for rows with a key
                    (rowid or PRIMARY KEY) of a table with confident links; the windows below and
                    their worker threads for App._stop_workers; the limits (engine.limits, with
                    the settings' overrides).
CopyRelatedWindow   the rows and every row a confident link leads to: a preview of the first
                    rows as Markdown, JSON or SQL, the number of rows and links before anything
                    long runs; Copy (up to the limit clipboard_chars) and Export… (streamed to a
                    file, with progress and Stop; never into the evidence folder).
DatabaseMapWindow   the options of the Database Map (format, sample rows, weaker links), then
                    the export with progress and Stop; the result names the file.
LimitsWindow        every limit with its default and range; saved in the settings.

Nothing opens by itself; the work runs on worker threads (grid.Runner), each closing its SQL
connection when it ends.
"""

import collections
import itertools
import os
import time
import tkinter as tk
from tkinter import filedialog, ttk

from constants import C, VERSION
from tokens import FONT as F
from engine import datamap as dm
from engine import limits as lim
from engine import related_copy as rc
from engine.relations import ROWID, relation_map
from engine.tags import TagError, load_settings, save_settings
from grid import Runner
from jobs import manifest_text, write_export_manifest
from utils import write_allowed
from widgets import SearchBox, release_variables
from widgets import fit_geometry

POLL_MS = 80
FORMATS = (("markdown", "Markdown"), ("json", "JSON"), ("sql", "SQL"))
MAP_FORMATS = (("html", "HTML (one self-contained file)"), ("markdown", "Markdown"),
               ("json", "JSON"))
LABEL = "Copy with related"


def keyed(locators):
    """The locators a copy can start from: rowid and PRIMARY KEY ones."""
    return [l for l in locators if getattr(l, "kind", None) in ("rowid", "pk")]


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "bytes" else ("%.1f %s" % (n, unit))
        n /= 1024.0


class RowSource(object):
    """Where a copy starts: explicit rows (locators), or every row a Browse filter keeps
    (rows() -> an iterator of engine Rows, called on a worker thread; count: known or None)."""

    def __init__(self, table, locators=None, rows=None, count=None, what=""):
        self.table = table
        self.locators = keyed(locators) if locators is not None else None
        self._rows = rows
        self.count = len(self.locators) if self.locators is not None else count
        self.what = what or ("%s row%s %s" % (
            table, "" if self.count == 1 else "s",
            ", ".join(l.display() for l in (self.locators or [])[:3]) +
            (" and %d more" % (self.count - 3) if (self.count or 0) > 3 else "")))

    def iterate(self):
        return iter(self.locators) if self.locators is not None else self._rows()

    def head(self, n):
        it = self.iterate()
        try:
            return list(itertools.islice(it, n))
        finally:
            close = getattr(it, "close", None)
            if close is not None:
                close()


class DataMapUI(object):
    """Menus, buttons and windows of Copy with related and the Database Map."""

    def __init__(self, app):
        self.app = app
        self.windows = []
        self._ended = []            # runners of closed windows, until their thread ends
        self.warn_with_dialogs = True   # ui_smoke reads last_message instead of a dialog
        self.last_message = ""

    # -- limits --------------------------------------------------------------------------------
    def settings(self):
        tags = getattr(self.app, "tags", None)
        s = getattr(tags, "settings", None)
        return s if isinstance(s, dict) else load_settings()

    def limits(self):
        """The limits in force (engine.limits: the defaults with the settings' valid
        overrides, loaded with the settings; refused values are listed as Issues)."""
        return lim.current()

    def save_limits(self, values):
        """Keep the limits that differ from their defaults in the settings."""
        settings = self.settings()
        changed = dict((k, v) for k, v in values.items() if lim.DEFAULTS.get(k) != v)
        if changed:
            settings["limits"] = changed
        else:
            settings.pop("limits", None)
        ev = self.app.db.evidence
        save_settings(settings, refuse_in=ev.directory if ev is not None else None)
        lim.load(settings)

    # -- the databases (a case) ------------------------------------------------------------------
    def _rel(self):
        return getattr(self.app, "relations", None)

    def member(self, member=None):
        """The database a copy reads (a case member): the given one, else the active one."""
        if member is not None:
            return member
        rel = self._rel()
        return rel.active() if rel is not None else None

    def multi(self):
        rel = self._rel()
        return rel is not None and rel.multi()

    def export_members(self, member, whole_case):
        """The databases an export of `member` reads (for its manifest): every database of
        the case when it covers the case, else that one."""
        case = getattr(self.app, "case", None)
        if whole_case and case is not None and len(case) > 1:
            return list(case)
        return [member] if member is not None else []

    def case_spec(self, member):
        """The engine CaseSpec of the open databases with the rows in `member`, and the trusted
        links between them; None for a database alone."""
        rel = self._rel()
        if rel is None or not rel.multi():
            return None
        members = [m for m in rel.members() if m.db.session is not None]
        if member not in members:
            return None
        return rc.CaseSpec(member.uid, [rc.Database(m.uid, m.name, m.db.session,
                                                    rel.map_of(m)) for m in members],
                           rel.cross_links())

    def cross_state(self):
        """In a case: how far the links between databases are known, in plain words ('' when
        they are, or for a database alone)."""
        rel = self._rel()
        if rel is None or not rel.multi():
            return ""
        if rel.cross is None:
            return "The links between the databases are not known yet (the mapping has not " \
                   "finished): only links inside each database are followed."
        return ""

    def is_protected(self, path):
        """True inside the folder of any open database (the whole case)."""
        case = getattr(self.app, "case", None)
        if case is not None and hasattr(case, "is_protected"):
            return case.is_protected(path)
        return self.app.db.is_protected(path)

    # -- what is offered -----------------------------------------------------------------------
    def offers(self, table, locators, member=None):
        """True when Copy with related can start from these rows: rows with a key, of a table
        that has confident links (or whose links are not all checked yet)."""
        rel = self._rel()
        member = self.member(member)
        return bool(table) and rel is not None and member is not None and \
            member.db.session is not None and rel.supported(table, member) and \
            bool(keyed(locators)) and self.has_links(table, member)

    def has_links(self, table, member=None):
        """False only when every link of the table (inside its database, and in a case to
        another database) is checked and none is confident."""
        rel = self._rel()
        member = self.member(member)
        if rel is None or member is None or member.db.session is None or \
                not rel.supported(table, member):
            return False
        m = rel.map_of(member)
        if m is None:
            return False
        cols = list(member.db.session.visible_columns(table))
        for c in cols:
            if rel.multi() and rel.cross_targets(table, c, member):
                return True
        if m.key(table) is ROWID:
            cols.append(ROWID)
        for c in cols:
            try:
                known = m.known_confident(table, c)
            except KeyError:
                continue
            if known is None or known:
                return True
        return False

    def row_menu(self, menu, table, locators, member=None):
        """Add 'Copy with related' for rows of a table (of `member`, default the active
        database); returns the number of items added."""
        locs = keyed(locators)
        if not self.offers(table, locs, member):
            return 0
        menu.add_command(label=LABEL,
                         command=lambda: self.copy_with_related(table, locs, member))
        return 1

    def browse_menu(self, menu, table, row):
        """Browse grid menu: the selected rows when the clicked row is among them, and all the
        rows the filter keeps when a filter is on."""
        if not table or table.startswith("WAL: "):
            return 0
        grid = self.app._browse_grid
        sel = grid.selected_rows()
        src = self.app._browse_source
        big = sel and sel[0] <= row <= sel[1] and sel[1] - sel[0] >= rc.CHUNK
        if sel and sel[0] <= row <= sel[1] and not big:
            data = grid.fetch_rows(sel[0], sel[1])
        else:
            data = [grid.row_data(row)]
        locs = [d[0][0] for d in data if d and d[0]]
        if big and hasattr(src, "flt") and self.offers(table, locs):
            n = sel[1] - sel[0] + 1
            menu.add_command(label="%s: %s selected rows" % (LABEL, format(n, ",")),
                             command=lambda: self.copy_range(table, src, sel[0], sel[1]))
            return 1
        added = self.row_menu(menu, table, locs)
        if added and src is not None and getattr(src, "filtered", False) and \
                hasattr(src, "flt"):
            n = src.row_count()
            label = "%s: all %s filtered rows" % (LABEL, format(n, ",")) if n is not None \
                else "%s: all filtered rows" % LABEL
            menu.add_command(label=label, command=lambda: self.copy_filtered(table, src))
            added += 1
        return added

    def search_menu(self, menu, result, member=None):
        """Search results menu: a database row (not a WAL copy or a freed-page record)."""
        source = result.get("source") or "DB"
        if source not in ("DB", "Database"):
            return 0
        return self.row_menu(menu, result.get("table"), [result.get("locator")], member)

    # -- windows ---------------------------------------------------------------------------------
    def _where(self, member, table):
        """'table', or in a case 'wa.db › table'."""
        return member.label(table) if self.multi() else table

    def copy_with_related(self, table, locators, member=None):
        locs = keyed(locators)
        member = self.member(member)
        if not self.offers(table, locs, member):
            return None
        source = RowSource(table, locs)
        if self.multi():
            source.what = self._where(member, source.what)
        return self._open(CopyRelatedWindow(self, source, member))

    def copy_filtered(self, table, src):
        """Every row the Browse filter keeps now (in the grid's order), read while copying."""
        member = self.member()
        session = self.app.db.session
        if session is None or member is None or not self.has_links(table, member):
            return None
        flt, order, desc = src.flt, src.order, src.desc
        n = src.row_count()
        source = RowSource(table, rows=lambda: session.iter_rows(table, flt, order, desc),
                           count=n, what="all %s filtered rows of %s" % (
                               format(n, ",") if n is not None else "the",
                               self._where(member, table)))
        source.filter = flt
        return self._open(CopyRelatedWindow(self, source, member))

    def copy_range(self, table, src, lo, hi):
        """Rows lo..hi of the Browse grid (as sorted and filtered now), read while copying."""
        db = self.app.db
        member = self.member()
        if db.session is None or member is None or not self.has_links(table, member):
            return None
        flt, order, desc = src.flt, src.order, src.desc

        def rows():
            for start in range(lo, hi + 1, rc.CHUNK):
                _cols, got, _note = db.browse_window(table, start, min(rc.CHUNK, hi + 1 - start),
                                                     order, desc, flt)
                for r in got:
                    if r:
                        yield r[0]          # the row's locator
        n = hi - lo + 1
        return self._open(CopyRelatedWindow(self, RowSource(
            table, rows=rows, count=n, what="%s selected rows of %s" % (
                format(n, ","), self._where(member, table))), member))

    def _open(self, w):
        self.windows.append(w)
        return w

    def export_map(self):
        """Open the Database Map window (one at a time)."""
        if self.app.db.session is None:
            self.tell("Export Database Map", "No database loaded.")
            return None
        for w in self.windows:
            if isinstance(w, DatabaseMapWindow):
                w.lift()
                return w
        return self._open(DatabaseMapWindow(self))

    def map_window(self):
        return next((w for w in self.windows if isinstance(w, DatabaseMapWindow)), None)

    def limits_window(self, parent=None):
        for w in list(self.windows):
            if isinstance(w, LimitsWindow):
                try:
                    if w.winfo_exists():
                        w.lift()
                        return w
                except tk.TclError:
                    pass
                self.windows.remove(w)      # closed some other way: open a new one
        return self._open(LimitsWindow(self, parent))

    def forget(self, w):
        if w in self.windows:
            self.windows.remove(w)
        runner = getattr(w, "runner", None)
        if runner is not None:
            self._ended.append(runner)

    def stop(self):
        for w in self.windows:
            w.stop()

    def worker_threads(self):
        self._ended = [r for r in self._ended if r.threads()]
        out = []
        for r in [getattr(w, "runner", None) for w in self.windows] + self._ended:
            if r is not None:
                out.extend(r.threads())
        return out

    def forget_member(self, member):
        """A database leaves the case: its copies and maps close, and one of another database
        that may read it (a case copy or map) stops. Returns their threads still running."""
        out = []
        for w in list(self.windows):
            runner = getattr(w, "runner", None)
            if getattr(w, "member", None) is member:
                w.close()
            elif getattr(w, "case", None) is not None or getattr(w, "multi", False):
                w.stop()
            else:
                continue
            if runner is not None:
                out.extend(runner.threads())
        return out

    def close_all(self):
        """The database is being closed: its copies and maps go with it."""
        for w in list(self.windows):
            w.close()

    # -- writing -----------------------------------------------------------------------------------
    def allowed(self, path):
        """False for no path, or for a path inside the evidence folder (said in a dialog, or in
        last_message when dialogs are off)."""
        if not path:
            return False
        if self.is_protected(path):
            self.last_message = "Refused: %s is inside the evidence folder." % path
            if self.warn_with_dialogs:
                write_allowed(path)
            return False
        return True

    def tell(self, title, text, parent=None):
        self.last_message = text
        if self.warn_with_dialogs:
            from tkinter import messagebox
            messagebox.showinfo(title, text, parent=parent or self.app)


class _Window(tk.Toplevel):
    """One job at a time on a worker thread; progress events are drained on the Tk thread."""

    def __init__(self, manager, title, name):
        tk.Toplevel.__init__(self, manager.app)
        self.manager, self.app = manager, manager.app
        self.session = self.app.db.session
        self.title(title)
        self.configure(bg=C["bg"])
        self.transient(self.app)
        self.runner = Runner(self, name, release=self.app._release_worker_connection)
        self._stop = False
        self._closed = False
        self._events = collections.deque()
        self._poll_id = None
        self.protocol("WM_DELETE_WINDOW", self.close)

    def run(self, key, work, done):
        """work(cancel, emit) on the worker thread; done(result, error) on the Tk thread."""
        self._stop = False
        events = self._events

        def job():
            return work(lambda: self._stop, events.append)

        def finished(result, error):
            if self._closed:
                return
            self._drain()
            done(result, error)
        self.runner.submit(key, job, finished)
        self._schedule()

    def busy(self):
        return self.runner.busy()

    def stop(self):
        self._stop = True
        self.runner.cancel()
        th = self.runner.running_thread()
        if th is None:
            return
        case = getattr(self.app, "case", None)
        if case is not None and hasattr(case, "interrupt"):
            case.interrupt(th)          # a copy or map may read every database of the case
        elif self.app.db.session is not None:
            self.app.db.interrupt(th)

    def close(self):
        if self._closed:
            return
        self.stop()
        self._closed = True
        self.manager.forget(self)
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        self.destroy()

    def destroy(self):
        tk.Toplevel.destroy(self)
        # the window may be freed later by the worker thread that ran its last job: its Tk
        # variables are let go of here, on the Tk thread
        release_variables(self)

    def _schedule(self):
        if self._poll_id is None and not self._closed:
            self._poll_id = self.after(POLL_MS, self._poll)

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        self._drain()
        if self.runner.busy() or self._events:
            self._schedule()

    def _drain(self):
        while self._events:
            self.on_event(self._events.popleft())

    def on_event(self, event):
        pass


class CopyRelatedWindow(_Window):
    """Copy with related: options, the estimate, a preview, Copy and Export…"""

    def __init__(self, manager, source, member=None):
        self.source = source
        self.member = manager.member(member)
        _Window.__init__(self, manager, "%s: %s" % (LABEL, source.what), "copy-related")
        self.session = self.member.db.session       # the rows' database
        self.case = manager.case_spec(self.member)  # the other databases (a case), or None
        fit_geometry(self, 1000, 700)
        self.limits = manager.limits()
        self.bundle = None
        self._text = ""
        self._links = None          # links followed from the table (known after the preview)
        self.result = None          # the last export's ExportResult
        self._build()
        self.rebuild()

    @property
    def complete_preview(self):
        """True when the preview holds every start row (Copy needs no second pass)."""
        n = self.source.count
        return n is not None and n <= self.limits["related_preview_rows"]

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 2))
        ttk.Label(top, text=self.source.what, style="B.TLabel").pack(side="left")
        scope = ""
        if self.case is not None:
            scope = " Links to the other %d databases of the case are followed too (matched by " \
                    "value), and every row names its database." % (len(self.case.databases) - 1)
            scope += " " + self.manager.cross_state() if self.manager.cross_state() else ""
        ttk.Label(self, text="The rows and every row a confident link leads to (declared foreign "
                             "keys, links whose values were found), with the link each came "
                             "through, the schema of the tables, dates converted beside the raw "
                             "values and BLOBs described." + scope,
                  style="M.TLabel", wraplength=960, justify="left").pack(fill="x", padx=10)
        opts = ttk.Frame(self)
        opts.pack(fill="x", padx=10, pady=6)
        ttk.Label(opts, text="Format:").pack(side="left")
        self.fmt_var = tk.StringVar(value="markdown")
        for key, text in FORMATS:
            ttk.Radiobutton(opts, text=text, value=key, variable=self.fmt_var,
                            command=self.render).pack(side="left", padx=3)
        ttk.Label(opts, text="Links to follow:").pack(side="left", padx=(16, 2))
        self.hops_var = tk.IntVar(value=1)
        for n in range(1, min(2, self.limits["related_hops"]) + 1):
            ttk.Radiobutton(opts, text=str(n), value=n, variable=self.hops_var,
                            command=self.rebuild).pack(side="left", padx=3)
        ttk.Label(opts, text="Rows per link:").pack(side="left", padx=(16, 2))
        self.per_link_var = tk.StringVar(value=str(self.limits["related_rows_per_link"]))
        spin = ttk.Spinbox(opts, from_=1, to=lim.LIMITS["related_rows_per_link"][2], width=6,
                           textvariable=self.per_link_var, command=self.rebuild)
        spin.pack(side="left")
        spin.bind("<Return>", lambda e: self.rebuild())
        self.hex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="BLOB bytes as hex", variable=self.hex_var,
                        command=self.rebuild).pack(side="left", padx=(16, 2))
        self.estimate = ttk.Label(self, text="", anchor="w", wraplength=960, justify="left")
        self.estimate.pack(fill="x", padx=10)
        self.status = ttk.Label(self, text="", style="M.TLabel", anchor="w", wraplength=960,
                                justify="left")
        self.status.pack(fill="x", padx=10)
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1.0)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        # asks for a few lines only: it takes the room left, the buttons below keep theirs
        self.preview = tk.Text(box, wrap="none", font=F["mono"], bg=C["bg2"], height=6,
                               fg=C["text"], relief="flat", padx=6, pady=4)
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.preview.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=self.preview.xview)
        self.preview.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self.preview.pack(fill="both", expand=True)
        # read-only but selectable
        self.preview.bind("<Key>", lambda e: None if (e.state & 4 and e.keysym.lower() in
                                                       ("c", "a")) else "break")
        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=10, pady=8)
        ttk.Button(bot, text="Close", command=self.close).pack(side="right", padx=3)
        self.export_btn = ttk.Button(bot, text="Export…", command=self.export_dialog)
        self.export_btn.pack(side="right", padx=3)
        self.copy_btn = ttk.Button(bot, text="Copy", command=self.copy)
        self.copy_btn.pack(side="right", padx=3)
        ttk.Button(bot, text="Limits…", command=lambda: self.manager.limits_window(self)).pack(
            side="left", padx=3)
        self.stop_btn = ttk.Button(bot, text="Stop", command=self.stop_work)

    # -- options -------------------------------------------------------------------------------
    def options(self):
        """(hops, per_link, include_hex); ValueError says what is wrong."""
        try:
            per_link = int(str(self.per_link_var.get()).strip())
        except ValueError:
            raise ValueError("rows per link must be a whole number")
        hops = int(self.hops_var.get())
        rc.check_options(None, hops, per_link, self.limits)
        return hops, per_link, bool(self.hex_var.get())

    def _working(self, text, progress=False):
        self.status.configure(text=text)
        self.stop_btn.pack(side="left", padx=3)
        self.copy_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        if progress:
            self.bar.configure(value=0)
            self.bar.pack(fill="x", padx=10, before=self.status)

    def _idle(self):
        self.stop_btn.pack_forget()
        self.bar.pack_forget()
        self.copy_btn.configure(state="normal")
        self.export_btn.configure(state="normal")

    # -- the preview ---------------------------------------------------------------------------
    def rebuild(self):
        """Collect the preview again (the source or an option changed)."""
        if self.busy():
            return
        try:
            hops, per_link, include_hex = self.options()
        except ValueError as e:
            self.status.configure(text="Not changed: %s." % e)
            return
        session, source, case = self.session, self.source, self.case
        n_preview = self.limits["related_preview_rows"]
        limits = self.limits
        t0 = time.perf_counter()

        def work(cancel, _emit):
            if source.count is None:
                flt = getattr(source, "filter", None)
                source.count = session.count(source.table, flt)
            rows = source.head(n_preview)
            try:
                b = rc.related_bundle(session, relation_map(session), source.table, rows,
                                      hops=hops, per_link=per_link, include_hex=include_hex,
                                      cancel=cancel, total=source.count, limits=limits,
                                      case=case, tool_version=VERSION)
            except rc.Cancelled:
                return None
            return b, b.walker.link_count(source.table)

        def done(result, error):
            self._idle()
            if error is not None:
                self.status.configure(text="Failed: %s" % error)
                return
            if result is None:
                self.status.configure(text="Stopped: nothing collected.")
                return
            self.bundle, self._links = result
            self.render()
            self._show_estimate(time.perf_counter() - t0)
        self.bundle, self._text = None, ""
        self.preview.delete("1.0", "end")
        self._working("Checking the links and collecting the related rows…")
        self.run("preview", work, done)

    def _show_estimate(self, seconds):
        b, n = self.bundle, self.source.count
        hops, per_link = b.hops, b.limit
        shown = len(b.roots)
        text = "%s start row%s · %d link%s from %s · at most %d related rows per link and row " \
               "(limit related_rows_per_link)%s" % (
                   format(n, ",") if n is not None else "?", "" if n == 1 else "s",
                   self._links, "" if self._links == 1 else "s", self.source.table, per_link,
                   " · %d links deep" % hops if hops > 1 else "")
        if n is not None and n > shown:
            text += ". The preview shows the first %s (limit related_preview_rows); Export… " \
                    "writes all %s, one batch at a time." % (format(shown, ","), format(n, ","))
        self.estimate.configure(text=text)
        self.status.configure(text="Preview: %s rows (%d start, %d related through %d link%s) in "
                                   "%.1f s%s" % (
                                       format(b.rows, ","), shown, b.walker.related,
                                       b.walker.groups, "" if b.walker.groups == 1 else "s",
                                       seconds, "; " + "; ".join(b.notes[:3]) +
                                       (" …" if len(b.notes) > 3 else "") if b.notes else ""))

    def render(self):
        """Show the preview in the chosen format."""
        if self.bundle is None:
            return
        try:
            self._text = self.bundle.render(self.fmt_var.get())
        except Exception as e:          # noqa: BLE001 - shown, never a crash
            self._text = ""
            self.status.configure(text="Could not write the preview: %s" % e)
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", self._text)

    def text(self):
        return self._text

    def stop_work(self):
        self.stop()
        self.status.configure(text="Stopping…")

    # -- Copy ----------------------------------------------------------------------------------
    def copy(self):
        """Put the whole text on the clipboard (at most clipboard_chars characters); for a
        source larger than the preview, the text is made first (with progress and Stop)."""
        limit = self.limits["clipboard_chars"]
        too_big = "Too large for the clipboard: more than %s characters (limit clipboard_chars). " \
                  "Use Export… to write it to a file." % format(limit, ",")
        if self.busy() or self.bundle is None:
            return False
        if self.complete_preview:
            if len(self._text) > limit:
                self.status.configure(text=too_big)
                return False
            self._put(self._text)
            return True
        try:
            hops, per_link, include_hex = self.options()
        except ValueError as e:
            self.status.configure(text="Not copied: %s." % e)
            return False
        session, source, fmt, limits = self.session, self.source, self.fmt_var.get(), \
            self.limits
        case = self.case

        def work(cancel, emit):
            sink = rc.LimitedText(limit)
            try:
                res = rc.export_related(session, relation_map(session), source.table,
                                        source.iterate(), sink, fmt, hops, per_link, include_hex,
                                        cancel, lambda d, t: emit((d, t)), source.count,
                                        source.what, limits, case, VERSION)
            except rc.TooLarge:
                return "too big", None
            except rc.Cancelled:
                return "stopped", None
            return ("done" if res.complete else "stopped"), sink.getvalue()

        def done(result, error):
            self._idle()
            if error is not None:
                self.status.configure(text="Failed: %s" % error)
            elif result[0] == "too big":
                self.status.configure(text=too_big)
            elif result[0] == "stopped":
                self.status.configure(text="Stopped: nothing was copied.")
            else:
                self._put(result[1])
        self._working("Making the text for the clipboard…", progress=True)
        self.run("copy", work, done)
        return None

    def _put(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="Copied %s (%s characters) to the clipboard." % (
            dict(FORMATS)[self.fmt_var.get()], format(len(text), ",")))

    def on_event(self, event):
        done, total = event
        if total:
            self.bar.configure(value=min(1.0, float(done) / total))
        self.status.configure(text="%s of %s rows…" % (format(done, ","),
                                                     format(total, ",") if total else "?"))

    # -- Export --------------------------------------------------------------------------------
    def default_name(self):
        return dm.safe_name("%s - related" % self.source.what) + \
            rc.EXTENSIONS[self.fmt_var.get()]

    def export_dialog(self):
        if self.busy():
            return None
        ext = rc.EXTENSIONS[self.fmt_var.get()]
        path = filedialog.asksaveasfilename(parent=self, defaultextension=ext,
                                            initialfile=self.default_name(),
                                            filetypes=[(dict(FORMATS)[self.fmt_var.get()],
                                                        "*" + ext), ("All files", "*.*")])
        return self.export_to(path) if path else None

    def export_to(self, path):
        """Write every start row and its related rows to path, streamed on the worker thread;
        False when the path is refused or an option is wrong."""
        if self.busy():
            return False
        if not self.manager.allowed(path):
            if path:
                self.status.configure(text=self.manager.last_message)
            return False
        try:
            hops, per_link, include_hex = self.options()
        except ValueError as e:
            self.status.configure(text="Not exported: %s." % e)
            return False
        session, source, fmt, limits = self.session, self.source, self.fmt_var.get(), \
            self.limits
        guard, case = self.manager.is_protected, self.case
        members = self.manager.export_members(self.member, case is not None)
        what_text = "Copy with related: %s (%s)" % (source.what, fmt)
        options = {"links_deep": hops, "rows_per_link": per_link, "blob_hex": include_hex}

        def work(cancel, emit):
            try:
                result = rc.export_related_file(
                    session, relation_map(session), source.table, source.iterate(), path, fmt,
                    is_protected=guard, hops=hops, per_link=per_link, include_hex=include_hex,
                    cancel=cancel, progress=lambda d, t: emit((d, t)), total=source.count,
                    title=source.what, limits=limits, case=case, tool_version=VERSION)
            except rc.Cancelled:
                return None
            manifest = write_export_manifest(
                self.app, path, what_text, members, [path], result.complete,
                rows=result.starts + result.related, extra=dict(
                    options, start_rows=result.starts, related_rows=result.related))
            return result, manifest

        def done(result, error):
            self._idle()
            if error is not None:
                self.result = None
                self.status.configure(text="Failed: %s" % error)
                return
            if result is None:
                self.result = None
                self.status.configure(text="Stopped before the first row: %s holds only its "
                                           "heading." % path if os.path.exists(path) else
                                      "Stopped: nothing was written.")
                return
            result, (manifest, why) = result
            self.result = result
            size = os.path.getsize(path) if os.path.exists(path) else 0
            what = "Exported" if result.complete else "Stopped after"
            text = "%s %s start rows and %s related rows to %s (%s, %.1f s)%s" % (
                what, format(result.starts, ","), format(result.related, ","), path,
                _size(size), result.seconds, "; %d note%s in the file" % (
                    len(result.notes), "" if len(result.notes) == 1 else "s")
                if result.notes else "")
            self.status.configure(text=text)
            self.app.activity("export", what=what_text, path=path, format=fmt,
                              rows=result.starts + result.related, complete=result.complete,
                              manifest=manifest, manifest_error=why)
            self.manager.tell("Copy with related", "%s\n\n%s" % (
                text, manifest_text(manifest, why)), parent=self)
        self._working("Exporting to %s…" % path, progress=True)
        self.run("export", work, done)
        return True


class DatabaseMapWindow(_Window):
    """Export Database Map…: options, then the export with progress and Stop."""

    def __init__(self, manager):
        _Window.__init__(self, manager, "Export Database Map", "database-map")
        self.member = manager.member()
        self.multi = manager.multi()
        self.geometry("720x%d" % (520 if self.multi else 450))
        self.limits = manager.limits()
        self.result_path = None
        self.result = None
        self.scope_var = tk.StringVar(value="active")
        self._build()

    def _build(self):
        s = self.session
        pad = dict(padx=12)
        ttk.Label(self, text="%s: %s" % ("Active database" if self.multi else "Database",
                                         dm.database_name(s)), style="B.TLabel").pack(
            anchor="w", pady=(12, 0), **pad)
        ttk.Label(self, text=s.evidence.main, style="M.TLabel").pack(anchor="w", **pad)
        if self.multi:
            # a case of several databases: the active one, or all of them in one map
            rel = self.manager._rel()
            names = [m.name for m in rel.members() if m.db.session is not None]
            box = ttk.Frame(self)
            box.pack(fill="x", pady=(8, 0), **pad)
            ttk.Label(box, text="Map of:").pack(side="left")
            ttk.Radiobutton(box, text="the active database (%s)" % self.member.name,
                            value="active", variable=self.scope_var).pack(side="left", padx=4)
            ttk.Radiobutton(box, text="several databases:", value="case",
                            variable=self.scope_var).pack(side="left", padx=4)
            if hasattr(self.app, "scopes"):
                from scope import ScopePicker
                # which databases (the global scope unless the map has its own choice)
                self.scope_picker = ScopePicker(box, self.app, "datamap",
                                                on_change=lambda: self.scope_var.set("case"))
                self.scope_picker.pack(side="left")
            state = self.manager.cross_state()
            ttk.Label(self, text="Several databases: the sections of each database chosen, "
                                 "the links between them (matched by value) and one diagram "
                                 "with the databases' colours." + (" " + state if state else ""),
                      style="M.TLabel", wraplength=690, justify="left").pack(anchor="w", **pad)
        ttk.Label(self, text="One file with every table (columns, types, keys, row counts), the "
                             "relationships with their evidence, the date and BLOB columns, a "
                             "ready-to-run JOIN query per linked table, the diagram and the "
                             "evidence hashes. The database is only read; no row is copied "
                             "unless sample rows are asked for.",
                  wraplength=690, justify="left").pack(anchor="w", pady=(8, 6), **pad)
        opts = ttk.Frame(self)
        opts.pack(fill="x", pady=2, **pad)
        ttk.Label(opts, text="Format:").grid(row=0, column=0, sticky="w")
        self.fmt_var = tk.StringVar(value="html")
        for i, (key, text) in enumerate(MAP_FORMATS):
            ttk.Radiobutton(opts, text=text, value=key, variable=self.fmt_var).grid(
                row=0, column=1 + i, sticky="w", padx=4)
        self.samples_var = tk.BooleanVar(value=False)
        self.sample_n = tk.StringVar(value=str(self.limits["map_sample_rows"]))
        ttk.Checkbutton(opts, text="Include sample rows per table:", variable=self.samples_var,
                        command=self._samples_toggled).grid(row=1, column=0, columnspan=2,
                                                            sticky="w", pady=(8, 0))
        self.sample_spin = ttk.Spinbox(opts, from_=1, to=lim.LIMITS["map_sample_rows"][2],
                                       width=6, textvariable=self.sample_n, state="disabled")
        self.sample_spin.grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(opts, text="(values cut to %d characters)" % self.limits["map_sample_chars"],
                  style="M.TLabel").grid(row=1, column=3, sticky="w", pady=(8, 0))
        self.weaker_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Include weaker links (listed apart, not used in the "
                                   "queries)", variable=self.weaker_var).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.stage = ttk.Label(self, text="", anchor="w")
        self.stage.pack(fill="x", pady=(14, 2), **pad)
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1.0)
        self.bar.pack(fill="x", **pad)
        self.status = ttk.Label(self, text="", style="M.TLabel", anchor="w", wraplength=690,
                                justify="left")
        self.status.pack(fill="x", pady=(6, 0), **pad)
        bot = ttk.Frame(self)
        bot.pack(side="bottom", fill="x", padx=12, pady=10)
        ttk.Button(bot, text="Close", command=self.close).pack(side="right", padx=3)
        self.export_btn = ttk.Button(bot, text="Export…", command=self.export_dialog)
        self.export_btn.pack(side="right", padx=3)
        ttk.Button(bot, text="Limits…", command=lambda: self.manager.limits_window(self)).pack(
            side="left", padx=3)
        self.stop_btn = ttk.Button(bot, text="Stop", command=self.stop_work)
        self.copy_path_btn = ttk.Button(bot, text="Copy path", command=self.copy_path)

    def _samples_toggled(self):
        self.sample_spin.configure(state="normal" if self.samples_var.get() else "disabled")

    def _databases(self):
        """The case's databases as the engine maps them (every open one, with its colour)."""
        rel = self.manager._rel()
        return [dm.MapDatabase(m.uid, m.name, m.db.session, rel.map_of(m),
                               getattr(m, "color", ""))
                for m in self._scoped_members()]

    def _scoped_members(self):
        """The databases a map of several covers: those of the Database Map's scope."""
        rel = self.manager._rel()
        members = [m for m in rel.members() if m.db.session is not None]
        scopes = getattr(self.app, "scopes", None)
        if scopes is None:
            return members
        chosen = set(m.uid for m in scopes.members("datamap"))
        return [m for m in members if m.uid in chosen] or members

    def options(self):
        """The MapOptions chosen; ValueError says what is wrong."""
        n = 0
        if self.samples_var.get():
            try:
                n = int(str(self.sample_n.get()).strip())
            except ValueError:
                raise ValueError("sample rows must be a whole number")
            if n < 1:
                raise ValueError("sample rows must be at least 1")
        return dm.MapOptions(sample_rows=n, weaker=self.weaker_var.get(), limits=self.limits)

    def export_dialog(self):
        if self.busy():
            return None
        fmt = self.fmt_var.get()
        ext = dm.MAP_EXTENSIONS[fmt]
        name = dm.case_file_name(self._databases(), fmt) if self.scope_var.get() == "case" \
            else dm.map_file_name(dm.database_name(self.session), fmt)
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=ext, initialfile=name,
            filetypes=[(dict(MAP_FORMATS)[fmt], "*" + ext), ("All files", "*.*")])
        return self.export_to(path) if path else None

    def export_to(self, path):
        """Build the map and write it to path on the worker thread; False when refused."""
        if self.busy():
            return False
        if not self.manager.allowed(path):
            if path:
                self.status.configure(text=self.manager.last_message)
            return False
        try:
            options = self.options()
        except ValueError as e:
            self.status.configure(text="Not exported: %s." % e)
            return False
        session, fmt = self.session, self.fmt_var.get()
        guard = self.manager.is_protected
        t0 = time.perf_counter()
        whole = self.multi and self.scope_var.get() == "case"
        dbs = self._databases() if whole else None
        rel = self.manager._rel()
        scoped = self._scoped_members() if whole else []
        uids = set(m.uid for m in scoped)
        cross = [l for l in rel.cross_links(confident=False)
                 if l.src_db in uids and l.dst_db in uids] if whole else []
        note = (rel.cross.limits_text() if rel.cross is not None else
                "the links between the databases were not known yet when this map was made "
                "(the mapping had not finished)") if whole else ""
        members = scoped if whole else self.manager.export_members(self.member, False)
        every = whole and len(scoped) == len([m for m in rel.members()
                                              if m.db.session is not None])
        what_text = "Database Map (%s%s)" % (fmt, (", the whole case" if every else
                                                   ", %d databases of the case" % len(scoped))
                                             if whole else "")

        def work(cancel, emit):
            report = lambda stage, done, total: emit((stage, done, total))  # noqa: E731
            if whole:
                m = dm.case_map(dbs, cross, options, cancel, report, VERSION, note)
            else:
                m = dm.database_map(session, relation_map(session), options, cancel, report,
                                    tool_version=VERSION)
            if m is None:
                return None
            emit(("Writing the file", 0, 1))
            size = dm.write_map(path, fmt, m, guard)
            emit(("Writing the manifest", 0, 1))
            manifest = write_export_manifest(
                self.app, path, what_text, members, [path], True,
                rows=m.data["summary"].get("tables"), extra={
                    "sample_rows": getattr(options, "sample_rows", None),
                    "weaker_links": getattr(options, "weaker", None)})
            return m, size, manifest

        def done(result, error):
            self.stop_btn.pack_forget()
            self.export_btn.configure(state="normal")
            self.bar.configure(value=0)
            if error is not None:
                self.stage.configure(text="")
                self.status.configure(text="Failed: %s" % error)
                return
            if result is None:
                self.stage.configure(text="")
                self.status.configure(text="Stopped: no file was written.")
                return
            self.result, size, (manifest, why) = result
            self.result_path = path
            s = self.result.data["summary"]
            self.stage.configure(text="Done in %.1f s." % (time.perf_counter() - t0))
            if isinstance(self.result, dm.CaseMap):
                notes = [n for _db, m in self.result.maps for n in m.data["notes"]]
                text = "Saved %s (%s) - %d databases, %d tables, %d links, %d between " \
                       "databases (matched by value)." % (
                           path, _size(size), s["databases"], s["tables"], s["links"],
                           s["cross_links"])
            else:
                notes = self.result.data["notes"]
                text = "Saved %s (%s) - %d tables, %d links, %d date columns, %d BLOB columns, " \
                       "%d queries." % (path, _size(size), s["tables"], s["confident_links"],
                                        s["date_columns"], s["blob_columns"], s["queries"])
            if notes:
                text += " %d note%s in the map (limits reached, tables not read)." % (
                    len(notes), "" if len(notes) == 1 else "s")
            self.status.configure(text=text)
            self.copy_path_btn.pack(side="left", padx=3)
            self.app.activity("export", what=what_text, path=path, format=fmt,
                              manifest=manifest, manifest_error=why)
            self.manager.tell("Database Map", "%s\n\n%s" % (text, manifest_text(manifest, why)),
                              parent=self)
        self.result_path = None
        self.copy_path_btn.pack_forget()
        self.export_btn.configure(state="disabled")
        self.stop_btn.pack(side="left", padx=3)
        self.stage.configure(text="Starting…")
        self.status.configure(text="Writing %s" % path)
        self.run("map", work, done)
        return True

    def on_event(self, event):
        stage, done, total = event
        frac = float(done) / total if total else 0.0
        count = "" if stage.startswith("Waiting") or total <= 1 else " %s/%s" % (
            format(done, ","), format(total, ","))
        self.stage.configure(text="%s…%s" % (stage, count))
        self.bar.configure(value=max(0.0, min(1.0, frac)))

    def stop_work(self):
        self.stop()
        self.stage.configure(text="Stopping…")

    def copy_path(self):
        if self.result_path:
            self.clipboard_clear()
            self.clipboard_append(self.result_path)


class LimitsWindow(tk.Toplevel):
    """Every limit: what it limits, its default and its value; Save keeps the changed ones in
    the settings (checked first; nothing is saved while one is wrong)."""

    def __init__(self, manager, parent=None):
        tk.Toplevel.__init__(self, parent or manager.app)
        self.manager, self.app = manager, manager.app
        self.runner = None
        self.title("Limits")
        self.configure(bg=C["bg"])
        fit_geometry(self, 860, 640)            # never larger than the screen
        self.protocol("WM_DELETE_WINDOW", self.close)
        ttk.Label(self, text="Every named limit of the tool (opening and the evidence, cases, "
                             "search, SQL, forensics, the timeline, links, Copy with related, "
                             "the Database Map, the Browse grid, tags and long text values). "
                             "Whatever a limit leaves out is said where it happens, with the "
                             "limit's name. Saved limits apply to new searches, windows and "
                             "jobs; ram_overlay_bytes when a database is opened.",
                  wraplength=820, justify="left").pack(fill="x", padx=10, pady=(10, 4))
        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=10, pady=8, side="bottom")
        self.status = ttk.Label(self, text="", style="M.TLabel", wraplength=820, justify="left")
        self.status.pack(fill="x", padx=10, pady=4, side="bottom")
        frow = ttk.Frame(self)
        frow.pack(fill="x", padx=10, pady=(0, 4))
        self.search = SearchBox(frow, placeholder="Find a limit by name or what it limits…",
                                delay=0, find_button=False, primary=True, width=36,
                                on_change=lambda t: self._filter())
        self.search.pack(side="left", fill="x", expand=True)
        self.find_var = self.search.var
        # one list of every limit and one editor for the chosen one (a field per limit made
        # ~370 widgets: 300 ms to open and as long to close)
        edit = ttk.Frame(self)
        edit.pack(fill="x", padx=10, pady=(6, 0), side="bottom")
        self.edit_name = ttk.Label(edit, text="", font=F["mono"])
        self.edit_name.grid(row=0, column=0, sticky="w")
        self.entry = ttk.Entry(edit, width=16)
        self.entry.grid(row=0, column=1, sticky="w", padx=8)
        self.edit_status = ttk.Label(edit, text="", style="M.TLabel")
        self.edit_status.grid(row=0, column=2, sticky="w")
        self.edit_what = ttk.Label(edit, text="", style="M.TLabel", wraplength=820,
                                   justify="left")
        self.edit_what.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=10)
        self.tree = ttk.Treeview(outer, columns=("value", "status", "what"), show="tree headings",
                                 selectmode="browse")
        for col, text, width, stretch in (("#0", "Limit", 220, False),
                                          ("value", "Value", 110, False),
                                          ("status", "", 190, False),
                                          ("what", "What it limits", 320, True)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        sb = ttk.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("bad", foreground=C["red"])
        self.tree.tag_configure("changed", foreground=C["orange"])
        current = manager.limits()
        self.vars = {}
        self._rows = {}                 # name -> (tree item, what it limits)
        self._status = {}               # name -> (text, colour) beside its value
        for name in sorted(lim.LIMITS):
            default, lo, hi, what = lim.LIMITS[name]
            var = tk.StringVar(self, value=str(current[name]))
            text = "%s (%s to %s)" % (what, format(lo, ","), format(hi, ","))
            iid = self.tree.insert("", "end", text=name, values=(var.get(), "", text))
            self.vars[name] = var
            self._rows[name] = (iid, text)
            var.trace_add("write", lambda *_a, n=name: self._mark(n))
            self._mark(name)
        self._shown = None              # the limit in the editor
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._choose())
        self.tree.bind("<Double-1>", lambda e: self._edit_now())
        self.tree.bind("<Return>", lambda e: self._edit_now())
        self.entry.bind("<Return>", lambda e: (self.tree.focus_set(), "break")[1])
        first = self.tree.get_children()
        if first:
            self.tree.selection_set(first[0])
            self.tree.focus(first[0])
            self._choose()
        ttk.Button(bot, text="Close", command=self.close).pack(side="right", padx=3)
        ttk.Button(bot, text="Save", command=self.save).pack(side="right", padx=3)
        ttk.Button(bot, text="Defaults", command=self.defaults).pack(side="left", padx=3)

    def _choose(self, name=None):
        """Show a limit in the editor below the list (the one selected)."""
        if name is None:
            sel = self.tree.selection()
            if not sel:
                return
            name = self.tree.item(sel[0], "text")
        if name not in self.vars:
            return
        self._shown = name
        self.edit_name.configure(text=name)
        self.entry.configure(textvariable=self.vars[name])
        self.edit_what.configure(text=self._rows[name][1])
        self._show_status(name)

    def choose(self, name):
        """Select a limit in the list and show it in the editor."""
        iid = self._rows[name][0]
        if not self.tree.exists(iid):
            return
        if iid not in self.tree.get_children():
            self.find_var.set("")
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self._choose(name)

    def _edit_now(self):
        self._choose()
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        return "break"

    def status_text(self, name):
        """What the list says beside a limit's value ('default …', 'changed …', 'not valid …')."""
        return self._status.get(name, ("", ""))[0]

    def _show_status(self, name):
        text, colour = self._status.get(name, ("", C["text2"]))
        self.edit_status.configure(text=text, foreground=colour)
        try:
            self.entry.configure(foreground=C["red"] if colour == C["red"] else C["text"])
        except tk.TclError:
            pass

    def _check_one(self, name):
        """(value, problem) of one field: problem '' when it is a whole number in range."""
        text = str(self.vars[name].get()).strip().replace(",", "")
        lo, hi = lim.LIMITS[name][1:3]
        try:
            v = int(text)
        except ValueError:
            return None, "%s: %r is not a whole number" % (name, self.vars[name].get())
        if not lo <= v <= hi:
            return None, "%s: %s is not from %s to %s" % (name, format(v, ","),
                                                          format(lo, ","), format(hi, ","))
        return v, ""

    def _mark(self, name):
        """A value that is not the default says so ('changed'), a wrong one is red and says
        why beside it."""
        iid, what = self._rows[name]
        default = lim.LIMITS[name][0]
        v, problem = self._check_one(name)
        if problem:
            text, colour, tag = "not valid: %s" % problem.split(": ", 1)[1], C["red"], "bad"
        elif v != default:
            text, colour, tag = ("changed (default %s)" % format(default, ","), C["orange"],
                                 "changed")
        else:
            text, colour, tag = "default %s" % format(default, ","), C["text2"], ""
        self._status[name] = (text, colour)
        try:
            self.tree.item(iid, values=(self.vars[name].get(), text, what),
                           tags=(tag,) if tag else ())
        except tk.TclError:
            pass
        if getattr(self, "_shown", None) == name:
            self._show_status(name)

    def changed(self):
        """Names of the limits whose value differs from the default now."""
        return [n for n in self.vars if self._check_one(n)[0] not in (None, lim.DEFAULTS[n])]

    def _filter(self):
        """Show the limits whose name or description holds the Find text."""
        q = self.find_var.get().strip().lower()
        n = 0
        for name in sorted(self._rows):
            iid, what = self._rows[name]
            show = not q or q in name.lower() or q in what.lower()
            if show:
                self.tree.move(iid, "", n)          # reattached, in name order
                n += 1
            else:
                self.tree.detach(iid)
        shown = self.tree.get_children()
        if shown and not set(self.tree.selection()) & set(shown):
            self.tree.selection_set(shown[0])
            self.tree.focus(shown[0])
        self.search.set_count(n, len(self._rows), "limit", "limits")

    def values(self):
        """({name: value}, [problems])."""
        out, bad = {}, []
        for name in self.vars:
            v, problem = self._check_one(name)
            if problem:
                bad.append(problem)
            else:
                out[name] = v
        return out, bad

    def save(self):
        values, bad = self.values()
        if bad:
            self.status.configure(text="Not saved: " + "; ".join(bad) +
                                  " (marked in red in the list)")
            return False
        try:
            self.manager.save_limits(values)
        except (OSError, TagError) as e:
            self.status.configure(text="Not saved: %s" % e)
            return False
        self.status.configure(text="Saved. New windows use these limits.")
        return True

    def defaults(self):
        for name, var in self.vars.items():
            var.set(str(lim.DEFAULTS[name]))

    def busy(self):
        return False

    def stop(self):
        pass

    def close(self):
        self.manager.forget(self)
        # Tk destroys the window and its many fields in one go first (destroying them one by
        # one re-lays out the window after each: seconds)
        try:
            self.tk.call("destroy", self._w)
        except tk.TclError:
            pass
        self.destroy()
        release_variables(self)
