"""The Overview tab: the landing page of a case of several databases.

Summary cards (the overall date range, the biggest tables, the links between the databases,
the warnings) above one table of the case's databases: name, app or folder, size, tables,
rows, WAL state, hash state, the range of the dates found, the links in and out. The table is
sortable (click a heading) and filterable (the search box, the chips). The dates are found in
the background, database by database (the rows fill in as each is done; Stop stops it); what
is found is kept for the Timeline so it does not look again.
"""

import os
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import ttk

from engine import limits
from engine import timeline as tl
from navigator import group_key, member_rows, member_warnings, short_count
from parts import Card, EmptyState, StatusLine
from tokens import COLOR as K, FONT as F, XS, S, M
from utils import _int_count, fmtb
from widgets import SearchBox, ToolTip, wrap_to_width

COLUMNS = (("name", "Database", 170, "w"), ("app", "App / folder", 150, "w"),
           ("path", "Path", 260, "w"),
           ("size", "Size", 70, "e"), ("tables", "Tables", 74, "e"),
           ("triggers", "Triggers", 110, "w"),
           ("rows", "Rows", 80, "e"), ("wal", "WAL", 90, "w"), ("hash", "SHA-256", 470, "w"),
           ("dates", "Dates found (sampled)", 190, "w"), ("links_in", "Links in", 58, "e"),
           ("links_out", "Links out", 64, "e"))
POLL_MS = 150


def fmt_day(dt):
    return dt.strftime("%Y-%m-%d") if dt is not None else ""


CORE_TAIL = 0.1             # date columns left out at each end for the span most of them share
CORE_MIN = 5                # columns needed before a core span is said


def future_note(when):
    """' (future too)' when a date is after today (an expiry, or a wrong clock)."""
    import datetime as _dt
    try:
        today = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        last = when if isinstance(when, _dt.datetime) else _dt.datetime(when.year, when.month,
                                                                        when.day)
        return " (future too)" if last > today else ""
    except Exception:                   # noqa: BLE001 - no note then
        return ""


def core_range(firsts, lasts):
    """(first, last) shared by most date columns - their first dates without the earliest
    CORE_TAIL, their last dates without the latest - when the whole range is more than twice
    as long; else None (the whole range says it)."""
    if len(firsts) < CORE_MIN or len(lasts) < CORE_MIN:
        return None
    f, l = sorted(firsts), sorted(lasts)
    lo, hi = f[int(len(f) * CORE_TAIL)], l[len(l) - 1 - int(len(l) * CORE_TAIL)]
    whole = (l[-1] - f[0]).total_seconds()
    if hi <= lo or whole <= 0 or (hi - lo).total_seconds() * 2 >= whole:
        return None
    return lo, hi


def date_range_of(det):
    """(date columns, first, last) of a detection: the dates sampled from its columns."""
    cols = det.detected() if det is not None else []
    firsts = [c.first for c in cols if c.first is not None]
    lasts = [c.last for c in cols if c.last is not None]
    return len(cols), (min(firsts) if firsts else None), (max(lasts) if lasts else None)


