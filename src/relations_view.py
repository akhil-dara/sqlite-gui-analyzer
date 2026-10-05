"""Column relationships and value search in the UI (engine.relations, engine.crossdb,
engine.value_search).

RelationWindows  the App's entry point: maps the links of every open database in the
                 background (and, in a case of several databases, the links between them,
                 matched by value), builds the Related / Find items of the right-click menus,
                 marks linked column headers and opens the windows below.
RelatedWindow    the rows of the related tables holding one value (confident links only, one
                 line per table/column with rows, strongest first, the reason in plain words);
                 in a case also those of the other databases; the selected line's rows below
                 (double-click: the row detail; right-click: Tag).
FindWindow       the same for 'Find this value everywhere' / 'Find inside other values': every
                 table, WAL row version and freed-page record holding the value, known links
                 first; in a case, every database the search covers.
ColumnMapWindow  every column related to one column: the confident ones, and the weaker
                 matches in a collapsed section, each with its score and reasons.

Nothing opens by itself, and the menus only offer what is there: Related rows appear only for
a column with confident links and a value some related table holds. Work runs on worker
threads (grid.Runner: each closes its connections when it ends); the App stops them all
before a database is closed.
"""

import collections
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

from browse_sources import ListSource
from constants import C
from date_columns import BrowseDates
from tokens import FONT as F
from database import RID, BrowseRow
from dialogs import RowWin
from engine import limits
from engine.crossdb import find_links
from engine.relations import (ROWID, Link, Relation, coerce, display_column,
                              is_confident, plain_reason, relation_map, trivial)
from engine.schema import Locator
from engine.tags import entry_from_db_row, entry_from_freelist, entry_from_wal_record
from engine.value_search import find_everywhere, is_common
from engine.linkgraph import Link as MapLink
from grid import DataGrid, Runner
from utils import plain_text
from widgets import SearchBox, ToolTip, TreeviewTooltip, fit_geometry

LINKS = {"out": "→ refers to", "in": "← referred by", "peer": "= same values"}
KINDS = {"fk": "declared FOREIGN KEY", "name": "name", "same_name": "same name",
         "shared": "same target", "value": "matched by value"}
LINK_MARK = " ⇄"  # drawn after the header of a column with confident links (⇄)
LINK_TIP = ("⇄ linked: rows of other tables hold this column's values. Right-click a "
            "cell › Related rows to see them, or open the Relationships tab.")
MENU_TARGETS = 15           # related tables listed in a cell menu at most
POLL_MS = 60


def _next_line(tree, forward=True):
    """Select the next (previous) line of a list, wrapping (a search field's Enter)."""
    items = list(tree.get_children())
    if not items:
        return
    sel = tree.selection()
    i = items.index(sel[0]) if sel and sel[0] in items else -1
    i = (i + (1 if forward else -1)) % len(items) if i >= 0 else 0
    tree.selection_set(items[i])
    tree.see(items[i])


def value_text(v, limit=60):
    """A value in one short line."""
    s = v.display() if isinstance(v, Locator) else plain_text(v)
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[:limit - 1] + "…"


def column_label(session, table, column):
    return display_column(session.info(table), column) if column is ROWID else column


def rows_text(n):
    return "%s row%s" % (format(n, ","), "" if n == 1 else "s")


def findable(value):
    """Values 'Find this value everywhere' is offered for: not NULL, not empty."""
    if value is None or isinstance(value, Locator):
        return False
    if isinstance(value, (bytes, str)):
        return len(value) > 0
    return True


class _Single(object):
    """The App's one database, shaped like a case member (an App without a case)."""

    def __init__(self, app):
        self.app, self.uid = app, None
        self.name, self.color = "", ""

    @property
    def db(self):
        return self.app.db

    def label(self, table=None):
        return table if table is not None else ""


def cross_relation(link, member_uid, table, column, other):
    """An engine Relation for a cross-database link seen from (member, table, column):
    RelationMap.rows_for() of the other database reads its rows with it."""
    o_uid, o_table, o_col = link.other_end(member_uid, table, column)
    direction = "out" if (link.src_db, link.src_table) == (member_uid, table) and \
        link.src_col.lower() == column.lower() else "in"
    rel = Relation(table, column, o_table, o_col, "value", direction, [link.reason()],
                   [Link((table, column, o_table, o_col), "value", link.fraction)])
    rel.links[0].overlap = link.overlap
    return rel


