"""The Case navigator: the left panel listing the databases of the case (or the one database
open), grouped by the app or folder they come from, with their tables and columns.

NameIndex        every database, table and column name of the case, for the navigator's
                 search box and the command palette (no Tk; a search over 60 databases takes a
                 few milliseconds).
group_key        the app or folder a database belongs to ('com.whatsapp' for
                 .../com.whatsapp/databases/msgstore.db).
case_name        a short name for the case (the app or folder the databases share).
CaseNavigator    the panel: a search box (databases, tables, columns), filter chips (has rows,
                 WAL, dates, hits, warnings), a sort menu, the tree (Pinned, then one group per
                 app or folder: databases with a colour dot, status, table and row counts;
                 expand a database for its tables, a table for its columns), a hover card per
                 line, a right-click menu, multi-selection to set a scope, and the CREATE
                 statement of the selected table folded away at the bottom.
"""

import os
import re
import sys
import time
import tkinter as tk
from tkinter import ttk

from constants import mode_label
from tokens import COLOR as K, FONT as F, XS, S, M
from utils import fmtb, _int_count
from widgets import ElideLabel, SearchBox, ToolTip, menu_button
from parts import ChipBar, Expander, HoverCard, dot_image

GENERIC = frozenset(("databases", "database", "db", "dbs", "default", "app_chrome",
                     "app_webview", "files", "data", "no_backup", "cache", "shared_prefs",
                     "app_databases", "user_de", "0", "sqlite"))
_PACKAGE_RE = re.compile(r"^[A-Za-z][\w-]*(\.[\w-]+)+$")


def short_count(n):
    """1234 -> '1.2k', 2460000 -> '2.5M' (for narrow columns; the hover card has the exact
    number)."""
    if n is None:
        return ""
    n = int(n)
    for div, unit in ((10 ** 9, "G"), (10 ** 6, "M"), (10 ** 3, "k")):
        if abs(n) >= div:
            v = n / float(div)
            return ("%.1f%s" % (v, unit)) if v < 100 else ("%d%s" % (v, unit))
    return str(n)


def group_key(path):
    """The app or folder of a database: the nearest folder named like a package
    ('com.whatsapp'), else the nearest folder that is not a generic name ('databases')."""
    parts = []
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(6):
        name = os.path.basename(d)
        if not name:
            break
        parts.append(name)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for name in parts:
        if _PACKAGE_RE.match(name) and name.lower() not in GENERIC:
            return name
    for name in parts:
        if name.lower() not in GENERIC:
            return name
    return parts[0] if parts else "?"


def case_name(paths):
    """A short name for databases opened together: the app they share, else their common
    folder (without generic names such as 'databases')."""
    paths = list(paths)
    if not paths:
        return ""
    groups = set(group_key(p) for p in paths)
    if len(groups) == 1:
        return groups.pop()
    try:
        common = os.path.commonpath([os.path.abspath(p) for p in paths])
    except ValueError:              # different drives
        return "%d folders" % len(groups)
    d = common
    for _ in range(6):
        name = os.path.basename(d)
        if name and name.lower() not in GENERIC:
            return name
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.basename(common) or common


def member_rows(m):
    """(rows, exact) of a database: the sum of its tables' counts; exact is False while
    some are estimates or unknown."""
    total, exact = 0, True
    for v in m.counts.values():
        if isinstance(v, int):
            total += v
        else:
            exact = False
            total += _int_count(v)
    return total, exact


def member_warnings(m):
    """The warnings and errors of a database's status (its header banners); the answer is
    reused for two seconds (the lists of a 60-database case ask for it thousands of times,
    and a banner looks at the files next to the database)."""
    now = time.monotonic()
    cached = getattr(m, "_warn_cache", None)
    if cached is not None and now - cached[0] < 2.0 and cached[2] is m.db.session:
        return cached[1]
    try:
        out = [b for b in m.db.banners() if b.level in ("warning", "error")]
    except Exception:               # noqa: BLE001 - a closed database
        out = []
    try:
        m._warn_cache = (now, out, m.db.session)
    except AttributeError:
        pass
    return out


def status_glyph(m):
    """Short status text of a database for a narrow column: 'WAL' when its WAL is merged,
    'WAL!' when SQL cannot see it, 'nat' when read natively, '' when immutable; '⚠' is added
    for a warning."""
    mode = m.db.mode if m.db.ok else ""
    s = {"ram-overlay": "WAL", "main-only": "WAL!", "native": "nat"}.get(mode, "")
    if member_warnings(m):
        s = (s + " ⚠").strip()
    return s


class Entry(object):
    """One name of the index."""
    __slots__ = ("kind", "member", "table", "column", "label", "text")

    def __init__(self, kind, member, table=None, column=None, label=""):
        self.kind, self.member, self.table, self.column = kind, member, table, column
        self.label = label
        self.text = label.lower()

    def __repr__(self):
        return "Entry(%s, %s)" % (self.kind, self.label)