class DateProfiler(object):
    """Looks for the date columns of databases one after the other on a worker thread
    (engine.timeline.detect, the same sampling the Timeline uses); results arrive through
    poll() on the Tk thread."""

    def __init__(self, app, on_result, on_done):
        self.app, self.on_result, self.on_done = app, on_result, on_done
        self._events = deque()
        self._thread = None
        self._stop = [False]
        self._after = None
        self.progress = (0, 0)
        self.running_name = ""

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def worker_threads(self):
        return [self._thread] if self.busy() else []

    def start(self, members):
        self.stop()
        todo = [m for m in members if m.db.ok and getattr(m, "date_detection", None) is None]
        if not todo:
            self.on_done(False)
            return
        flag = self._stop = [False]
        self.progress = (0, len(todo))
        most = limits.get("overview_date_tables")
        events = self._events
        release = getattr(self.app, "_release_worker_connection", None)
        jobs = [(m, m.db.session) for m in todo]

        def work():
            try:
                for i, (m, s) in enumerate(jobs):
                    if flag[0]:
                        break
                    events.append(("start", m, i))
                    try:
                        names = tl.timeline_tables(s)
                        det = tl.detect(s, tables=names[:most], cancel=lambda: flag[0])
                        det.more_tables = max(0, len(names) - most)
                    except Exception as e:      # noqa: BLE001 - reported on the row
                        det = tl.Detection()
                        det.notes.append("could not be read: %s" % e)
                        det.more_tables = 0
                    if flag[0] or det.cancelled:
                        break
                    events.append(("result", m, det))
            finally:
                if release is not None:
                    try:
                        release()
                    except Exception:           # noqa: BLE001
                        pass
                events.append(("end", None, flag[0]))
        self._thread = threading.Thread(target=work, name="overview-dates", daemon=True)
        self._thread.start()
        self._schedule()

    def _schedule(self):
        if self._after is None:
            try:
                self._after = self.app.after(POLL_MS, self.poll)
            except tk.TclError:
                self._after = None

    def poll(self):
        self._after = None
        while self._events:
            kind, m, x = self._events.popleft()
            if kind == "start":
                self.running_name = m.name
                self.progress = (x, self.progress[1])
            elif kind == "result":
                if m in list(self.app.case):
                    m.date_detection = x
                    self.on_result(m, x)
                self.progress = (self.progress[0] + 1, self.progress[1])
            elif kind == "end":
                self.running_name = ""
                self.on_done(bool(x))
                return
        if self.busy() or self._events:
            self._schedule()

    def stop(self):
        self._stop[0] = True
        th = self._thread
        if th is not None and th.is_alive():
            case = getattr(self.app, "case", None)
            if case is not None:
                case.interrupt(th)