class RelationWindows(object):
    """The App's relationships: background mapping, menus, header marks and windows."""

    def __init__(self, app):
        self.app = app
        self.windows = []
        self._ended = []            # runners of closed windows, until their thread ends
        self.mapper = Runner(app, "relations-map", release=app._release_worker_connection)
        self._map_stop = False
        self._map_flag = [False]    # the running mapping's own stop flag
        self._events = collections.deque()
        self._poll_id = None
        self._gen = 0
        self.listeners = []         # fn(state) after every mapping progress step
        self.state = ("idle", 0, 0)     # ('idle' | 'mapping' | 'done' | 'stopped', done, total)
        self.map_seconds = None
        self.cross = None           # engine.crossdb.CrossResult of the case (2+ databases)
        self.cross_seconds = None

    # -- the databases -----------------------------------------------------------------------
    def members(self):
        """The open databases (case members; one pseudo member for an App without a case)."""
        case = getattr(self.app, "case", None)
        if case is not None:
            return list(case)
        return [_Single(self.app)] if self.app.db.session is not None else []

    def multi(self):
        case = getattr(self.app, "case", None)
        return case is not None and len(case) > 1

    def active(self):
        case = getattr(self.app, "case", None)
        if case is not None:
            return case.active
        return _Single(self.app) if self.app.db.session is not None else None

    def member(self, uid):
        for m in self.members():
            if m.uid == uid:
                return m
        return None

    def _m(self, member):
        return member if member is not None else self.active()

    # -- the map -----------------------------------------------------------------------------
    @property
    def map(self):
        s = self.app.db.session
        return relation_map(s) if s is not None else None

    def map_of(self, member):
        member = self._m(member)
        s = member.db.session if member is not None else None
        return relation_map(s) if s is not None else None

    def start_mapping(self):
        """Check every link of every open database against its values in the background,
        then (2+ databases) find the links between them by their values."""
        members = [m for m in self.members() if m.db.session is not None]
        if not members:
            return
        self._map_stop = False
        self._gen += 1
        gen = self._gen
        events = self._events
        t0 = time.perf_counter()
        maps = [(m, relation_map(m.db.session)) for m in members]
        cross = len(members) > 1
        # the tables of each map are indexed on the worker (schema only, but for a case of
        # many databases too long for the Tk thread); until then the schema's table count
        # stands in for the total
        guess = [0]
        for m in members:
            try:
                guess[0] += len(m.db.session.schema.names("table"))
            except Exception:           # noqa: BLE001 - only an estimate
                pass
        total = [guess[0] + (1 if cross else 0)]
        # this job's own stop flag: a job stopped and replaced by a new one must not resume
        # when the new one clears its flag
        self._map_flag[0] = True            # a mapping still running is replaced
        flag = self._map_flag = [False]
        stop = lambda: flag[0] or self._map_stop    # noqa: E731

        def work():
            sizes = []
            for _m, mp in maps:
                if stop():
                    return False, None
                sizes.append(len(mp.build().tables))
            total[0] = sum(sizes) + (1 if cross else 0)
            base = 0
            for (m, mp), n in zip(maps, sizes):
                ok = mp.map_links(stop, lambda done, _t, b=base: events.append(
                    ("progress", b + done, total[0])))
                if not ok:
                    return False, None
                base += n
            if not cross:
                return True, None
            t1 = time.perf_counter()
            res = find_links([(m.uid, m.db.session) for m, _mp in maps], stop)
            res.seconds = time.perf_counter() - t1
            by_uid = dict((m.uid, mp) for m, mp in maps)
            for l in res.confident():       # row counts of the linked tables, for the list
                for uid, table in ((l.src_db, l.src_table), (l.dst_db, l.dst_table)):
                    if stop():
                        break
                    by_uid[uid].table_rows(table)
            events.append(("progress", total[0], total[0]))
            return not res.cancelled, res

        def done(result, error):
            if gen != self._gen:
                return
            self.map_seconds = time.perf_counter() - t0
            self._drain()
            ok, res = result if result is not None else (False, None)
            if res is not None:
                self.cross = res
                self.cross_seconds = getattr(res, "seconds", None)
            self.state = ("done" if ok and error is None else "stopped", total[0], total[0])
            self._notify()
            self.refresh_marks()
        self.cross = None
        self.state = ("mapping", 0, total[0])
        self.mapper.submit("map", work, done)
        self._schedule()

    def _schedule(self):
        if self._poll_id is None:
            try:
                self._poll_id = self.app.after(POLL_MS * 3, self._poll)
            except tk.TclError:
                self._poll_id = None

    def _poll(self):
        self._poll_id = None
        self._drain()
        if self.mapper.busy():
            self._schedule()

    def _drain(self):
        changed = False
        while self._events:
            _kind, done, total = self._events.popleft()
            if self.state[0] == "mapping":
                self.state = ("mapping", done, total)
                changed = True
        if changed:
            self._notify()

    def _notify(self):
        for fn in list(self.listeners):
            try:
                fn(self.state)
            except Exception:           # noqa: BLE001 - a listener must not stop the others
                self.app.report_callback_exception(*sys.exc_info())

    def confident(self, table, column, member=None):
        """The column's confident relations when known (never reads the database), else None."""
        m = self.map_of(member)
        if m is None or not self.supported(table, member):
            return None
        try:
            return m.known_confident(table, column)
        except KeyError:
            return []

    # -- links between databases ---------------------------------------------------------------
    def cross_links(self, confident=True):
        """The links between the open databases (matched by value), trusted ones only by
        default."""
        res = self.cross
        if res is None or not self.multi():
            return []
        uids = set(m.uid for m in self.members())
        return [l for l in res.links if (l.confident or not confident)
                and l.src_db in uids and l.dst_db in uids]

    def cross_targets(self, table, column, member=None):
        """[(CrossLink, other member, other table, other column)] of the trusted links of
        member's table.column to other databases."""
        member = self._m(member)
        if member is None or column in (None, False) or column is ROWID:
            return []
        out = []
        for l in self.cross_links():
            if l.touches(member.uid, table, column):
                o_uid, o_table, o_col = l.other_end(member.uid, table, column)
                other = self.member(o_uid)
                if other is not None and other.db.session is not None:
                    out.append((l, other, o_table, o_col))
        return out

    def forget_member(self, member):
        """A database leaves the case: close its windows, drop its links."""
        for w in list(self.windows):
            if getattr(w, "member", None) is member or member in getattr(w, "members", ()):
                w.close()
        res = self.cross
        if res is not None:
            res.links = [l for l in res.links if member.uid not in (l.src_db, l.dst_db)]

    def refresh_marks(self):
        """Mark the Browse grid's headers of columns with confident links (in the database or
        to another one)."""
        grid = getattr(self.app, "_browse_grid", None)
        table = self.app._browse_table_var.get() if hasattr(self.app, "_browse_table_var") \
            else None
        if grid is None:
            return
        marks = {}
        if table and self.supported(table):
            cols = grid.columns()
            for c, name in enumerate(cols):
                column = self.key_column(table, name)[0]
                if column is False:
                    continue
                rels = self.confident(table, column)
                if rels or self.cross_targets(table, column):
                    marks[c] = LINK_MARK
        grid.mark_tips[LINK_MARK.strip()] = LINK_TIP
        grid.set_header_marks(marks)

    # -- helpers -----------------------------------------------------------------------------
    def supported(self, table, member=None):
        """True for a table the relation map covers (not a view, virtual or sqlite_ table)."""
        member = self._m(member)
        s = member.db.session if member is not None else None
        if s is None or not table or table.lower().startswith("sqlite_"):
            return False
        info = s.schema.get(table)
        return info is not None and info.kind == "table" and bool(info.columns)

    def key_column(self, table, column, value=None, member=None):
        """(column, value) for engine.relations: the row locator column '_rid' (with a Locator
        value) stands for the table's key - its INTEGER PRIMARY KEY or rowid (ROWID), or a
        one-column PRIMARY KEY; (False, None) when the table has no such key."""
        if column != RID:
            return column, value
        info = self._m(member).db.session.info(table)
        kind = getattr(value, "kind", None)
        if not info.without_rowid and info.kind == "table":
            key = info.columns[info.rowid_alias].name if info.rowid_alias is not None else ROWID
            return key, (value.value if kind == "rowid" else None)
        if info.without_rowid and len(info.pk_columns) == 1:
            key = info.columns[info.pk_columns[0]].name
            return key, (value.value[0] if kind == "pk" else None)
        return False, None

    def related_counts(self, table, column, value, member=None):
        """[(Relation, rows)] of the confident relations holding the value, best first,
        counted only where that is instant; [] when there is nothing to offer."""
        m = self.map_of(member)
        if m is None or not self.supported(table, member) or column is False or trivial(value):
            return []
        try:
            got = m.quick_counts(table, column, value)
        except KeyError:
            return []
        return got or []

    def cross_counts(self, table, column, value, member=None):
        """[(Relation, other member, rows)] of the other databases' rows holding the value
        through a trusted link, counted only where that is instant."""
        member = self._m(member)
        if column in (False, None) or trivial(value):
            return []
        out = []
        for link, other, _t, _c in self.cross_targets(table, column, member):
            rel = cross_relation(link, member.uid, table, column, other)
            om = relation_map(other.db.session)
            try:
                if not om.cheap(rel, value):
                    continue
                n = om.rows_for(rel, value, limit=1).count
            except Exception:           # noqa: BLE001 - a table that cannot be read
                continue
            if n:
                out.append((rel, other, n))
        return out

    # -- menus -------------------------------------------------------------------------------
    def value_menu(self, menu, table, column, value, locator=None, member=None):
        """Add the Related rows submenu (only when related tables hold the value) and the Find
        items (for a value that is not NULL or empty) for one cell. column may be '_rid' with
        the row's Locator as value. In a case of several databases the submenu lists the rows
        'In this database' and 'In other databases'. Returns the number of items added."""
        added = 0
        member = self._m(member)
        key, kval = (column, value)
        if table and self.supported(table, member):
            key, kval = self.key_column(table, column, value, member)
        counts = self.related_counts(table, key, kval, member) if table else []
        cross = self.cross_counts(table, key, kval, member) if table and self.multi() else []
        find_value = kval if column == RID else value
        if not counts and not cross and not findable(find_value):
            return 0
        menu.add_separator()
        if counts or cross:
            sub = tk.Menu(menu, tearoff=0)
            session = member.db.session
            if cross:
                sub.add_command(label="In this database (%s)" % member.name, state="disabled")
                if not counts:
                    sub.add_command(label="(no related rows)", state="disabled")
            for rel, n in counts[:MENU_TARGETS]:
                label = "%s (%s) — %s" % (rel.other, column_label(session, rel.other,
                                                                   rel.other_column),
                                                rows_text(n))
                sub.add_command(label=label, command=lambda r=rel: self.related(
                    table, key, kval, select=(r.other, r.other_column), member=member))
            if len(counts) > MENU_TARGETS:
                sub.add_command(label="… %d more" % (len(counts) - MENU_TARGETS),
                                command=lambda: self.related(table, key, kval, member=member))
            if cross:
                sub.add_separator()
                sub.add_command(label="In other databases (matched by value)",
                                state="disabled")
                for rel, other, n in cross[:MENU_TARGETS]:
                    sub.add_command(label="%s (%s) — %s" % (other.label(rel.other),
                                                          rel.other_column, rows_text(n)),
                                    command=lambda r=rel, o=other: self.related(
                                        table, key, kval, member=member,
                                        select=(r.other, r.other_column, o.uid)))
            sub.add_separator()
            sub.add_command(label="All related rows…",
                            command=lambda: self.related(table, key, kval, member=member))
            menu.add_cascade(label="Related rows", menu=sub)
            added += 1
        if findable(find_value):
            origin = (table, column if column != RID else key, locator) if table else None
            menu.add_command(label="Find this value everywhere",
                             command=lambda: self.find(find_value, False, origin, member))
            added += 1
            if isinstance(find_value, (str, bytes)):
                menu.add_command(label="Find inside other values",
                                 command=lambda: self.find(find_value, True, origin, member))
                added += 1
        return added

    def browse_menu(self, menu, table, data, col):
        """Browse cell menu items (see value_menu)."""
        if not table or table.startswith("WAL: ") or not data or col is None \
                or col >= len(data[0]):
            return 0
        name = self.app._browse_grid.columns()[col]
        loc = data[0][0]
        if not self.supported(table):
            return self.value_menu(menu, None, name, data[0][col]) if name != RID else 0
        return self.value_menu(menu, table, name, data[0][col], loc)

    def header_menu(self, menu, table, c):
        """Browse column header menu item: the column's relationships."""
        if not self.supported(table):
            return
        column = self.key_column(table, self.app._browse_grid.columns()[c])[0]
        if column is False:
            return
        menu.add_separator()
        menu.add_command(label="Column relationships…",
                         command=lambda: self.column_map(table, column))

    def search_menu(self, menu, result):
        """Search result menu items for the matched cell's whole value (of the database the
        result came from)."""
        member = self.member(result.get("dbid")) if result.get("dbid") is not None else None
        member = self._m(member)
        s = member.db.session if member is not None else None
        table, column, row = result.get("table"), result.get("column"), result.get("row")
        if s is None or not row or column is None:
            return 0
        source = result.get("source") or "DB"
        info = s.schema.get(table)
        if source in ("DB", "Database") and info is not None:
            cols = s.visible_columns(table)
            if column in cols and cols.index(column) < len(row):
                v = row[cols.index(column)]
                if self.supported(table, member):
                    return self.value_menu(menu, table, column, v, result.get("locator"),
                                           member)
                return self.value_menu(menu, None, column, v, member=member)
        return 0

    # -- windows -----------------------------------------------------------------------------
    def related(self, table, column, value, select=None, member=None):
        """Open the rows related to table.column = value (column may be ROWID, or '_rid' with
        a row locator). None when there is nothing to look up."""
        member = self._m(member)
        if not self.supported(table, member):
            return None
        column, value = self.key_column(table, column, value, member)
        if column is False or trivial(value):
            return None
        w = RelatedWindow(self, table, column, value, select, member)
        self.windows.append(w)
        return w

    def find(self, value, contains=False, origin=None, member=None):
        """Open 'Find this value everywhere' (contains: 'Find inside other values'); in a case,
        every database the search covers."""
        if not findable(value) or self.app.db.session is None:
            return None
        w = FindWindow(self, value, contains, origin, self._m(member))
        self.windows.append(w)
        return w

    def column_map(self, table, column, member=None):
        """Open the map of every column related to table.column ('_rid': the table's key)."""
        member = self._m(member)
        if not self.supported(table, member):
            return None
        column = self.key_column(table, column, member=member)[0]
        if column is False:
            return None
        for w in self.windows:
            if isinstance(w, ColumnMapWindow) and (w.table, w.column, w.member.uid) == \
                    (table, column, member.uid):
                w.lift()
                return w
        w = ColumnMapWindow(self, table, column, member)
        self.windows.append(w)
        return w

    def forget(self, w):
        if w in self.windows:
            self.windows.remove(w)
        self._ended.append(w.runner)

    def stop(self):
        """Stop the mapping and every window's work (the App interrupts their statements)."""
        self.stop_mapping()
        for w in self.windows:
            w.stop()

    def stop_mapping(self):
        """Stop the mapping of the links only (start_mapping() starts it again); the windows
        keep working. Returns the mapping's threads still running."""
        self._map_stop = True
        self._map_flag[0] = True
        self._gen += 1
        self.mapper.cancel()
        return self.mapper.threads()

    def worker_threads(self):
        self._ended = [r for r in self._ended if r.threads()]
        out = list(self.mapper.threads())
        for r in [w.runner for w in self.windows] + self._ended:
            out.extend(r.threads())
        return out

    def close_all(self):
        """Close the windows and forget the mapping (the database is being closed)."""
        for w in list(self.windows):
            w.close()
        self._events.clear()
        self.state = ("idle", 0, 0)
        self.map_seconds = None
        self.cross = None
        self.cross_seconds = None
        self._notify()