class NameIndex(object):
    """Every database, table (and view) and column name of the databases given."""

    def __init__(self, members, columns=True):
        self.entries = []
        self.seconds = 0.0
        t0 = time.perf_counter()
        for m in members:
            if not m.db.ok:
                continue
            self.entries.append(Entry("database", m, label=m.name))
            names = list(m.db.tables()) + list(m.db.views())
            for t in names:
                self.entries.append(Entry("table", m, t, label=t))
                if not columns:
                    continue
                try:
                    cols = m.db.columns(t)
                except Exception:       # noqa: BLE001 - a table SQLite cannot describe
                    cols = []
                for c in cols:
                    name = c[0] if isinstance(c, (tuple, list)) else c
                    self.entries.append(Entry("column", m, t, name, label=str(name)))
        self.seconds = time.perf_counter() - t0

    def __len__(self):
        return len(self.entries)

    def find(self, text, kinds=None):
        """Entries whose name holds every word of text (any case), in index order."""
        words = str(text or "").lower().split()
        if not words:
            return []
        out = []
        for e in self.entries:
            if kinds is not None and e.kind not in kinds:
                continue
            t = e.text
            ok = True
            for w in words:
                if w not in t:
                    ok = False
                    break
            if ok:
                out.append(e)
        return out


SORTS = (("name", "Name"), ("size", "Size"), ("rows", "Rows"), ("hits", "Search hits"),
         ("recent", "Recently used"))
PLACEHOLDER = "loading"