class OverviewTab(ttk.Frame):
    """The Overview tab of App (shown for a case of two or more databases)."""

    def __init__(self, parent, app):
        ttk.Frame.__init__(self, parent)
        self.app = app
        self.sort_col, self.sort_desc = "name", False
        self._rows = {}             # iid -> member
        self.profiler = DateProfiler(app, self._date_result, self._dates_done)
        self._hash_after = None
        self._build()
        rel = getattr(app, "relations", None)
        if rel is not None:
            rel.listeners.append(lambda state: self._links_changed(state))

    # -- layout -------------------------------------------------------------------------------
    def _build(self):
        head = ttk.Frame(self)
        head.pack(fill="x", padx=M, pady=(M, S))
        self.title = ttk.Label(head, text="Overview", style="Title.TLabel")
        self.title.pack(side="left")
        self.subtitle = ttk.Label(head, text="", style="Muted.TLabel")
        self.subtitle.pack(side="left", padx=(S, 0))

        cards = self._cards_box = ttk.Frame(self)
        cards.pack(fill="x", padx=M)
        self.cards = {}
        self._card_cols = None
        for i, (key, title) in enumerate((("dates", "Date range (sampled)"),
                                          ("tables", "Biggest tables"),
                                          ("links", "Links between databases"),
                                          ("warn", "Warnings"))):
            card = Card(cards, title)
            value = wrap_to_width(ttk.Label(card.body, text="…", style="Metric.TLabel",
                                            justify="left"))
            value.pack(anchor="w", fill="x")
            sub = wrap_to_width(ttk.Label(card.body, text="", style="CardMuted.TLabel",
                                          justify="left"))
            sub.pack(anchor="w", fill="x")
            self.cards[key] = (card, value, sub)
        self._reflow_cards(4)
        cards.bind("<Configure>", lambda e: self._reflow_cards(4 if e.width >= 760 else 2),
                   add="+")
        dcard, dvalue, dsub = self.cards["dates"]
        for w in (dcard, dvalue, dsub):
            ToolTip(w, "The big dates: the span most date columns share (the tenth of the "
                       "columns starting earliest and the tenth ending latest left out, so "
                       "one birthday or expiry column does not stretch it). 'all': every date "
                       "found in the sampled rows. 'future too': some dates are after today "
                       "(expiries, or a wrong clock).")
        for key in ("links", "warn"):
            card, value, sub = self.cards[key]
            for w in (card, value, sub, card.title):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda e, k=key: self._card_click(k))

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=M, pady=(M, XS))
        self.search = SearchBox(bar, placeholder="Filter databases (name, app, folder)…",
                                delay=0, width=30, find_button=False, primary=True,
                                on_change=lambda t: self.fill())
        self.search.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", style="Small.TButton",
                                   command=self.stop_dates)
        self.dates_btn = ttk.Button(bar, text="Find dates", style="Small.TButton",
                                    command=self.start_dates)
        ToolTip(self.dates_btn, "Look for the date columns of every database (the first and "
                                "last rows of each table are sampled), as the Timeline does")
        self.export_btn = ttk.Button(bar, text="Export ▾", style="Small.TButton")
        self.export_btn.configure(command=lambda: self._export_menu(self.export_btn))
        self.export_btn.pack(side="right", padx=(XS, 0))
        ToolTip(self.export_btn, "Every database listed, with its path, size in bytes, tables, "
                                 "rows, WAL, whole SHA-256, dates, links, triggers and "
                                 "warnings: copy as text / CSV / JSON, or save a file")
        self.status = StatusLine(bar)
        self.status.pack(side="left", fill="x", expand=True, padx=(M, 0))

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=M, pady=(0, M))
        self.tree = ttk.Treeview(box, columns=[c[0] for c in COLUMNS], show="headings",
                                 selectmode="extended")
        for key, text, width, anchor in COLUMNS:
            self.tree.heading(key, text=text, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, minwidth=40, anchor=anchor,
                             stretch=key in ("name", "app", "path", "dates"))
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("active", font=F["body_bold"])
        self.tree.tag_configure("warn", foreground=K["warning"])
        self.tree.bind("<Double-1>", self._open_row)
        self.tree.bind("<Return>", self._open_row)
        self.tree.bind("<Button-3>", self._menu)
        self.tree.bind("<Control-c>", lambda e: self.copy_lines())
        from parts import HoverCard
        HoverCard(self.tree, self._hover)
        self.empty = EmptyState(self, "No databases", "Open a folder of databases to see "
                                "them here, side by side.", "Open folder…",
                                command=lambda: self.app._open_folder())

    def _reflow_cards(self, cols):
        """Four cards in a row, or two rows of two in a narrow window."""
        if cols == self._card_cols:
            return
        self._card_cols = cols
        box = self._cards_box
        for i in range(4):
            box.columnconfigure(i, weight=0, uniform="")
        for i, key in enumerate(("dates", "tables", "links", "warn")):
            card = self.cards[key][0]
            r, c = divmod(i, cols)
            card.grid(row=r, column=c, sticky="nsew", padx=(0 if c == 0 else S, 0),
                      pady=(0 if r == 0 else S, 0))
        for c in range(cols):
            box.columnconfigure(c, weight=1, uniform="cards")

    # -- data ---------------------------------------------------------------------------------
    def members(self):
        return list(getattr(self.app, "case", []) or [])

    def on_open(self):
        """The case changed: list it again and look for dates in the databases new to it."""
        self.fill()
        self.update_cards()
        if len(self.members()) > 1:
            self.start_dates()

    def reset(self):
        self.stop_dates()
        self.tree.delete(*self.tree.get_children())
        self._rows = {}

    def worker_threads(self):
        return self.profiler.worker_threads()

    def busy(self):
        return self.profiler.busy()

    def stop_dates(self):
        self.profiler.stop()

    def start_dates(self):
        self.profiler.start(self.members())
        self._update_progress()

    def _update_progress(self):
        if self.profiler.busy():
            done, total = self.profiler.progress
            self.status.set("Looking for dates… %d of %d databases%s" % (
                done, total, " (%s)" % self.profiler.running_name
                if self.profiler.running_name else ""))
            if not self.stop_btn.winfo_manager():
                self.stop_btn.pack(side="right")
            self.dates_btn.pack_forget()
            self.after(300, self._update_progress)
        else:
            self.stop_btn.pack_forget()

    def _date_result(self, m, det):
        """A database's dates are known: its line now, the cards and the navigator's 'Has
        dates' a moment later (once for a burst of results)."""
        self._refresh_member(m)
        if getattr(self, "_dates_after", None) is None:
            try:
                self._dates_after = self.after(300, self._dates_changed)
            except tk.TclError:
                self._dates_after = None

    def _dates_changed(self):
        self._dates_after = None
        self.update_cards()
        nav = getattr(self.app, "_navigator", None)
        if nav is not None:
            nav.set_dates(dict((x.uid, date_range_of(getattr(x, "date_detection", None))[0])
                               for x in self.members()
                               if getattr(x, "date_detection", None) is not None))

    def _dates_done(self, stopped):
        if getattr(self, "_dates_after", None) is not None:
            try:
                self.after_cancel(self._dates_after)
            except tk.TclError:
                pass
        self._dates_changed()
        missing =[m for m in self.members() if getattr(m, "date_detection", None) is None]
        self.stop_btn.pack_forget()
        if missing:
            if not self.dates_btn.winfo_manager():
                self.dates_btn.pack(side="right")
        else:
            self.dates_btn.pack_forget()
        self._status_line(stopped)

    def _status_line(self, stopped=False):
        ms = self.members()
        dated = [m for m in ms if date_range_of(getattr(m, "date_detection", None))[0]]
        looked = [m for m in ms if getattr(m, "date_detection", None) is not None]
        none = [m.name for m in looked if m not in dated]
        not_yet = [m.name for m in ms if m not in looked]
        summary = "Dates found in %d of %d databases" % (len(dated), len(ms))
        if stopped:
            summary += " (stopped)"
        details = []
        if none:
            details.append("%d database%s: no date columns found (%s)" % (
                len(none), "" if len(none) == 1 else "s", ", ".join(none)))
        if not_yet:
            details.append("%d database%s not looked at yet: %s" % (
                len(not_yet), "" if len(not_yet) == 1 else "s", ", ".join(not_yet)))
        for m in looked:
            det = m.date_detection
            if getattr(det, "more_tables", 0):
                details.append("%s: dates looked for in the first %s tables only (%s more; "
                               "limit overview_date_tables)" % (
                                   m.name, format(limits.get("overview_date_tables"), ","),
                                   format(det.more_tables, ",")))
        self.status.set(summary, details)

    def _row_values(self, m):
        rows, exact = member_rows(m)
        ev = m.db.evidence if m.db.ok else None
        sha = m.sha256()
        if sha:
            hash_text = "✓ " + sha            # whole: an examiner cites it as it is
        elif ev is not None and ev.hash_error:
            hash_text = "not hashed"
        elif ev is not None:
            hash_text = "hashing %d%%" % (100 * ev.hashed_bytes // max(ev.total_bytes, 1))
        else:
            hash_text = "-"
        mode = m.db.mode if m.db.ok else ""
        wal = {"ram-overlay": "merged", "main-only": "not in SQL", "native": "read natively"}.get(
            mode, "no WAL" if not (m.db.ok and m.db.has_wal) else "present")
        det = getattr(m, "date_detection", None)
        n, first, last = date_range_of(det)
        if det is None:
            dates = "…"
        elif not n:
            dates = "none found"
        else:
            dates = "%s – %s (%d col.)" % (fmt_day(first), fmt_day(last), n)
        li, lo = self._links(m)
        tables = len(m.db.tables()) if m.db.ok else 0
        unknown = any(not isinstance(v, int) and not (isinstance(v, str) and v.startswith("~"))
                      for v in m.counts.values())
        # '…' while some table is not even estimated yet: never '~1' for a database whose
        # tables have not been counted
        rows_text = "…" if unknown else ("" if exact else "~") + format(rows, ",")
        if m.db.ok and getattr(m.db.session, "encrypted", ""):
            # nothing of it can be read: never '0 tables, 0 rows' as if it were empty
            tables, rows_text, dates = "encrypted", "encrypted", "encrypted"
        return (m.name, group_key(m.path), m.path,
                fmtb(m.size) if m.size is not None else "?",
                tables, self._triggers(m)[0], rows_text, wal, hash_text, dates,
                li or "", lo or "")

    def _triggers(self, m):
        """(column text, hover line) on what this database's triggers do; ('', '') for none.
        Kept per database until the recovered schema changes (the list redraws often)."""
        if not m.db.ok or getattr(m.db, "session", None) is None:
            return "", ""
        session = m.db.session
        fx = session.__dict__.get("_forensics")
        key = id((getattr(fx, "_cache", None) or {}).get("dropped"))
        got = m.__dict__.get("_trigger_tally")
        if got is None or got[0] != key:
            try:
                from engine.triggers import for_session, tally, tally_text
                c = tally(for_session(session))
                col = "" if not c["total"] else (
                    "%d (%d delete rows)" % (c["total"], c["deletes"]) if c["deletes"]
                    else str(c["total"]))
                got = (key, col, tally_text(c))
            except Exception:           # noqa: BLE001 - nothing said then
                got = (key, "", "")
            m.__dict__["_trigger_tally"] = got
        return got[1], got[2]

    def _links(self, m):
        rel = getattr(self.app, "relations", None)
        if rel is None:
            return 0, 0
        try:
            links = rel.cross_links()
        except Exception:               # noqa: BLE001
            return 0, 0
        return (sum(1 for l in links if l.dst_db == m.uid),
                sum(1 for l in links if l.src_db == m.uid))

    def _sort_key(self, m):
        k = self.sort_col
        if k == "size":
            return (m.size or 0,)
        if k == "tables":
            return (len(m.db.tables()) if m.db.ok else 0,)
        if k == "rows":
            return (member_rows(m)[0],)
        if k == "app":
            return (group_key(m.path).lower(), m.name.lower())
        if k == "path":
            return (m.path.lower(),)
        if k == "dates":
            _n, first, _l = date_range_of(getattr(m, "date_detection", None))
            return (first is None, first.isoformat() if first is not None else "")
        if k in ("links_in", "links_out"):
            li, lo = self._links(m)
            return (li if k == "links_in" else lo,)
        if k == "triggers":
            text = self._triggers(m)[0]             # '357 (141 delete rows)'
            nums = [int(x) for x in text.replace("·", " ").split() if x.isdigit()] + [0, 0]
            return (nums[1], nums[0])               # the ones deleting rows first
        if k == "wal":
            return (m.db.mode if m.db.ok else "",)
        if k == "hash":
            return (bool(m.sha256()),)
        return (m.name.lower(),)

    def sort_by(self, key):
        if self.sort_col == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = key, key in ("size", "rows", "tables", "triggers",
                                                         "links_in", "links_out")
        self.fill()

    def shown_members(self):
        q = self.search.get().lower().split()
        out = []
        for m in self.members():
            text = ("%s %s %s" % (m.name, group_key(m.path), m.path)).lower()
            if all(w in text for w in q):
                out.append(m)
        out.sort(key=self._sort_key, reverse=self.sort_desc)
        return out

    def fill(self):
        tree = self.tree
        tree.delete(*tree.get_children())
        self._rows = {}
        ms = self.members()
        if not ms:
            self.empty.place(relx=0, rely=0, relwidth=1, relheight=1)
            return
        self.empty.place_forget()
        active = getattr(self.app.case, "active", None)
        for key, text, _w, _a in COLUMNS:
            arrow = (" ▾" if self.sort_desc else " ▴") if key == self.sort_col else ""
            tree.heading(key, text=text + arrow)
        shown = self.shown_members()
        for m in shown:
            tags = (("active",) if m is active else ()) + (("warn",) if member_warnings(m)
                                                           else ())
            iid = tree.insert("", "end", values=self._row_values(m), tags=tags)
            self._rows[iid] = m
        total = sum((m.size or 0) for m in ms)
        tables = sum(len(m.db.tables()) for m in ms if m.db.ok)
        self.subtitle.configure(text="%d databases · %s · %s tables%s" % (
            len(ms), fmtb(total), format(tables, ","),
            "" if len(shown) == len(ms) else " · %d shown" % len(shown)))
        if self.search.get():
            self.search.set_count(len(shown), len(ms), "database", "databases")
        if not self.profiler.busy():
            self._status_line()
        self._schedule_hash_poll()

    def _refresh_member(self, m):
        for iid, x in self._rows.items():
            if x is m and self.tree.exists(iid):
                self.tree.item(iid, values=self._row_values(m))

    def refresh_counts(self):
        for iid, m in self._rows.items():
            if self.tree.exists(iid):
                self.tree.item(iid, values=self._row_values(m))
        self.update_cards()

    def _schedule_hash_poll(self):
        if self._hash_after is None and any(m.db.ok and not m.sha256() and
                                            not m.db.evidence.hash_error
                                            for m in self.members()):
            try:
                self._hash_after = self.after(1500, self._hash_poll)
            except tk.TclError:
                self._hash_after = None

    def _hash_poll(self):
        self._hash_after = None
        if self.winfo_ismapped():
            self.refresh_counts()
        self._schedule_hash_poll()

    def _links_changed(self, _state):
        try:
            if self.winfo_exists():
                self.refresh_counts()
        except tk.TclError:
            pass

    # -- cards ---------------------------------------------------------------------------------
    def update_cards(self):
        ms = self.members()
        dets = [(m, getattr(m, "date_detection", None)) for m in ms]
        firsts, lasts, cols = [], [], 0
        for m, d in dets:
            n, f, l = date_range_of(d)
            cols += n
            if f is not None:
                firsts.append(f)
            if l is not None:
                lasts.append(l)
        _c, value, sub = self.cards["dates"]
        looked = sum(1 for _m, d in dets if d is not None)
        if firsts:
            lo, hi = min(firsts), max(lasts)
            per_col = [c for _m, d in dets if d is not None for c in d.detected()]
            core = core_range([c.first for c in per_col if c.first is not None],
                              [c.last for c in per_col if c.last is not None])
            where = "%d date columns · %d of %d databases" % (
                cols, sum(1 for _m, d in dets if date_range_of(d)[0]), looked)
            if core is not None:
                # a birthday in 1992 or an expiry in 2036 would make the card say nothing
                # about when the phone was used: the span most columns share, the full range
                # said under it
                value.configure(text="%s – %s" % (fmt_day(core[0]), fmt_day(core[1])))
                sub.configure(text="most of %d columns · all %s – %s%s" % (
                    cols, fmt_day(lo), fmt_day(hi), future_note(hi)))
            else:
                value.configure(text="%s – %s" % (fmt_day(lo), fmt_day(hi)))
                sub.configure(text=where)
        else:
            value.configure(text="…" if looked < len(ms) else "none")
            sub.configure(text="looking for dates…" if looked < len(ms) else
                          "no date columns found")
        big, pending = [], False
        for m in ms:
            for t, c in m.counts.items():
                if isinstance(c, int):
                    big.append((c, m.name, t, True))
                elif isinstance(c, str) and c.startswith("~"):
                    big.append((_int_count(c), m.name, t, False))  # an estimate, said so
                else:
                    pending = True              # not counted yet
        big.sort(key=lambda x: (-x[0], x[1], x[2]))
        _c, value, sub = self.cards["tables"]
        if big:
            n, d, t, exact = big[0]
            value.configure(text=("" if exact else "~") + short_count(n) + " rows")
            sub.configure(text="%s › %s%s" % (d, t, "" if exact else " (estimate)")
                          + (" · still counting" if pending else ""))
            tip = getattr(self, "_big_tip", None)
            if tip is None:
                tip = self._big_tip = ToolTip(sub, "")
            tip.text = "The biggest tables:\n" + "\n".join(
                "%s › %s  %s%s rows" % (d, t, "" if ex else "~", format(n, ","))
                for n, d, t, ex in big[:10])
        else:
            value.configure(text="…")
            sub.configure(text="counting rows…")
        rel = getattr(self.app, "relations", None)
        _c, value, sub = self.cards["links"]
        state = rel.state[0] if rel is not None else "idle"
        links = rel.cross_links() if rel is not None else []
        if state == "mapping":
            value.configure(text="…")
            done, total = (rel.state[1], rel.state[2]) if len(rel.state) >= 3 else (0, 0)
            if total and total - done <= 1:     # the last table can be the largest
                sub.configure(text="checking values: the last of %s tables" % format(total, ","))
            elif total:                 # half a minute of 'checking' looked stuck: say where
                sub.configure(text="checking values: %d%% (%s of %s tables)" % (
                    100 * done // max(1, total), format(done, ","), format(total, ",")))
            else:
                sub.configure(text="checking values (runs in the background)")
        else:
            value.configure(text=str(len(links)))
            pairs = set((l.src_db, l.dst_db) for l in links)
            text = "matched by value · %d pairs of databases" % len(pairs) \
                if links else "none found (matched by value)"
            res = getattr(getattr(rel, "manager", None), "cross", None)
            same = getattr(res, "same_tables", None) or {}
            if same:
                # copies of one app's tables (ten profiles' meta.key) are not links
                text += " · %d same table%s across databases, listed apart" % (
                    len(same), "" if len(same) == 1 else "s")
            sub.configure(text=text)
        _c, value, sub = self.cards["warn"]
        warned = [(m, member_warnings(m)) for m in ms]
        warned = [(m, w) for m, w in warned if w]
        value.configure(text=str(sum(len(w) for _m, w in warned)))
        sub.configure(text=", ".join(m.name for m, _w in warned[:2]) +
                      (" +%d more" % (len(warned) - 2) if len(warned) > 2 else "")
                      if warned else "none")

    def _card_click(self, key):
        if key == "links":
            tab = getattr(self.app, "_relations_tab", None)
            if tab is not None:
                self.app._nb.select(tab)
        elif key == "warn":
            self.app._show_status_detail()

    # -- rows ----------------------------------------------------------------------------------
    def _open_row(self, e=None):
        iid = self.tree.focus() if e is None or not hasattr(e, "y") else \
            (self.tree.identify_row(e.y) or self.tree.focus())
        m = self._rows.get(iid)
        if m is not None:
            self.app.browse_member_table(m, None)
        return "break"

    def _menu(self, e):
        iid = self.tree.identify_row(e.y)
        m = self._rows.get(iid)
        nav = getattr(self.app, "_navigator", None)
        if m is None or nav is None:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        line = nav.select_member(m)
        menu = nav.build_menu(line) if line is not None else None
        if menu is None:
            menu = tk.Menu(self, tearoff=0)
        else:
            menu.insert_separator(0)
        # the database's facts, whole (the column shows the hash's first digits only)
        sha = m.sha256() or ""
        menu.insert_command(0, label="Copy SHA-256" if sha else "SHA-256 not computed yet",
                            command=lambda: self._copy(sha, "SHA-256"),
                            state="normal" if sha else "disabled")
        n = len(self.tree.selection())
        menu.insert_command(1, label="Copy this line (every field)" if n <= 1 else
                            "Copy the %d selected lines" % n, accelerator="Ctrl+C",
                            command=self.copy_lines)
        menu.insert_command(2, label="Copy the whole table (every database)",
                            command=lambda: self.copy_lines(every=True))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    # -- the facts of every database, copyable and exportable ---------------------------------
    FIELDS = ("database", "path", "app_folder", "size_bytes", "tables", "rows", "rows_exact",
              "wal", "sha256", "open_mode", "dates_found", "date_columns", "date_first",
              "date_last", "links_in", "links_out", "triggers", "warnings")

    def facts(self, m):
        """One database's facts as a dict (the row's numbers whole, the hash whole)."""
        rows, exact = member_rows(m)
        det = getattr(m, "date_detection", None)
        n, first, last = date_range_of(det)
        li, lo = self._links(m)
        mode = m.db.mode if m.db.ok else ""
        wal = {"ram-overlay": "merged", "main-only": "not in SQL", "native": "read natively"}.get(
            mode, "no WAL" if not (m.db.ok and m.db.has_wal) else "present")
        encrypted = bool(m.db.ok and getattr(m.db.session, "encrypted", ""))
        return {"database": m.name, "path": m.path, "app_folder": group_key(m.path),
                "size_bytes": m.size,
                "tables": "encrypted" if encrypted else (len(m.db.tables()) if m.db.ok else 0),
                "rows": "encrypted" if encrypted else rows, "rows_exact": bool(exact), "wal": wal,
                "sha256": m.sha256() or "", "open_mode": mode,
                "dates_found": "not looked yet" if det is None else ("yes" if n else "none"),
                "date_columns": n, "date_first": fmt_day(first), "date_last": fmt_day(last),
                "links_in": li or 0, "links_out": lo or 0,
                "triggers": self._triggers(m)[1],
                "warnings": "; ".join(b.text for b in member_warnings(m))}

    def facts_rows(self, members=None):
        """[dict] for the lines shown (members None), in the list's order."""
        if members is None:
            members = [self._rows[i] for i in self.tree.get_children() if i in self._rows]
        return [self.facts(m) for m in members]

    def facts_text(self, rows, fmt="text"):
        """The facts as CSV, JSON or text (one 'field: value' block per database)."""
        import csv
        import io
        import json
        if fmt == "json":
            return json.dumps(rows, indent=2, ensure_ascii=False, default=str)
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(self.FIELDS)
            for r in rows:
                w.writerow(["" if r.get(f) is None else r.get(f) for f in self.FIELDS])
            return buf.getvalue()
        blocks = []
        for r in rows:
            blocks.append("\n".join("%s: %s" % (f, "" if r.get(f) is None else r.get(f))
                                    for f in self.FIELDS))
        return "\n\n".join(blocks)

    def copy_lines(self, every=False, fmt="text"):
        """Copy the selected databases' facts (every=True: all listed) to the clipboard."""
        if every or not self.tree.selection():
            members = None
        else:
            members = [self._rows[i] for i in self.tree.selection() if i in self._rows]
        rows = self.facts_rows(members)
        if not rows:
            return "break"
        self._copy(self.facts_text(rows, fmt), "%d database%s" % (
            len(rows), "" if len(rows) == 1 else "s"))
        return "break"

    def export_facts(self, fmt="csv"):
        """Write the facts of every listed database to a file the user picks (never into
        the evidence folder)."""
        from tkinter import filedialog, messagebox
        from utils import write_allowed
        rows = self.facts_rows()
        if not rows:
            return None
        path = filedialog.asksaveasfilename(
            parent=self, initialfile="databases.%s" % fmt, defaultextension="." + fmt,
            filetypes=[(fmt.upper(), "*." + fmt), ("All files", "*.*")])
        if not path:
            return None
        if not write_allowed(path):
            messagebox.showwarning("Not written", "That folder holds evidence: nothing is "
                                                  "written there. Choose another folder.",
                                   parent=self)
            return None
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(self.facts_text(rows, fmt))
        self.status.set("Written: %s (%d database%s)" % (path, len(rows),
                                                          "" if len(rows) == 1 else "s"))
        return path

    def _copy(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        try:
            self.status.set("Copied: %s" % what)
        except Exception:           # noqa: BLE001 - the status line is a convenience
            pass

    def _export_menu(self, button):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Copy the table (text)", command=lambda: self.copy_lines(every=True))
        m.add_command(label="Copy the table as CSV",
                      command=lambda: self.copy_lines(every=True, fmt="csv"))
        m.add_command(label="Copy the table as JSON",
                      command=lambda: self.copy_lines(every=True, fmt="json"))
        m.add_separator()
        m.add_command(label="Save as CSV…", command=lambda: self.export_facts("csv"))
        m.add_command(label="Save as JSON…", command=lambda: self.export_facts("json"))
        m.add_command(label="Save as text…", command=lambda: self.export_facts("txt"))
        try:
            m.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            m.grab_release()

    def _hover(self, iid):
        m = self._rows.get(iid)
        if m is None:
            return None
        det = getattr(m, "date_detection", None)
        lines = [m.name, m.path]
        sha = m.sha256()
        lines.append("SHA-256 %s" % sha if sha else "SHA-256: not computed yet")
        if m.size is not None:
            lines.append("%s bytes" % format(m.size, ","))
        if det is not None:
            for c in det.detected()[:8]:
                lines.append("%s.%s: %s, %s – %s" % (c.table, c.column, tl.SHORT.get(
                    c.kind, c.kind), fmt_day(c.first), fmt_day(c.last)))
            if len(det.detected()) > 8:
                lines.append("… and %d more date columns" % (len(det.detected()) - 8))
            for n in det.notes[:3]:
                lines.append(n)
        trg = self._triggers(m)[1]
        if trg:
            lines.append(trg + " (the database does this by itself; each trigger, in words "
                         "and SQL, is in the navigator under Triggers)")
        for b in member_warnings(m):
            lines.append("⚠ " + b.text)
        return "\n".join(lines)

    def rows(self):
        """[(values...)] of the lines shown (tests)."""
        return [tuple(self.tree.item(i, "values")) for i in self.tree.get_children()]