class _Window(tk.Toplevel):
    """A relationship window: one job at a time on its own worker thread; the job reports
    through emit(event), handled on the Tk thread by on_event()."""

    def __init__(self, manager, title, member=None):
        tk.Toplevel.__init__(self, manager.app)
        self.manager, self.app = manager, manager.app
        self.member = member if member is not None else manager.active()
        self.session = self.member.db.session
        self.title(title)
        self.configure(bg=C["bg"])
        self.runner = Runner(self, "relations", release=self.app._release_worker_connection)
        self._stop = False
        self._closed = False
        self._events = collections.deque()
        self._poll_id = None
        self._t0 = None
        self.protocol("WM_DELETE_WINDOW", self.close)

    def where(self, table):
        """'table', or in a case 'wa.db › table'."""
        return self.member.label(table) if self.manager.multi() else table

    def run(self, key, work, done):
        """work(cancel, emit) on the worker thread; done(result, error) on the Tk thread."""
        self._stop = False
        self._t0 = time.perf_counter()
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
        if th is not None:
            case = getattr(self.app, "case", None)
            for t in [th] + [t for t in getattr(self, "_pool_threads", ()) if t.is_alive()]:
                if case is not None:
                    case.interrupt(t)   # the job may read several databases of the case
                elif self.app.db.session is not None:
                    self.app.db.interrupt(t)

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

    def elapsed(self):
        return (time.perf_counter() - self._t0) if self._t0 is not None else 0.0

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

    # the size each kind of window was last given by the user (this session)
    _sizes = {}

    def size_to(self, width, height):
        """Open at the size the content needs (or the size this kind of window was last left
        at), never larger than the screen."""
        remembered = _Window._sizes.get(type(self).__name__)
        if remembered:
            width, height = remembered
        # a size the user gave is in pixels already; the defaults grow with the scaling
        fit_geometry(self, int(width), int(height), scale=not remembered)
        self.bind("<Configure>", self._remember, add="+")

    def _remember(self, event):
        if event.widget is self and event.width > 200 and event.height > 150 and \
                getattr(self, "_sized", False):
            _Window._sizes[type(self).__name__] = (event.width, event.height)
        self._sized = True

    def _tree(self, parent, columns, height=10, show="headings"):
        box = ttk.Frame(parent)
        tree = ttk.Treeview(box, columns=[c for c, _t, _w in columns], show=show,
                            height=height, selectmode="browse")
        if show != "headings":
            tree.column("#0", width=28, stretch=False)
        for c, title, w in columns:
            tree.heading(c, text=title)
            tree.column(c, width=w, minwidth=40, stretch=w >= 300, anchor="w")
        ysb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        TreeviewTooltip(tree)
        tree.tag_configure("known", foreground=C["green"])
        tree.tag_configure("cross", foreground=C["orange"])
        tree.tag_configure("weak", foreground=C["text2"])
        tree.tag_configure("group", foreground=C["text2"], font=F["italic"])
        return box, tree

    def _popup(self, menu, event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


class Group(object):
    """One line of a rows window: the rows of one source/table/column holding the value (of
    one database: member)."""
    __slots__ = ("source", "table", "column", "columns", "rows", "count", "link", "why",
                 "strength", "known", "entries", "member", "cut")

    def __init__(self, source, table, column, columns, rows, count, link, why, strength,
                 known, entries, member=None):
        self.source, self.table, self.column, self.columns = source, table, column, columns
        self.rows, self.count, self.link, self.why = rows, count, link, why
        self.strength, self.known, self.entries = strength, known, entries
        self.member = member
        self.cut = False                # the search stopped at its limit in this table


class _RowsWindow(_Window):
    """Lines (Groups) above, the selected line's rows in a grid below."""

    HEAD = ""

    def __init__(self, manager, title, header, member=None):
        _Window.__init__(self, manager, title, member)
        self.size_to(1100, 720)
        self.groups = []
        self._shown = {}            # tree iid -> Group
        self._current = None
        self._row_index = {}        # id(grid row) -> index into the shown Group's rows
        self._select = None         # (table, column[, member uid]) to show first
        self._done = False
        self._build(header)

    def _build(self, header):
        # the buttons first, at the bottom: a small window shrinks the lists, never them
        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=6, side="bottom")
        for text, cmd, tip in (("Close", self.close, "Close this window"),
                               ("Stop", self.stop_work, "Stop the search"),
                               ("Open in Browse", self._browse_selected,
                                "Open the selected line's table in Browse, on its rows"),
                               ("Tag all rows found…", self._tag_all_popup,
                                "Tag every row listed in this window")):
            b = ttk.Button(bot, text=text, command=cmd)
            b.pack(side="right", padx=3)
            ToolTip(b, tip)
            if text.startswith("Tag"):
                self.tag_all_btn = b
            if text == "Stop":
                self.stop_btn = b       # shown only while the search runs (finish hides it)
        top = self.top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text=header, style="B.TLabel").pack(side="left")
        self.warning = ttk.Label(self, text="", style="M.TLabel", foreground=C["orange"])
        self.status = ttk.Label(self, text="", style="M.TLabel", anchor="w")
        self.status.pack(fill="x", padx=10, pady=(0, 4))
        self.search = SearchBox(self, placeholder="Find a table or column…",
                                on_change=lambda t: self.refresh(),
                                on_next=lambda f: _next_line(self.tree, f), width=30)
        self.search.pack(fill="x", padx=8, pady=(0, 2))
        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=8, pady=2)
        upper = ttk.Frame(pane)
        box, self.tree = self._tree(upper, (("source", "Found in", 70), ("table", "Table", 190),
                                            ("column", "Column", 160), ("rows", "Rows", 70),
                                            ("link", "Link", 130), ("why", "Why", 420)))
        if self.manager.multi():
            self.tree.column("source", width=160)
        box.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())
        self.tree.bind("<Double-1>", lambda e: self._browse_selected())
        self.tree.bind("<Button-3>", self._tree_menu)
        pane.add(upper, weight=1)
        lower = ttk.Frame(pane)
        self.rows_label = ttk.Label(lower, text="", style="M.TLabel")
        self.rows_label.pack(fill="x", padx=2, pady=(4, 2))
        self.grid = DataGrid(lower, frozen=1, on_open_row=self._open_row,
                             on_context_menu=self._grid_menu,
                             on_header_menu=self._grid_header_menu)
        self.grid.pack(fill="both", expand=True)
        pane.add(lower, weight=2)
        # 'Show as date' for the related rows, sharing the Browse choices
        self._dates = BrowseDates(
            self.app, grid=self.grid,
            table_fn=lambda: self._current.table if self._current is not None else None,
            db_fn=lambda: self._gmember(self._current).db
            if self._current is not None else self.app.db)

    def _grid_header_menu(self, menu, c):
        if getattr(self, "_dates", None) is not None:
            self._dates.header_menu(menu, c)

    def _line_matches(self, g):
        term = self.search.get().lower() if hasattr(self, "search") else ""
        if not term:
            return True
        return any(term in str(v).lower() for v in (g.table, g.column, g.why, g.link,
                                                    self._source_text(g)))

    def set_warning(self, text):
        self.warning.configure(text=text)
        if text and not self.warning.winfo_manager():
            self.warning.pack(fill="x", padx=10, pady=(0, 2), before=self.status)

    def stop_work(self):
        self.stop()
        self.status.configure(text=self.summary() + " (stopped)")

    def _gmember(self, g):
        return g.member if g.member is not None else self.member

    def _matches_select(self, g):
        sel = self._select
        if sel is None:
            return True
        if (g.table, g.column) != tuple(sel[:2]):
            return False
        return len(sel) < 3 or self._gmember(g).uid == sel[2]

    def add_group(self, g):
        """A line arrived from the worker."""
        self.groups.append(g)
        if not self._line_matches(g):
            return
        self._insert(g)
        if self._current is None and self._matches_select(g):
            self._pick(g)

    def _pick(self, g):
        for iid, grp in self._shown.items():
            if grp is g:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                self.show_rows(g)
                return

    def _source_text(self, g):
        """'DB', 'WAL' ...; in a case 'wa.db · DB' (every line says which database)."""
        if not self.manager.multi():
            return g.source
        return "%s · %s" % (self._gmember(g).name, g.source)

    def _insert(self, g):
        m = self._gmember(g)
        tags = ("cross",) if m is not self.member and g.known else \
            (("known",) if g.known else ())
        iid = self.tree.insert("", "end", values=(
            self._source_text(g), g.table, column_label(m.db.session, g.table, g.column)
            if g.source == "DB" and m.db.session.schema.get(g.table) is not None else g.column,
            format(g.count, ","), g.link, g.why), tags=tags)
        self._shown[iid] = g
        return iid

    def refresh(self):
        """Rebuild the list, strongest first (known links first, then by strength); lines of
        this database before those of other databases."""
        keep = self._current
        self.tree.delete(*self.tree.get_children())
        self._shown = {}
        n = 0
        for g in sorted(self.groups, key=lambda g: (self._gmember(g) is not self.member,
                                                    -g.known, -g.strength, -g.count,
                                                    g.source, g.table, str(g.column))):
            if not self._line_matches(g):
                continue
            n += 1
            iid = self._insert(g)
            if g is keep:
                self.tree.selection_set(iid)
                self.tree.see(iid)
        self.search.set_count(n, len(self.groups), "line", "lines", "table or column")

    def finish(self, text):
        self._done = True
        btn = getattr(self, "stop_btn", None)
        if btn is not None:
            try:
                btn.pack_forget()       # nothing runs any more: no Stop that looks busy
            except tk.TclError:
                pass
        self.refresh()
        if self._current is None and self.tree.get_children():
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.show_rows(self._shown[first])
        self.status.configure(text=text)

    def selected(self):
        sel = self.tree.selection()
        return self._shown.get(sel[0]) if sel else None

    def _show_selected(self):
        g = self.selected()
        if g is not None and g is not self._current:
            self.show_rows(g)

    def show_rows(self, g):
        self._current = g
        m = self._gmember(g)
        rows = [(BrowseRow([loc] + list(values), flags), flags) for loc, values, flags in g.rows]
        self._row_index = dict((id(r), i) for i, (r, _f) in enumerate(rows))
        self.grid.set_source(ListSource([RID] + list(g.columns), rows,
                                        encoding=m.db.session.encoding))
        if getattr(self, "_dates", None) is not None:
            self._dates.apply(g.table)
        more = " (the first %s shown: limit related_rows_shown / value_search_rows)" \
            % format(len(g.rows), ",") if g.count > len(g.rows) else ""
        if getattr(g, "cut", False):
            more += " — the search stopped at %s rows in this table (limit " \
                    "value_search_rows): there may be more" % format(len(g.rows), ",")
        where = "" if g.source == "DB" else " (%s)" % g.source
        name = m.label(g.table) if self.manager.multi() else g.table
        self.rows_label.configure(text="%s%s: %s holding the value in %s%s" % (
            name, where, rows_text(g.count), g.column if g.column is not ROWID else "rowid",
            more))

    def _open_row(self, _row, values):
        g = self._current
        if values and g is not None and g.source == "DB" and \
                getattr(values[0], "kind", None) in ("rowid", "pk", "ordinal"):
            RowWin.show(self.app, self._gmember(g).db, g.table, values[0])

    def _browse_selected(self):
        g = self.selected()
        if g is None or g.source != "DB":
            return
        m = self._gmember(g)
        if m.db.session.schema.get(g.table) is None:
            return
        activate = getattr(self.app, "activate_member", None)
        if activate is not None and m.uid is not None:
            activate(m)                 # Browse shows the active database
        self.app.browse_related(g.table, g.column, self.browse_value(g))

    def browse_value(self, g):
        return None

    # -- tags ----------------------------------------------------------------------------------
    def all_entries(self):
        out = []
        for g in self.groups:
            out.extend(g.entries(i) for i in range(len(g.rows)))
        return [e for e in out if e is not None]

    def _mark(self, g, make):
        """Entries of a line belong to its database."""
        m = self._gmember(g)
        tags = getattr(self.app, "tags", None)
        if tags is None or m.uid is None or not hasattr(tags, "mark"):
            return make
        return lambda i: tags.mark(make(i), m)

    def _grid_menu(self, menu, row, _col):
        g = self._current
        if g is None:
            return
        lo, hi = self.grid.selected_rows() or (row, row)
        index = self._row_index

        def entries():
            picked = [index.get(id(values)) for values, _f in self.grid.fetch_rows(lo, hi)]
            return [e for e in (g.entries(i) for i in picked if i is not None) if e is not None]
        menu.add_separator()
        dmu = getattr(self.app, "datamap", None)
        if dmu is not None and g.source == "DB":
            m = self._gmember(g)
            dmu.row_menu(menu, g.table, [values[0] for values, _f in self.grid.fetch_rows(lo, hi)
                                         if values], m if m.uid is not None else None)
        self.app.tag_menu(menu, entries)

    def _tree_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        g = self._shown.get(iid)
        menu = tk.Menu(self, tearoff=0)
        if g is not None:
            m = self._gmember(g)
            if g.source == "DB" and m.db.session.schema.get(g.table) is not None:
                menu.add_command(label="Open in Browse", command=self._browse_selected)
                if self.manager.supported(g.table, m):
                    menu.add_command(label="Column relationships of %s.%s…" % (
                        g.table, column_label(m.db.session, g.table, g.column)),
                        command=lambda: self.manager.column_map(g.table, g.column, m))
                menu.add_separator()
            self.app.tag_menu(menu, lambda: [e for e in (g.entries(i)
                                                         for i in range(len(g.rows)))
                                             if e is not None],
                              "Tag the %s of %s" % (rows_text(len(g.rows)), g.table))
        self.app.tag_menu(menu, self.all_entries, "Tag all rows found")
        self._popup(menu, event)

    def tag_all_menu(self):
        menu = tk.Menu(self, tearoff=0)
        self.app.tag_menu(menu, self.all_entries, "Tag all rows found")
        return menu

    def _tag_all_popup(self):
        menu = self.tag_all_menu()
        b = self.tag_all_btn
        try:
            menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            menu.grab_release()