class CaseNavigator(ttk.Frame):
    """The left panel of the main window (see the module docstring). app gives the case and
    the actions (activate_member, browse_member_table, remove_member, ...)."""

    def __init__(self, master, app):
        ttk.Frame.__init__(self, master, style="Panel.TFrame")
        self.app = app
        self.index = None
        self.hits = {}                  # uid -> search hits (after a search)
        self.dates = {}                 # uid -> date columns found (once known)
        self.pinned = set()             # paths
        self.last_used = {}             # uid -> time made active
        self.sort = "name"
        self._nodes = {}                # iid -> ('group'|'db'|'table'|'column'|..., member, name)
        self._db_iid = {}               # uid -> iid
        self._open_groups = set()
        self._closed_groups = set()
        self._filter_after = None
        self._counts_after = None
        self.last_filter_ms = 0.0
        self._build()

    # -- layout -------------------------------------------------------------------------------
    def _build(self):
        head = ttk.Frame(self, style="Panel.TFrame")
        head.pack(fill="x", padx=M, pady=(M, XS))
        # Sort ▾ is packed first so it keeps its width; the count shortens itself instead
        self.sort_btn, self.sort_menu = menu_button(head, "Sort ▾",
                                                  style="Subtle.TButton")
        self.sort_btn.pack(side="right")
        self.title = ttk.Label(head, text="Databases", style="Heading.TLabel")
        self.title.pack(side="left")
        self.count_lbl = ElideLabel(head, text="", style="Muted.TLabel")
        self.count_lbl.pack(side="left", padx=(S, 0), fill="x", expand=True)
        self.sort_var = tk.StringVar(master=self, value="name")
        for key, label in SORTS:
            self.sort_menu.add_radiobutton(label=label, value=key, variable=self.sort_var,
                                           command=self._sort_changed)
        self.sort_menu.add_separator()
        self.sort_menu.add_command(label="Expand all groups", command=lambda: self.expand_all(True))
        self.sort_menu.add_command(label="Collapse all groups",
                                   command=lambda: self.expand_all(False))
        self.sort_menu.add_separator()
        settings = getattr(getattr(self.app, "tags", None), "settings", None)
        self.hide_empty_var = tk.BooleanVar(
            master=self, value=bool(settings.get("nav_hide_empty")) if isinstance(
                settings, dict) else False)
        self.sort_menu.add_checkbutton(label="Hide empty tables (0 rows)",
                                       variable=self.hide_empty_var,
                                       command=self._hide_empty_changed)
        ToolTip(self.sort_btn, "Order the databases (within their groups) by name, size, "
                               "rows, search hits or when last used")

        self.search = SearchBox(
            self, placeholder="Find a database, table or column…", delay=120, width=18,
            find_button=False, count_below=True, on_change=lambda t: self.apply_filter(),
            on_next=lambda forward: self._next_match(forward),
            tooltip="Lists the databases, tables and columns whose name holds every word, "
                    "as you type; Enter selects the next match; Escape clears it.")
        self.search.pack(fill="x", padx=M, pady=(0, XS))

        self.chips = ChipBar(self, bg=K["background"], style="Panel.TFrame")
        self.chips.pack(fill="x", padx=M, pady=(0, XS))
        self._chip = {}
        for key, text, tip in (
                ("rows", "Has rows", "Only databases with rows"),
                ("wal", "WAL", "Only databases whose WAL file was merged (or could not be)"),
                ("dates", "Has dates", "Only databases with date columns (known once the "
                                       "Overview or Timeline looked for them)"),
                ("hits", "Has hits", "Only databases where the last search found something"),
                ("warn", "Warnings", "Only databases with a warning (hot journal, WAL SQL "
                                     "cannot see, read natively...)")):
            self._chip[key] = self.chips.add_chip(text, toggle=True, tooltip=tip,
                                                  on_change=lambda on: self.apply_filter())
        self.chips.show(self._chip["dates"], False)
        self.chips.show(self._chip["hits"], False)
        self.chips.show(self._chip["warn"], False)

        # the selection bar: shown while two or more databases are selected
        self.sel_bar = ttk.Frame(self, style="Panel.TFrame")
        self.sel_lbl = ttk.Label(self.sel_bar, text="", style="Muted.TLabel")
        self.sel_lbl.pack(side="left")
        ttk.Button(self.sel_bar, text="Clear", style="Link.TButton",
                   command=self.clear_selection).pack(side="right")
        self.scope_btn = ttk.Button(self.sel_bar, text="Use as scope", style="Small.TButton",
                                    command=self._scope_to_selection)
        self.scope_btn.pack(side="right", padx=(0, XS))
        ToolTip(self.scope_btn, "Search, Timeline, Relationships, Find everywhere and the "
                                "Database Map then cover only the selected databases")

        box = ttk.Frame(self, style="Panel.TFrame")
        box.pack(fill="both", expand=True, padx=(XS, 0))
        self._tree_box = box
        self.tree = ttk.Treeview(box, style="Nav.Treeview", columns=("st", "tables", "rows"),
                                 show="tree", selectmode="extended")
        self.tree.column("#0", width=150, minwidth=110, stretch=True)
        self.tree.column("st", width=40, minwidth=30, stretch=False, anchor="center")
        self.tree.column("tables", width=34, minwidth=28, stretch=False, anchor="e")
        self.tree.column("rows", width=52, minwidth=40, stretch=False, anchor="e")
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=xsb.set)
        xsb.pack(side="bottom", fill="x")
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        ToolTip(self.tree,
                "Each database shows: status \u00b7 number of tables \u00b7 total rows.\n"
                "Each table shows its row count; hover a table for its full name.")
        self.tree.tag_configure("group", foreground=K["muted_text"], font=F["small_bold"])
        self.tree.tag_configure("active", font=F["body_bold"], foreground=K["heading"])
        self.tree.tag_configure("muted", foreground=K["muted_text"])
        self.tree.tag_configure("match", foreground=K["heading"])
        self.tree.tag_configure("colmatch", foreground=K["primary"])
        self.tree.tag_configure("section", foreground=K["muted_text"], font=F["small"])
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewClose>>", self._on_close)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<ButtonRelease-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", self._on_return)
        self.tree.bind("<Button-3>", self._on_menu)
        self.tree.bind("<Shift-F10>", self._on_menu_key)
        if sys.platform == "win32":          # the Menu/Application key: no such keysym on X11
            self.tree.bind("<App>", self._on_menu_key)
        HoverCard(self.tree, self._hover_text)

        self.schema = Expander(self, "CREATE statement", style="Muted.TLabel")
        self.schema.pack(fill="x", padx=M, pady=(XS, M), side="bottom")
        self.sql_text = tk.Text(self.schema.body, height=6, font=F["mono_small"], wrap="word",
                                background=K["card"], foreground=K["text"], relief="flat",
                                highlightthickness=1, highlightbackground=K["border"],
                                state="disabled")
        self.sql_text.pack(fill="x")
        btns = ttk.Frame(self.schema.body)
        btns.pack(fill="x", pady=(XS, 0))
        ttk.Button(btns, text="Copy CREATE", style="Small.TButton",
                   command=self.copy_sql).pack(side="left")
        ttk.Button(btns, text="Copy schema", style="Small.TButton",
                   command=self.copy_schema).pack(side="left", padx=XS)
        self.note = ttk.Label(btns, text="", style="Success.TLabel")
        self.note.pack(side="left", padx=XS)
        self._note_after = None
        self.empty = ttk.Label(box, text="Open a database or a folder:\nits databases, "
                                         "tables and columns are listed here.",
                               style="Muted.TLabel", justify="center")

    # -- the case ---------------------------------------------------------------------------
    def members(self):
        case = getattr(self.app, "case", None)
        return [m for m in case] if case is not None else []

    def rebuild(self):
        """The case changed (databases joined or left, the active one changed): list it
        again, keeping the groups the user opened or closed and the search."""
        self.index = NameIndex(self.members())
        ms = self.members()
        self.count_lbl.configure(text=("%d" % len(ms)) if len(ms) > 1 else "")
        self.title.configure(text="Databases" if len(ms) > 1 else "Database")
        self._update_chips()
        if ms:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.3, anchor="center")
        self.apply_filter()

    def _update_chips(self):
        ms = self.members()
        multi = len(ms) > 1
        self.chips.show(self._chip["rows"], multi)
        self.chips.show(self._chip["wal"], multi and any(m.db.ok and m.db.has_wal for m in ms))
        self.chips.show(self._chip["dates"], multi and bool(self.dates))
        self.chips.show(self._chip["hits"], multi and bool(self.hits))
        self.chips.show(self._chip["warn"], multi and any(member_warnings(m) for m in ms))
        for key, chip in self._chip.items():
            if not self.chips.shown(chip) and chip.on:
                chip.set(on=False)
        if self.hits:
            self._chip["hits"].set(count=sum(1 for v in self.hits.values() if v))
        if self.dates:
            self._chip["dates"].set(count=sum(1 for v in self.dates.values() if v))

    def set_hits(self, hits):
        """{uid: hits} of the last search (empty: no search)."""
        self.hits = dict(hits or {})
        self._update_chips()
        if self.sort == "hits" or self._chip["hits"].on:
            self.apply_filter()
        else:
            self.refresh_counts()

    def set_dates(self, dates):
        """{uid: date columns found} once known."""
        self.dates = dict(dates or {})
        self._update_chips()

    def chip_on(self, key):
        chip = self._chip.get(key)
        return chip is not None and chip.on and self.chips.shown(chip)

    def set_chip(self, key, on):
        self._chip[key].set(on=on)
        self.apply_filter()

    # -- what is listed ---------------------------------------------------------------------
    def _passes(self, m):
        if not m.db.ok:
            return False
        if self.chip_on("rows") and member_rows(m)[0] <= 0:
            return False
        if self.chip_on("wal") and not m.db.has_wal:
            return False
        if self.chip_on("dates") and not self.dates.get(m.uid):
            return False
        if self.chip_on("hits") and not self.hits.get(m.uid):
            return False
        if self.chip_on("warn") and not member_warnings(m):
            return False
        return True

    def _sort_key(self, m):
        s = self.sort
        if s == "size":
            return (-(m.size or 0), m.name.lower())
        if s == "rows":
            return (-member_rows(m)[0], m.name.lower())
        if s == "hits":
            return (-(self.hits.get(m.uid) or 0), m.name.lower())
        if s == "recent":
            return (-self.last_used.get(m.uid, 0), m.name.lower())
        return (m.name.lower(),)

    def _sort_changed(self):
        self.sort = self.sort_var.get()
        self.apply_filter()

    def groups(self, members=None):
        """[(group name, [members])] in display order: Pinned first, then the groups by
        name (or by the chosen order of their best member)."""
        members = self.members() if members is None else members
        pinned = [m for m in members if m.path in self.pinned]
        rest = [m for m in members if m.path not in self.pinned]
        by = {}
        for m in rest:
            by.setdefault(group_key(m.path), []).append(m)
        for ms in by.values():
            ms.sort(key=self._sort_key)
        if self.sort == "name":
            order = sorted(by, key=lambda g: g.lower())
        else:
            order = sorted(by, key=lambda g: (self._sort_key(by[g][0]), g.lower()))
        out = []
        if pinned:
            out.append(("Pinned", sorted(pinned, key=self._sort_key)))
        out.extend((g, by[g]) for g in order)
        return out

    def apply_filter(self):
        """List the case for the search text and the chips (a few ms for 60 databases)."""
        t0 = time.perf_counter()
        tree = self.tree
        sel = set(self.selected_members(uids=True))
        focus_uid = None
        f = tree.focus()
        if f and f in self._nodes and self._nodes[f][1] is not None:
            focus_uid = self._nodes[f][1].uid
        tree.delete(*tree.get_children())
        self._nodes, self._db_iid = {}, {}
        members = [m for m in self.members() if self._passes(m)]
        text = self.search.get()
        matches = None
        if text and self.index is not None:
            matches = {}
            for e in self.index.find(text):
                matches.setdefault(e.member.uid, []).append(e)
            members = [m for m in members if m.uid in matches]
        multi = len(self.members()) > 1
        groups = self.groups(members)
        show_groups = multi and (len(groups) > 1 or (groups and groups[0][0] == "Pinned"))
        active = getattr(self.app.case, "active", None) if hasattr(self.app, "case") else None
        n_tables = n_cols = 0
        for gname, ms in groups:
            parent = ""
            if show_groups:
                gid = tree.insert("", "end", text="%s  (%d)" % (gname, len(ms)),
                                  values=("", "", ""), tags=("group",),
                                  open=self._group_open(gname, ms, active, matches))
                self._nodes[gid] = ("group", None, gname)
                parent = gid
            for m in ms:
                iid = self._insert_db(parent, m, m is active)
                if m.uid in sel:
                    tree.selection_add(iid)
                if matches is not None:
                    tn, cn = self._fill_matches(iid, m, matches.get(m.uid, []))
                    n_tables += tn
                    n_cols += cn
                elif multi and m is active and not getattr(self, "_active_closed", False):
                    tree.item(iid, open=True)
                    self._fill_tables(iid, m)
                elif not multi:
                    tree.item(iid, open=True)
                    self._fill_tables(iid, m)
        if focus_uid is not None and focus_uid in self._db_iid:
            tree.focus(self._db_iid[focus_uid])
        if matches is not None:
            nd = len(members)
            if nd or n_tables or n_cols:
                parts = ["%d database%s" % (nd, "" if nd == 1 else "s")]
                if n_tables:
                    parts.append("%d table%s" % (n_tables, "" if n_tables == 1 else "s"))
                if n_cols:
                    parts.append("%d column%s" % (n_cols, "" if n_cols == 1 else "s"))
                self.search.set_status(" · ".join(parts))
            else:
                self.search.set_status("No database, table or column matches “%s”" % text,
                                       error=True)
        else:
            hidden = len(self.members()) - len(members)
            self.search.set_status("%d of %d databases shown (filters)" % (
                len(members), len(self.members())) if hidden else "")
        self._update_sel_bar()
        self.last_filter_ms = (time.perf_counter() - t0) * 1000.0

    def _group_open(self, gname, ms, active, matches):
        if matches is not None or gname == "Pinned":
            return True
        if gname in self._open_groups:
            return True
        if gname in self._closed_groups:
            return False
        if len(self.members()) <= 20:
            return True
        return active in ms

    def _insert_db(self, parent, m, active):
        tree = self.tree
        rows, exact = member_rows(m)
        img = dot_image(tree, m.color, 10, ring=K["heading"] if active else None)
        iid = tree.insert(parent, "end", text=" " + m.name, image=img,
                          values=(status_glyph(m), len(m.db.tables()),
                                  ("" if exact else "~") + short_count(rows)),
                          tags=("db",) + (("active",) if active else ()))
        self._nodes[iid] = ("db", m, m.name)
        self._db_iid[m.uid] = iid
        tree.insert(iid, "end", text=PLACEHOLDER)
        return iid

    def _fill_tables(self, iid, m):
        """A database's tables (with row counts), then views and WAL-only tables."""
        tree = self.tree
        for c in tree.get_children(iid):
            if tree.item(c, "text") == PLACEHOLDER:
                tree.delete(c)
        if tree.get_children(iid):
            return
        hidden = 0
        for t in m.db.tables():
            if self.hide_empty_var.get() and self._is_empty(m, t):
                hidden += 1
                continue
            self._insert_table(iid, m, t)
        if hidden:
            # never silent: the line says how many are hidden, a click lists them again
            x = tree.insert(iid, "end", text="%d empty table%s hidden — show" % (
                hidden, "" if hidden == 1 else "s"), tags=("section",))
            self._nodes[x] = ("empty_hidden", m, hidden)
        views = m.db.views()
        if views:
            vid = tree.insert(iid, "end", text="Views (%d)" % len(views), tags=("section",))
            self._nodes[vid] = ("section", m, "views")
            for v in views:
                x = tree.insert(vid, "end", text=v, values=("view", "", ""))
                self._nodes[x] = ("view", m, v)
        try:
            triggers = m.db.triggers()
        except Exception:               # noqa: BLE001
            triggers = []
        if triggers:
            tid = tree.insert(iid, "end", text="Triggers (%d)" % len(triggers),
                              tags=("section",))
            self._nodes[tid] = ("section", m, "triggers")
            for trg in triggers:
                x = tree.insert(tid, "end", text=trg, tags=("muted",))
                self._nodes[x] = ("trigger", m, trg)
        if m.db.has_wal:
            try:
                wal_only = m.db.wal_tables()
            except Exception:           # noqa: BLE001
                wal_only = []
            if wal_only:
                wid = tree.insert(iid, "end", text="WAL-only tables (%d)" % len(wal_only),
                                  tags=("section",))
                self._nodes[wid] = ("section", m, "wal")
                for wt in wal_only:
                    x = tree.insert(wid, "end", text=wt, values=("WAL", "", ""))
                    self._nodes[x] = ("wal_table", m, "WAL: " + wt)
        self._autosize_tree_column(iid)

    @staticmethod
    def _is_empty(m, t):
        """A table known to hold no rows (an exact count of 0, or an estimate of 0)."""
        c = m.counts.get(t)
        return c == 0 or (isinstance(c, str) and c.strip() in ("~0", "0"))

    def _hide_empty_changed(self):
        """Hide empty tables was ticked or unticked: kept in the settings, the lists filled
        again."""
        settings = getattr(getattr(self.app, "tags", None), "settings", None)
        if isinstance(settings, dict):
            settings["nav_hide_empty"] = bool(self.hide_empty_var.get())
            try:
                self.app.tags.save_settings()
            except Exception:           # noqa: BLE001 - a setting not kept is not fatal
                pass
        self._refill_open_databases()

    def _refill_open_databases(self):
        """List the tables of every expanded database again (the hide-empty choice
        changed)."""
        tree = self.tree
        for iid, (kind, m, _name) in list(self._nodes.items()):
            if kind != "db" or not tree.exists(iid) or not tree.item(iid, "open"):
                continue
            for c in tree.get_children(iid):
                self._forget_subtree(c)
                tree.delete(c)
            self._fill_tables(iid, m)

    def _forget_subtree(self, iid):
        for c in self.tree.get_children(iid):
            self._forget_subtree(c)
        self._nodes.pop(iid, None)

    def _autosize_tree_column(self, iid, cap=420):
        """Widen the name column to fit the longest name listed under iid (up to `cap`
        px; the horizontal scroll bar shows the rest), so table and column names are not
        cut at the pane edge."""
        try:
            import tkinter.font as tkfont
            font = tkfont.nametofont("TkDefaultFont")
            depth, p = 0, iid
            while p:
                depth += 1
                p = self.tree.parent(p)
            max_w = 0
            for c in self.tree.get_children(iid):
                txt = self.tree.item(c, "text") or ""
                max_w = max(max_w, font.measure(txt))
            # indent per level + the open/close indicator + padding
            need = min(max_w + 20 * (depth + 1) + 24, cap)
            if need > self.tree.column("#0", "width"):
                self.tree.column("#0", width=int(need))
        except (tk.TclError, RuntimeError):
            pass

    def _insert_table(self, parent, m, t, tags=(), open_=False):
        c = m.counts.get(t, "?")
        n = c if isinstance(c, int) else None
        text = short_count(n) if n is not None else ("~" + short_count(_int_count(c))
                                                     if isinstance(c, str) and c.startswith("~")
                                                     else "…")
        x = self.tree.insert(parent, "end", text=t, values=("", "", text), tags=tags,
                             open=open_)
        self._nodes[x] = ("table", m, t)
        if not open_:
            self.tree.insert(x, "end", text=PLACEHOLDER)
        return x

    def _fill_matches(self, iid, m, entries):
        """Under a database: the tables that match or hold a matching column (the columns
        listed under them)."""
        tree = self.tree
        for c in tree.get_children(iid):
            tree.delete(c)
        tables, cols = [], {}
        for e in entries:
            if e.kind == "table":
                if e.table not in tables:
                    tables.append(e.table)
            elif e.kind == "column":
                if e.table not in tables and e.table not in cols:
                    tables.append(e.table)
                cols.setdefault(e.table, []).append(e.column)
        for t in tables:
            x = self._insert_table(iid, m, t, tags=("match",), open_=bool(cols.get(t)))
            for c in cols.get(t, []):
                y = tree.insert(x, "end", text="  " + c, tags=("colmatch",))
                self._nodes[y] = ("column", m, (t, c))
        tree.item(iid, open=bool(tables))
        if not tables:
            tree.insert(iid, "end", text=PLACEHOLDER)
        return len([t for t in tables if t not in cols]), sum(len(v) for v in cols.values())

    def refresh_counts(self):
        """Row counts changed (the count worker): update the lines listed (throttled)."""
        if self._counts_after is not None:
            return
        try:
            self._counts_after = self.after(250, self._refresh_counts_now)
        except tk.TclError:
            self._counts_after = None

    def _refresh_counts_now(self):
        self._counts_after = None
        tree = self.tree
        for iid, (kind, m, name) in list(self._nodes.items()):
            if not tree.exists(iid) or m is None:
                continue
            if kind == "db":
                rows, exact = member_rows(m)
                tree.set(iid, "rows", ("" if exact else "~") + short_count(rows))
                tree.set(iid, "st", status_glyph(m))
            elif kind == "table":
                c = m.counts.get(name, "?")
                tree.set(iid, "rows", short_count(c) if isinstance(c, int) else (
                    "~" + short_count(_int_count(c)) if isinstance(c, str) and
                    c.startswith("~") else "…"))
        for gid, (kind, _m, gname) in list(self._nodes.items()):
            if kind == "group" and tree.exists(gid):
                tree.set(gid, "rows", "")

    def expand_all(self, open_):
        for iid, (kind, _m, name) in self._nodes.items():
            if kind == "group" and self.tree.exists(iid):
                self.tree.item(iid, open=open_)
                (self._open_groups if open_ else self._closed_groups).add(name)
                (self._closed_groups if open_ else self._open_groups).discard(name)

    # -- events ------------------------------------------------------------------------------
    def node(self, iid):
        return self._nodes.get(iid)

    def _on_open(self, _e=None):
        iid = self.tree.focus()
        n = self._nodes.get(iid)
        if n is None:
            return
        kind, m, name = n
        if kind == "group":
            self._open_groups.add(name)
            self._closed_groups.discard(name)
        elif kind == "db":
            if self.search.get():
                return
            self._fill_tables(iid, m)
        elif kind == "table":
            self._fill_columns(iid, m, name)

    def _on_close(self, _e=None):
        n = self._nodes.get(self.tree.focus())
        if n is not None and n[0] == "group":
            self._closed_groups.add(n[2])
            self._open_groups.discard(n[2])

    def _fill_columns(self, iid, m, table):
        tree = self.tree
        kids = tree.get_children(iid)
        if not (len(kids) == 1 and tree.item(kids[0], "text") == PLACEHOLDER):
            return
        tree.delete(kids[0])
        try:
            cols = m.db.columns(table)
        except Exception:               # noqa: BLE001
            cols = []
        for c in cols:
            cn, ct = (c[0], c[1]) if isinstance(c, (tuple, list)) and len(c) > 1 else (c, "")
            # the name and the full declared type on the line itself (the narrow side
            # columns cut a type to 'VARC'); the hover card adds the constraints
            text = "  %s   %s" % (cn, ct) if ct else "  " + str(cn)
            y = tree.insert(iid, "end", text=text, values=("", "", ""), tags=("muted",))
            self._nodes[y] = ("column", m, (table, cn))
        self._autosize_tree_column(iid)
        try:
            for fk in m.db.fkeys_full(table):
                actions = ["ON %s %s" % (k.split("_")[1].upper(), fk[k])
                           for k in ("on_delete", "on_update") if fk.get(k)]
                y = tree.insert(iid, "end", text="  FK %s → %s(%s)%s" % (
                    fk["from"], fk["table"], fk["to"],
                    "  [%s]" % ", ".join(actions) if actions else ""), tags=("section",))
                self._nodes[y] = ("fk", m, (table, fk["from"]))
        except Exception:               # noqa: BLE001 - no foreign keys to show
            pass
        try:
            for name, unique, icols in m.db.indexes(table):
                y = tree.insert(iid, "end", text="  %s %s (%s)" % (
                    "UNIQUE" if unique else "INDEX", name, ", ".join(icols)), tags=("section",))
                self._nodes[y] = ("index", m, (table, name))
        except Exception:               # noqa: BLE001 - no indexes to show
            pass
        try:
            for chk in m.db.check_constraints(table):
                y = tree.insert(iid, "end", text="  CHECK %s" % chk, tags=("section",))
                self._nodes[y] = ("check", m, (table, chk))
        except Exception:               # noqa: BLE001
            pass

    def _on_select(self, _e=None):
        self._update_sel_bar()
        n = self._nodes.get(self.tree.focus())
        if n is None:
            return
        kind, m, name = n
        table = name if kind in ("table", "view") else name[0] if kind in ("column", "fk") \
            else None
        if table is not None and m is not None:
            self._show_sql(m, table)

    def _show_sql(self, m, table):
        try:
            sql = m.db.create_sql(table) or ""
        except Exception:               # noqa: BLE001
            sql = ""
        self.sql_text.configure(state="normal")
        self.sql_text.delete("1.0", "end")
        self.sql_text.insert("1.0", sql)
        self.sql_text.configure(state="disabled")
        self.schema.set_title("CREATE statement", "%s › %s" % (m.name, table)
                              if len(self.members()) > 1 else table)
        self._sql_of = (m, table)

    def _on_click(self, e):
        """A click on a table opens it in Browse (in its database, made active)."""
        iid = self.tree.identify_row(e.y)
        if not iid or e.state & 0x0005:         # Shift / Ctrl: selecting several
            return
        if self.tree.identify_element(e.x, e.y) in ("Treeitem.indicator", "indicator"):
            return
        n = self._nodes.get(iid)
        if n is not None and n[0] == "empty_hidden":
            self.hide_empty_var.set(False)       # "N empty tables hidden — show"
            self._hide_empty_changed()
            return
        if n is not None and n[0] in ("table", "view", "wal_table"):
            self.app.browse_member_table(n[1], n[2])

    def _on_double(self, e):
        iid = self.tree.identify_row(e.y)
        n = self._nodes.get(iid)
        if n is not None and n[0] == "db":
            self.app.activate_member(n[1])
            return "break"
        if n is not None and n[0] == "column":
            self.app.browse_member_table(n[1], n[2][0])
            return "break"
        return None

    def _on_return(self, _e=None):
        n = self._nodes.get(self.tree.focus())
        if n is None:
            return None
        kind, m, name = n
        if kind == "db":
            self.app.activate_member(m)
        elif kind in ("table", "view", "wal_table"):
            self.app.browse_member_table(m, name)
        elif kind == "column":
            self.app.browse_member_table(m, name[0])
        return "break"

    def _next_match(self, forward=True):
        items = [iid for iid, n in self._nodes.items()
                 if n[0] in ("table", "column") and self.tree.exists(iid)]
        items.sort(key=lambda i: self.tree.index(i))
        order = []

        def walk(p):
            for c in self.tree.get_children(p):
                if c in self._nodes and self._nodes[c][0] in ("table", "column", "db"):
                    order.append(c)
                walk(c)
        walk("")
        if not order:
            return
        cur = self.tree.focus()
        i = order.index(cur) if cur in order else -1
        i = (i + (1 if forward else -1)) % len(order)
        self.tree.selection_set(order[i])
        self.tree.focus(order[i])
        self.tree.see(order[i])

    # -- selection ---------------------------------------------------------------------------
    def selected_members(self, uids=False):
        out = []
        for iid in self.tree.selection():
            n = self._nodes.get(iid)
            if n is not None and n[0] == "db":
                out.append(n[1].uid if uids else n[1])
        return out

    def clear_selection(self):
        self.tree.selection_set(())
        self._update_sel_bar()

    def _update_sel_bar(self):
        ms = self.selected_members()
        if len(ms) >= 2:
            self.sel_lbl.configure(text="%d databases selected" % len(ms))
            if not self.sel_bar.winfo_manager():
                self.sel_bar.pack(fill="x", padx=M, pady=(0, XS), before=self._tree_box)
        elif self.sel_bar.winfo_manager():
            self.sel_bar.pack_forget()

    def _scope_to_selection(self):
        scopes = getattr(self.app, "scopes", None)
        if scopes is not None:
            scopes.set_global([m.uid for m in self.selected_members()])

    # -- hover and menus ---------------------------------------------------------------------
    def _hover_text(self, iid):
        n = self._nodes.get(iid)
        if n is None:
            return None
        kind, m, name = n
        if kind == "db":
            from case_ui import member_tooltip
            active = m is getattr(self.app.case, "active", None)
            rows, exact = member_rows(m)
            lines = member_tooltip(m, active).split("\n")
            lines.insert(3, "%d tables, %s%s rows" % (len(m.db.tables()),
                                                       "" if exact else "about ",
                                                       format(rows, ",")))
            lines.insert(4, "App / folder: %s" % group_key(m.path))
            warns = member_warnings(m)
            for b in warns:
                lines.append("⚠ " + b.text)
            if self.hits.get(m.uid):
                lines.append("Last search: %s rows found" % format(self.hits[m.uid], ","))
            return "\n".join(l for l in lines if not l.startswith("Click:")) + \
                "\nDouble-click or Enter: make active.  Right-click: more."
        if kind == "table":
            c = m.counts.get(name, "?")
            try:
                ncols = len(m.db.columns(name))
            except Exception:           # noqa: BLE001
                ncols = 0
            return "%s\n%s · %s rows · %d columns\nClick: open in Browse." % (
                name, m.name, format(c, ",") if isinstance(c, int) else c, ncols)
        if kind == "column":
            table, col = name
            try:
                info = m.db.info(table)
                decl = next((c.type for c in info.columns if c.name == col), "")
            except Exception:
                decl = ""
            return "%s%s\nClick: open %s in Browse." % (
                col, " (%s)" % decl if decl else "", table)
        if kind == "group":
            ms = [x for x in self.members() if (x.path in self.pinned) == (name == "Pinned")
                  and (name == "Pinned" or group_key(x.path) == name)]
            size = sum((x.size or 0) for x in ms)
            return "%s\n%d databases, %s" % (name, len(ms), fmtb(size))
        return None

    def build_menu(self, iid):
        """The right-click menu of a line (None: nothing to offer)."""
        n = self._nodes.get(iid)
        if n is None:
            return None
        kind, m, name = n
        app = self.app
        menu = tk.Menu(self, tearoff=0)
        if kind == "db":
            active = m is getattr(app.case, "active", None)
            menu.add_command(label="Make active", state="disabled" if active else "normal",
                             command=lambda: app.activate_member(m))
            menu.add_command(label="Open in Browse", command=lambda: app.browse_member_table(
                m, None))
            menu.add_separator()
            menu.add_command(label="Unpin" if m.path in self.pinned else "Pin to top",
                             command=lambda: self.toggle_pin(m))
            menu.add_command(label="Show in folder", command=lambda: app.show_in_folder(m.path))
            menu.add_command(label="Copy path", command=lambda: self._copy(m.path, "path"))
            menu.add_command(label="Hash details…", command=lambda: app.show_member_evidence(m))
            if len(self.members()) > 1:
                menu.add_separator()
                sel = self.selected_members()
                if len(sel) >= 2 and m in sel:
                    menu.add_command(label="Use the %d selected as scope" % len(sel),
                                     command=self._scope_to_selection)
                menu.add_command(label="Remove from case",
                                 command=lambda: app.remove_member(m))
            return menu
        if kind in ("table", "view", "column", "fk", "wal_table"):
            table = name if kind in ("table", "view", "wal_table") else name[0]
            where = " (%s)" % m.name if len(self.members()) > 1 else ""
            menu.add_command(label="Browse '%s'%s" % (table, where),
                             command=lambda: app.browse_member_table(m, table))
            if kind != "wal_table":
                menu.add_separator()
                menu.add_command(label="Copy CREATE SQL",
                                 command=lambda: self._copy(m.db.create_sql(table) or "",
                                                            "CREATE statement"))
                menu.add_command(label="Copy table name", command=lambda: self._copy(table,
                                                                                    "name"))
                menu.add_command(label="Search in '%s'%s" % (table, where),
                                 command=lambda: app.search_in_table(m, table))
            if kind == "column" and app.relations.supported(table, m):
                col = str(name[1])
                menu.add_command(label="Column relationships of '%s'…" % col,
                                 command=lambda: (app.activate_member(m),
                                                  app.show_column_relations(table, col)))
            return menu
        return None

    def _on_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._popup(iid, e.x_root, e.y_root)

    def _on_menu_key(self, _e=None):
        iid = self.tree.focus()
        if not iid:
            return "break"
        bbox = self.tree.bbox(iid)
        x = self.tree.winfo_rootx() + (bbox[0] + 20 if bbox else 20)
        y = self.tree.winfo_rooty() + (bbox[1] + bbox[3] if bbox else 20)
        self._popup(iid, x, y)
        return "break"

    def _popup(self, iid, x, y):
        menu = self.build_menu(iid)
        if menu is None:
            return
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def toggle_pin(self, m):
        if m.path in self.pinned:
            self.pinned.discard(m.path)
        else:
            self.pinned.add(m.path)
        save = getattr(self.app, "_save_navigator_state", None)
        if save is not None:
            save()
        self.apply_filter()

    def note_used(self, m):
        self.last_used[m.uid] = time.time()

    def _copy(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.flash("Copied the %s" % what)

    def flash(self, text):
        self.note.configure(text=text)
        if not self.schema.is_open():
            self.search.set_status(text)
        if self._note_after is not None:
            try:
                self.after_cancel(self._note_after)
            except tk.TclError:
                pass
        self._note_after = self.after(3000, lambda: self.note.configure(text=""))

    def copy_sql(self):
        m, t = getattr(self, "_sql_of", (None, None))
        if m is None:
            self.flash("Select a table first")
            return
        self._copy(m.db.create_sql(t) or "", "CREATE statement of %s" % t)

    def copy_schema(self):
        m, t = getattr(self, "_sql_of", (None, None))
        if m is None:
            self.flash("Select a table first")
            return
        from utils import _build_schema_text
        self._copy(_build_schema_text(m.db, t, m.counts.get(t, "?")), "schema of %s" % t)

    def select_member(self, m):
        """Show a database's line (its group opened) and select it."""
        iid = self._db_iid.get(m.uid)
        if iid is None:
            return None
        parent = self.tree.parent(iid)
        if parent:
            self.tree.item(parent, open=True)
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        return iid

    def db_lines(self):
        """[(name, status, tables, rows)] of the databases listed (tests)."""
        out = []
        for iid, (kind, m, _n) in self._nodes.items():
            if kind == "db" and self.tree.exists(iid):
                v = self.tree.item(iid, "values")
                out.append((m.name, v[0], v[1], v[2]))
        return out

    def group_lines(self):
        return [self.tree.item(iid, "text") for iid, n in self._nodes.items()
                if n[0] == "group" and self.tree.exists(iid)]

    def mode_text(self, m):
        return mode_label(m.db.mode, long=True) if m.db.ok else "closed"
