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
from utils import fmtb
from widgets import SearchBox, ToolTip, wrap_to_width

COLUMNS = (("name", "Database", 170, "w"), ("app", "App / folder", 150, "w"),
           ("size", "Size", 70, "e"), ("tables", "Tables", 54, "e"),
           ("rows", "Rows", 80, "e"), ("wal", "WAL", 90, "w"), ("hash", "SHA-256", 90, "w"),
           ("dates", "Dates found (sampled)", 190, "w"), ("links_in", "Links in", 58, "e"),
           ("links_out", "Links out", 64, "e"))
POLL_MS = 150


def fmt_day(dt):
    return dt.strftime("%Y-%m-%d") if dt is not None else ""


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
        self.status = StatusLine(bar)
        self.status.pack(side="left", fill="x", expand=True, padx=(M, 0))

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=M, pady=(0, M))
        self.tree = ttk.Treeview(box, columns=[c[0] for c in COLUMNS], show="headings",
                                 selectmode="extended")
        for key, text, width, anchor in COLUMNS:
            self.tree.heading(key, text=text, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, minwidth=40, anchor=anchor,
                             stretch=key in ("name", "app", "dates"))
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
            hash_text = "✓ " + sha[:10] + "…"
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
        return (m.name, group_key(m.path), fmtb(m.size) if m.size is not None else "?",
                len(m.db.tables()) if m.db.ok else 0,
                ("" if exact else "~") + format(rows, ","), wal, hash_text, dates,
                li or "", lo or "")

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
        if k == "dates":
            _n, first, _l = date_range_of(getattr(m, "date_detection", None))
            return (first is None, first.isoformat() if first is not None else "")
        if k in ("links_in", "links_out"):
            li, lo = self._links(m)
            return (li if k == "links_in" else lo,)
        if k == "wal":
            return (m.db.mode if m.db.ok else "",)
        if k == "hash":
            return (bool(m.sha256()),)
        return (m.name.lower(),)

    def sort_by(self, key):
        if self.sort_col == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = key, key in ("size", "rows", "tables", "links_in",
                                                         "links_out")
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
            value.configure(text="%s – %s" % (fmt_day(min(firsts)), fmt_day(max(lasts))))
            sub.configure(text="%d date columns · %d of %d databases" % (
                cols, sum(1 for _m, d in dets if date_range_of(d)[0]), looked))
        else:
            value.configure(text="…" if looked < len(ms) else "none")
            sub.configure(text="looking for dates…" if looked < len(ms) else
                          "no date columns found")
        big = []
        for m in ms:
            for t, c in m.counts.items():
                if isinstance(c, int):
                    big.append((c, m.name, t))
        big.sort(reverse=True)
        _c, value, sub = self.cards["tables"]
        if big:
            value.configure(text=short_count(big[0][0]) + " rows")
            n, d, t = big[0]
            sub.configure(text="%s › %s" % (d, t))
            tip = getattr(self, "_big_tip", None)
            if tip is None:
                tip = self._big_tip = ToolTip(sub, "")
            tip.text = "The biggest tables:\n" + "\n".join(
                "%s › %s  %s rows" % (d, t, format(n, ",")) for n, d, t in big[:10])
        else:
            value.configure(text="…")
            sub.configure(text="counting rows…")
        rel = getattr(self.app, "relations", None)
        _c, value, sub = self.cards["links"]
        state = rel.state[0] if rel is not None else "idle"
        links = rel.cross_links() if rel is not None else []
        if state == "mapping":
            value.configure(text="…")
            sub.configure(text="checking values (runs in the background)")
        else:
            value.configure(text=str(len(links)))
            pairs = set((l.src_db, l.dst_db) for l in links)
            sub.configure(text="matched by value · %d pairs of databases" % len(pairs)
                          if links else "none found (matched by value)")
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
            return
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _hover(self, iid):
        m = self._rows.get(iid)
        if m is None:
            return None
        det = getattr(m, "date_detection", None)
        lines = [m.name, m.path]
        if det is not None:
            for c in det.detected()[:8]:
                lines.append("%s.%s: %s, %s – %s" % (c.table, c.column, tl.SHORT.get(
                    c.kind, c.kind), fmt_day(c.first), fmt_day(c.last)))
            if len(det.detected()) > 8:
                lines.append("… and %d more date columns" % (len(det.detected()) - 8))
            for n in det.notes[:3]:
                lines.append(n)
        for b in member_warnings(m):
            lines.append("⚠ " + b.text)
        return "\n".join(lines)

    def rows(self):
        """[(values...)] of the lines shown (tests)."""
        return [tuple(self.tree.item(i, "values")) for i in self.tree.get_children()]