def _db_entries(table, columns, rows):
    return lambda i: entry_from_db_row(table, rows[i][0], list(columns), list(rows[i][1]),
                                       rows[i][2])


class RelatedWindow(_RowsWindow):
    """Rows of the related tables holding one value: confident links, lines with rows only;
    in a case also the rows of the other databases reached by a link matched by value."""

    def __init__(self, manager, table, column, value, select=None, member=None):
        self.table, self.column, self.value = table, column, value
        member = member if member is not None else manager.active()
        session = member.db.session
        col = column_label(session, table, column)
        where = member.label(table) if manager.multi() else table
        _RowsWindow.__init__(self, manager, "Related rows: %s.%s = %s" % (
            where, col, value_text(value, 40)),
            "Rows related to %s.%s = %s" % (where, col, value_text(value, 80)), member)
        self._select = select
        self.members = [member]
        self.start()

    def start(self):
        table, column, value, session = self.table, self.column, self.value, self.session
        member = self.member
        cross = [(cross_relation(l, member.uid, table, column, o), o)
                 for l, o, _t, _c in self.manager.cross_targets(table, column, member)]
        self.members = [member] + [o for _r, o in cross]
        self._searched = collections.OrderedDict((m.uid, m) for m in self.members)

        def work(cancel, emit):
            m = relation_map(session)
            rels = m.confident(table, column, cancel)
            if rels is None:
                return False
            for rel in rels:
                if cancel():
                    return False
                res = m.rows_for(rel, value, limits.get("related_rows_shown"), cancel)
                if res.count:
                    emit((rel, res, None))
            for rel, other in cross:
                if cancel():
                    return False
                res = relation_map(other.db.session).rows_for(
                    rel, value, limits.get("related_rows_shown"), cancel)
                if res.count:
                    emit((rel, res, other))
            return True
        self.status.configure(text="Looking in the related tables%s…" % (
            " of %d databases" % len(self.members) if len(self.members) > 1 else ""))
        self.run("related", work, self._finished)

    def on_event(self, event):
        rel, res, other = event
        rows = [(r.locator, list(r.values), r.flags) for r in res.rows]
        g = Group("DB", res.table, res.column, res.columns, rows, res.count,
                  LINKS[rel.direction], plain_reason(rel) if other is None else rel.reasons[0],
                  rel.score, True, _db_entries(res.table, res.columns, rows),
                  other if other is not None else None)
        g.entries = self._mark(g, g.entries)
        self.add_group(g)
        self.status.configure(text=self.summary() + "…")

    def browse_value(self, g):
        return coerce(self.value, relation_map(self._gmember(g).db.session).affinity(
            g.table, g.column))

    def summary(self):
        n = sum(g.count for g in self.groups)
        text = "%s in %d related table column%s" % (
            rows_text(n), len(self.groups), "" if len(self.groups) == 1 else "s")
        if len(self.members) > 1:
            # every database looked in, with what was found there
            per = []
            for m in self.members:
                k = sum(g.count for g in self.groups if self._gmember(g) is m)
                per.append("%s: %s" % (m.name, rows_text(k) if k else "nothing found"))
            text += " — " + "; ".join(per)
        return text + " (%.1f s)" % self.elapsed()

    def _finished(self, result, error):
        if error is not None:
            self.finish("Failed: %s" % error)
        elif not self.groups:
            self.finish("No related table holds this value%s." % (
                " (looked in %s)" % ", ".join(m.name for m in self.members)
                if len(self.members) > 1 else "") if result
                else "Stopped before a related row was found.")
        else:
            self.finish(self.summary() + ("" if result else " (stopped)"))


class FindWindow(_RowsWindow):
    """Every table, WAL row version and freed-page record holding one value; in a case,
    every database the search covers (each line says which)."""

    def __init__(self, manager, value, contains=False, origin=None, member=None):
        self.value, self.contains, self.origin = value, contains, origin
        what = "Find inside other values" if contains else "Find this value everywhere"
        _RowsWindow.__init__(self, manager, "%s: %s" % (what, value_text(value, 40)),
                             "%s: %s" % (what, value_text(value, 80)), member)
        self._tables_done, self._tables_total = 0, 0
        self._hits = collections.OrderedDict()      # (source, table, column) -> [ValueHit]
        self.errors = []
        self._pool_threads = []
        if is_common(value):
            self.set_warning("Common value: matches may be coincidental. Known links are "
                             "listed first.")
        self.members = self._scope()
        self.known = self._known_targets()
        if self.manager.multi() and hasattr(self.app, "scopes"):
            from scope import ScopePicker
            # the databases searched; another choice searches again in a new window
            self.scope_picker = ScopePicker(self.top, self.app, "find",
                                            on_change=self._scope_changed, prefix="In: ",
                                            own_changes_only=True)
            self.scope_picker.pack(side="right")
        self.start()

    def _scope_changed(self):
        if self._closed_or_gone():
            return
        FindWindow(self.manager, self.value, self.contains, self.origin, self.member)
        self.close()

    def _closed_or_gone(self):
        try:
            return not self.winfo_exists()
        except tk.TclError:
            return True

    def _scope(self):
        """The databases searched: in a case, those the 'Find everywhere' scope covers (the
        global scope unless it has its own), always including the one the value came from."""
        if not self.manager.multi():
            return [self.member]
        scopes = getattr(self.app, "scopes", None)
        chosen = scopes.members("find") if scopes is not None else self.manager.members()
        out = [self.member] + [m for m in chosen if m is not self.member]
        return [m for m in out if m.db.session is not None]

    def _known_targets(self):
        """(member uid, table, column) confidently related to the column the value came from,
        in its database and in the others."""
        if not self.origin or not self.origin[0]:
            return set()
        table, column = self.origin[0], self.origin[1]
        rels = self.manager.confident(table, column, self.member) or []
        out = set((self.member.uid, r.other, str(r.other_column).lower()) for r in rels)
        for _l, o, t, c in self.manager.cross_targets(table, column, self.member):
            out.add((o.uid, t, c.lower()))
        return out

    def start(self):
        value, contains = self.value, self.contains
        jobs = []
        total = 0
        for m in self.members:
            records = m.db.recovered_records()
            origin = self.origin if m is self.member else None
            jobs.append((m, records, origin))
            total += len(m.db.session.tables()) + len(records)
        self._tables_total = total
        self._done_dbs = []

        release = self.app._release_worker_connection
        threads = self._pool_threads

        def one(job, cancel, emit):
            m, records, origin = job
            for name, hits, err in find_everywhere(m.db.session, value, contains,
                                                   records=records, origin=origin,
                                                   limit=limits.get("value_search_rows"),
                                                   cancel=cancel):
                emit((m, name, hits, err))
            if not cancel():
                emit((m, None, None, None))

        def work(cancel, emit):
            if len(jobs) == 1:
                one(jobs[0], cancel, emit)
                return not cancel()

            # several databases side by side (each one's tables in parallel as well); each
            # thread closes its connections when its database is done
            def task(job):
                threads.append(threading.current_thread())
                try:
                    one(job, cancel, emit)
                finally:
                    release()
            with ThreadPoolExecutor(max_workers=min(limits.get("case_search_parallel"),
                                                    len(jobs))) as ex:
                for f in [ex.submit(task, j) for j in jobs]:
                    f.result()
            return not cancel()
        n = sum(len(m.db.session.tables()) for m in self.members)
        if len(self.members) > 1:
            self.status.configure(text="Searching %d databases (%s) · %d tables…" % (
                len(self.members), ", ".join(m.name for m in self.members), n))
        else:
            self.status.configure(text="Searching %d tables…" % n)
        self.run("find", work, self._finished)

    def on_event(self, event):
        m, name, hits, err = event
        if name is None:
            self._done_dbs.append(m)
            if len(self.members) > 1:   # which databases are done, with what they gave
                self.status.configure(text="Searched %d of %d… %s — %s" % (
                    self._tables_done, self._tables_total, self.summary(),
                    self.per_database(running=True)))
            return
        self._tables_done += 1
        if err is not None:
            self.errors.append("%s%s: %s" % (m.name + " " if len(self.members) > 1 else "",
                                             name, err))
        groups = collections.OrderedDict()
        for h in hits:
            groups.setdefault(h.key(), []).append(h)
        # the table (or source) reached the limit value_search_rows: its groups say so
        cut = len(set(repr(h.locator) for h in hits)) >= limits.get("value_search_rows")
        for key, hs in groups.items():
            g = self._group(key, hs, m)
            g.cut = cut
            if cut:
                g.why += "; stopped at the limit value_search_rows (there may be more)"
            self.add_group(g)
        self.status.configure(text="Searched %d of %d… %s" % (
            self._tables_done, self._tables_total, self.summary()))

    def _group(self, key, hits, member):
        source, table, column = key
        known = source == "DB" and (member.uid, table, column.lower()) in self.known
        whole = sum(1 for h in hits if h.kind == "whole")
        inside = len(hits) - whole
        parts = []
        if whole:
            parts.append("whole value" + ("" if whole == len(hits) else " (%d)" % whole))
        if inside:
            parts.append("inside a larger value" + ("" if inside == len(hits)
                                                    else " (%d)" % inside))
        hows = sorted(set(h.how for h in hits if h.how))
        why = "; ".join(parts) + (" — " + ", ".join(hows[:3]) if hows else "")
        link = "known link" if known else "same value found"
        if known and member is not self.member:
            link = "known link (matched by value)"
        if not known:
            why += "; coincidence possible"
        rows = [(h.locator if h.locator is not None else Locator("ordinal", i),
                 list(h.values), set(h.flags) if h.flags else set()) for i, h in enumerate(hits)]
        cols = hits[0].columns
        g = Group(source, table, column, cols, rows, len(hits), link, why,
                  1.0 if known else (0.5 if whole else 0.3), known,
                  self._entries(source, table, cols, rows, hits),
                  member if member is not self.member else None)
        g.entries = self._mark(g, g.entries)
        return g

    @staticmethod
    def _entries(source, table, cols, rows, hits):
        def make(i):
            h = hits[i]
            p = h.provenance
            if source == "WAL" and p.get("wal_record") is not None:
                return entry_from_wal_record(p["wal_record"], p.get("frames"))
            if source == "Freelist":
                return entry_from_freelist(table, p.get("page"), p.get("cell_offset"),
                                           list(cols), list(h.values), h.rowid,
                                           p.get("confidence"), h.flags)
            return entry_from_db_row(table, rows[i][0], list(cols), list(rows[i][1]),
                                     rows[i][2])
        return make

    def browse_value(self, g):
        return self.value

    def summary(self):
        n = sum(g.count for g in self.groups)
        known = sum(1 for g in self.groups if g.known)
        text = "%s in %d column%s" % (rows_text(n), len(self.groups),
                                      "" if len(self.groups) == 1 else "s")
        if known:
            text += " (%d known link%s)" % (known, "" if known == 1 else "s")
        return text

    def per_database(self, running=False):
        """'msgstore.db: 3 rows; wa.db: searched, nothing found' (a case only)."""
        if len(self.members) <= 1:
            return ""
        parts = []
        for m in self.members:
            k = sum(g.count for g in self.groups if self._gmember(g) is m)
            done = m in self._done_dbs
            if k:
                parts.append("%s: %s%s" % (m.name, rows_text(k),
                                           "" if done or not running else "…"))
            elif done:
                parts.append("%s: searched, nothing found" % m.name)
            else:
                parts.append("%s: %s" % (m.name, "searching…" if running
                                         else "not searched (stopped)"))
        return "; ".join(parts)

    def _finished(self, result, error):
        text = self.summary() + " (%.1f s)" % self.elapsed()
        if error is not None:
            text = "Failed: %s" % error
        elif not self.groups:
            text = "The value is nowhere else (%.1f s)" % self.elapsed()
        per = self.per_database()
        if per and error is None:
            text += " — " + per
        if result is False:
            text += " (stopped)"
        if self.errors:
            text += "; %d source(s) could not be searched: %s" % (len(self.errors),
                                                                   "; ".join(self.errors)[:200])
        self.finish(text)


class ColumnMapWindow(_Window):
    """Every column related to one column: the trusted links, and the weaker matches in their
    own collapsed section (shown only when there are some), each with its strength (Declared /
    Strong / Likely / Weak) and the reason; the selected link explained in full below; in a
    case also the columns of the other databases whose values match (always 'matched by
    value')."""

    COLUMNS = (("table", "Table", 170), ("column", "Column", 150), ("link", "Link", 110),
               ("kind", "Found by", 130), ("strength", "Strength", 130), ("why", "Why", 360))
    LEGEND = ("Strength: Declared — a FOREIGN KEY in the schema; Strong — values found for at "
              "least 95% of the sample; Likely — other checked links; Weak — too few values "
              "found, or a table without rows. Select a line for the whole explanation.")

    def __init__(self, manager, table, column, member=None):
        self.table, self.column = table, column
        member = member if member is not None else manager.active()
        label = column_label(member.db.session, table, column)
        where = member.label(table) if manager.multi() else table
        _Window.__init__(self, manager, "Column relationships: %s.%s" % (where, label), member)
        self.relations = []
        self.cross = []             # [(CrossLink, other member, table, column)] incl. weak
        self._iids = {}             # id(Relation or CrossLink) -> (tree, iid)
        self.weak_node = None       # (kept for callers: the weaker section is its own list)
        self._weak_open = False
        self._build(where, label)
        self.size_to(980, 420)
        self.start()

    def _build(self, where, label):
        # the buttons first, at the bottom, right-aligned: never squashed by the lists
        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=6, side="bottom")
        self.buttons = {}
        for key, text, tip in (("close", "Close", "Close this window"),
                               ("stop", "Stop", "Stop checking the links"),
                               ("diagram", "Show in diagram",
                                "Show the selected line's table in the Relationships diagram"),
                               ("related", "Column relationships of target…",
                                "Every column related to the selected line's column"),
                               ("browse", "Browse target",
                                "Open the selected line's table in Browse")):
            b = ttk.Button(bot, text=text, command=lambda k=key: self._action(k))
            b.pack(side="right", padx=3)
            ToolTip(b, tip)
            self.buttons[key] = b
        self.why = tk.Text(self, height=6, wrap="word", font=F["body"], bg=C["bg2"],
                           fg=C["text"], relief="flat", padx=6, pady=4, state="disabled")
        self.why.pack(fill="x", padx=8, pady=(2, 2), side="bottom")
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="%s.%s" % (where, label), style="B.TLabel").pack(side="left")
        self.status = ttk.Label(self, text="", style="M.TLabel", anchor="w")
        self.status.pack(fill="x", padx=10, pady=(0, 2))
        legend = ttk.Label(self, text=self.LEGEND, style="M.TLabel", wraplength=900,
                           justify="left")
        legend.pack(fill="x", padx=10, pady=(0, 4))
        legend.bind("<Configure>", lambda e: legend.configure(wraplength=max(200, e.width - 8)))
        self.search = SearchBox(self, placeholder="Find a table or column…",
                                on_change=lambda t: self.refresh(),
                                on_next=lambda f: _next_line(self.tree, f), width=28)
        self.search.pack(fill="x", padx=8, pady=(0, 2))
        self.lists = ttk.Frame(self)
        self.lists.pack(fill="both", expand=True, padx=8, pady=2)
        box, self.tree = self._tree(self.lists, self.COLUMNS, height=6)
        box.pack(fill="both", expand=True)
        self.weak_btn = ttk.Button(self.lists, text="", command=self.toggle_weaker)
        ToolTip(self.weak_btn, "Names or values only partly agree: not offered in menus")
        wbox, self.weak_tree = self._tree(self.lists, self.COLUMNS, height=5)
        self._weak_box = wbox
        for tree in (self.tree, self.weak_tree):
            tree.bind("<<TreeviewSelect>>", lambda e, t=tree: self._picked(t))
            tree.bind("<Double-1>", lambda e: self._browse_selected())
            tree.bind("<Button-3>", self._tree_menu)

    def toggle_weaker(self):
        self._weak_open = not self._weak_open
        self._show_weaker()

    def _show_weaker(self):
        n = len(self.weak_tree.get_children())
        total = len(self.weaker()) + sum(1 for x in self.cross if not x[0].confident)
        if not total:
            self.weak_btn.pack_forget()
            self._weak_box.pack_forget()
            return
        self.weak_btn.configure(text="%s Weaker matches (%d%s)" % (
            "▾" if self._weak_open else "▸", total,
            "" if n == total else ", %d shown" % n))
        if not self.weak_btn.winfo_manager():
            self.weak_btn.pack(anchor="w", pady=(4, 0))
        if self._weak_open:
            if not self._weak_box.winfo_manager():
                self._weak_box.pack(fill="both", expand=True, after=self.weak_btn)
        else:
            self._weak_box.pack_forget()

    def weaker_shown(self):
        return bool(self._weak_box.winfo_manager())

    def _picked(self, tree):
        other = self.weak_tree if tree is self.tree else self.tree
        if tree.selection() and other.selection():
            other.selection_remove(*other.selection())
        self._show_why()

    def _action(self, key):
        if key == "close":
            self.close()
        elif key == "stop":
            self.stop()
        elif key == "browse":
            self._browse_selected()
        elif key == "related":
            target = self._target()
            if target is not None:
                m, t, c = target
                self.manager.column_map(t, c, m)
        elif key == "diagram":
            target = self._target()
            m, t = (target[0], target[1]) if target is not None else (self.member, self.table)
            tab = getattr(self.app, "_relations_tab", None)
            if tab is not None:
                node = (m.name if self.manager.multi() else "main", t)
                self.app._nb.select(tab)
                tab.show_view("diagram")
                tab.set_focus(node if node in tab.graph.nodes else tab.focus)

    def _target(self):
        """(member, table, column) of the selected line's other end."""
        rel = self.selected()
        if rel is not None:
            return self.member, rel.other, rel.other_column
        x = self.selected_cross()
        if x is not None:
            return x[1], x[2], x[3]
        return None

    def start(self):
        table, column, session = self.table, self.column, self.session
        member = self.member
        cross = []
        for l in self.manager.cross_links(confident=False):
            if column is not ROWID and l.touches(member.uid, table, column):
                o_uid, o_table, o_col = l.other_end(member.uid, table, column)
                other = self.manager.member(o_uid)
                if other is not None:
                    cross.append((l, other, o_table, o_col))
        self.cross = cross

        def work(cancel, _emit):
            m = relation_map(session)
            rels = m.for_column(table, column)
            for rel in rels:
                if not m.verify(rel, cancel=cancel):
                    return rels, False
            return rels, True

        def done(result, error):
            stop = self.buttons.get("stop")
            if stop is not None:
                try:
                    stop.pack_forget()      # the check ended: no Stop that looks busy
                except tk.TclError:
                    pass
            if error is not None:
                self.status.configure(text="Failed: %s" % error)
                return
            self.relations, complete = result
            self.refresh()
            self.status.configure(text=self._summary() + ("" if complete else " (stopped)"))
        self.status.configure(text="Checking the links against the values…")
        self.run("map", work, done)

    def strong(self):
        return [r for r in self.relations if is_confident(r)]

    def weaker(self):
        return [r for r in self.relations if not is_confident(r)]

    def _summary(self):
        strong, weak = self.strong(), self.weaker()
        weak_cross = [c for c in self.cross if not c[0].confident]
        cross = [c for c in self.cross if c[0].confident]
        if not self.relations and not self.cross:
            return "No related columns: no declared key, name or shared column name links " \
                   "this column to another table"
        text = "%d trusted link%s" % (len(strong), "" if len(strong) == 1 else "s")
        n_weak = len(weak) + len(weak_cross)
        if n_weak:
            text += "; %d weaker match%s (below, folded)" % (n_weak,
                                                              "" if n_weak == 1 else "es")
        if self.manager.multi():
            text += "; %d link%s to other databases (matched by value)" % (
                len(cross), "" if len(cross) == 1 else "s")
        return text + " (%.1f s)" % self.elapsed()

    def _map_link(self, rel):
        mp = self.manager.map_of(self.member)
        return MapLink(rel, rows=mp.known_rows if mp is not None else None)

    def refresh(self):
        term = self.search.get().lower()

        def hit(values):
            return not term or any(term in str(v).lower() for v in values)
        self._iids = {}
        n = total = 0
        for tree in (self.tree, self.weak_tree):
            tree.delete(*tree.get_children())
        for rel in sorted(self.strong(), key=lambda r: (-r.score, r.other)):
            vals = self._values(rel)
            total += 1
            if hit(vals):
                n += 1
                self._iids[id(rel)] = (self.tree, self.tree.insert("", "end", values=vals,
                                                                   tags=("known",)))
        for link, other, t, c in sorted(self.cross, key=lambda x: -x[0].fraction):
            if link.confident:
                vals = self._cross_values(link, other, t, c)
                total += 1
                if hit(vals):
                    n += 1
                    self._iids[id(link)] = (self.tree, self.tree.insert(
                        "", "end", tags=("cross",), values=vals))
        for rel in sorted(self.weaker(), key=lambda r: (-r.score, r.other)):
            vals = self._values(rel)
            total += 1
            if hit(vals):
                n += 1
                self._iids[id(rel)] = (self.weak_tree, self.weak_tree.insert(
                    "", "end", values=vals, tags=("weak",)))
        for link, other, t, c in [x for x in self.cross if not x[0].confident]:
            vals = self._cross_values(link, other, t, c)
            total += 1
            if hit(vals):
                n += 1
                self._iids[id(link)] = (self.weak_tree, self.weak_tree.insert(
                    "", "end", tags=("weak",), values=vals))
        if term and self.weak_tree.get_children():
            self._weak_open = True
        self._show_weaker()
        self.search.set_count(n, total, "link", "links", "table or column")
        self._fit_columns()

    def _fit_columns(self):
        """Columns as wide as their longest text (Why up to a limit: its whole text is in
        the tooltip and below the list)."""
        import tkinter.font as tkfont
        try:
            font = tkfont.nametofont("TkDefaultFont")
        except tk.TclError:
            return
        for tree in (self.tree, self.weak_tree):
            rows = [tree.item(i, "values") for i in tree.get_children()]
            for k, (c, title, w) in enumerate(self.COLUMNS):
                need = max([font.measure(title) + 24] +
                           [font.measure(str(r[k])) + 16 for r in rows if len(r) > k])
                tree.column(c, width=min(need, 520 if c == "why" else 320))

    def _values(self, rel):
        ml = self._map_link(rel)
        ev = ml.evidence()
        return (rel.other, column_label(self.session, rel.other, rel.other_column),
                LINKS[rel.direction], KINDS.get(rel.kind, rel.kind),
                ml.strength() + (" · " + ev if ev else ""),
                ml.reason or plain_reason(rel) or "; ".join(rel.why()))

    def _cross_values(self, link, other, table, column):
        out = (link.src_db, link.src_table) == (self.member.uid, self.table)
        ov = link.overlap
        ev = ("%d%% of %d sampled" % (round(100 * ov.fraction), ov.sampled)
              if ov is not None and ov.sampled else "")
        return (other.label(table), column, LINKS["out" if out else "in"],
                KINDS["value"], ("Strong" if link.confident and link.fraction >= 0.95 else
                                 "Likely" if link.confident else "Weak") +
                (" · " + ev if ev else ""), link.reason())

    def _selection(self):
        for tree in (self.tree, self.weak_tree):
            sel = tree.selection()
            if sel:
                return tree, sel[0]
        return None

    def selected(self):
        sel = self._selection()
        if sel is None:
            return None
        for rel in self.relations:
            if self._iids.get(id(rel)) == sel:
                return rel
        return None

    def selected_cross(self):
        sel = self._selection()
        if sel is None:
            return None
        for x in self.cross:
            if self._iids.get(id(x[0])) == sel:
                return x
        return None

    def explain(self, rel):
        """The selected link in full, one fact per line: the columns, the strength, whether it
        is declared, the rule that found it, what the value check sampled and found."""
        ml = self._map_link(rel)
        lines = ["%s.%s %s %s.%s" % (rel.table, column_label(self.session, rel.table,
                                                             rel.column), rel.arrow(),
                                     rel.other, column_label(self.session, rel.other,
                                                             rel.other_column)),
                 "Strength: %s%s (score %.2f)" % (ml.strength(), ", values found: " +
                                                  ml.evidence() if ml.evidence() else "",
                                                  rel.score),
                 "Declared FOREIGN KEY: %s" % ("yes" if rel.kind == "fk" or any(
                     l.kind == "fk" for l in rel.links) else "no"),
                 "Found by: %s — %s" % (KINDS.get(rel.kind, rel.kind),
                                        "; ".join(rel.reasons) or "(no other reason)")]
        for l in rel.links:
            ov = l.overlap
            if ov is None:
                lines.append("Values: not checked")
                continue
            lines.append("Values: %s%s" % (ov.text(), "; every distinct value of the column "
                                                      "was sampled" if ov.exhausted else ""))
            if ov.sampled:
                lines.append("  %d distinct value%s sampled, %d found, %d not found" % (
                    ov.sampled, "" if ov.sampled == 1 else "s", ov.found,
                    ov.sampled - ov.found))
        if not ml.confident and rel.kind != "fk":
            lines.append("Why weaker: %s" % (ml.reason or plain_reason(rel)))
        return "\n".join(lines)

    def _show_why(self):
        rel = self.selected()
        x = self.selected_cross() if rel is None else None
        self.why.configure(state="normal")
        self.why.delete("1.0", "end")
        if rel is not None:
            self.why.insert("1.0", self.explain(rel))
        elif x is not None:
            l, other, t, c = x
            ov = l.overlap
            self.why.insert("1.0", "\n".join([
                "%s.%s ↔ %s.%s" % (self.where(self.table), self.column, other.label(t), c),
                "Found by: matched by value (between databases)",
                "Values: %s" % l.reason(),
                "  %d distinct values sampled, %d found" % (ov.sampled, ov.found)
                if ov is not None and ov.sampled else "  no values checked"]))
        self.why.configure(state="disabled")

    def _browse_in(self, member, table):
        activate = getattr(self.app, "activate_member", None)
        if activate is not None and member.uid is not None:
            activate(member)
        self.app.browse_table(table)

    def _browse_selected(self):
        rel = self.selected()
        if rel is not None:
            self._browse_in(self.member, rel.other)
            return
        x = self.selected_cross()
        if x is not None:
            self._browse_in(x[1], x[2])

    def _tree_menu(self, event):
        tree = event.widget
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        self._picked(tree)
        rel = self.selected()
        x = self.selected_cross() if rel is None else None
        if rel is None and x is None:
            return
        menu = tk.Menu(self, tearoff=0)
        if x is not None:
            _l, other, t, c = x
            menu.add_command(label="Open %s in Browse" % other.label(t),
                             command=lambda: self._browse_in(other, t))
            menu.add_command(label="Column relationships of %s.%s…" % (other.label(t), c),
                             command=lambda: self.manager.column_map(t, c, other))
            self._popup(menu, event)
            return
        menu.add_command(label="Open %s in Browse" % rel.other,
                         command=lambda: self._browse_in(self.member, rel.other))
        menu.add_command(label="Column relationships of %s.%s…" % (
            rel.other, column_label(self.session, rel.other, rel.other_column)),
            command=lambda: self.manager.column_map(rel.other, rel.other_column, self.member))
        self._popup(menu, event)
