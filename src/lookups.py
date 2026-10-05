"""Browse: 'Show value from linked table…' - a linked column shown with the value it stands
for in the linked table.

A column with a confident link (engine.relations in the same database, engine.crossdb to
another database of a case: 'matched by value') can show, next to each raw value, a column of
the row it links to - a jid with the contact's display name, a url id with the url. The
header's right-click menu offers it only for such columns; the user picks the linked table
(when there are several) and the column to show (a name / title / url / label-like text column
is preselected). Nothing is special-cased for any app.

The grid draws 'raw  → looked-up (db › table.column)' in its own colour through the column
formatter hook (grid.DataGrid.set_column_formatter): sorting, filters, copies and the raw
value stay the stored ones, and an export adds the looked-up values as a separate, clearly
named column. The linked table is read once on a worker thread (at most the 'lookup_rows'
limit rows; the header says when that cut it short). The choice is kept per table and column
with the tags of the database (never next to the evidence) and applied again when the table is
shown.
"""

import re
import sqlite3
import tkinter as tk
from tkinter import ttk

from combobox import SearchableCombobox
from constants import C
from database import RID
from tokens import COLOR as K, FONT as F
from engine import limits
from engine.relations import ROWID, _value_key, coerce, relation_map
from engine.schema import column_affinity, quote_ident
from grid import Runner
from utils import plain_text

SECTION = "lookups"     # {table: {column: {"db": path or "", "table", "key", "show"}}}
COLOR = K["teal"]       # looked-up values (dates are green, BLOBs purple)
ARROW = "→"
_SHOW_NAMES = re.compile(r"(?:^|_)(?:display_?name|full_?name|name|title|subject|label|url|"
                         r"nickname|description|text|value|number|address|email)$", re.I)


def short(v, n=60):
    s = plain_text(v).replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


class Target(object):
    """A linked column a lookup can read: member (None: the active database), table, key
    column, how it is linked and why."""
    __slots__ = ("member", "table", "key", "cross", "why")

    def __init__(self, member, table, key, cross, why):
        self.member, self.table, self.key, self.cross, self.why = member, table, key, cross, why

    def label(self, multi):
        where = self.member.label(self.table) if multi and self.member is not None \
            else self.table
        key = "rowid" if self.key is ROWID else self.key
        return "%s.%s — %s" % (where, key, self.why)


class Lookup(object):
    """One active lookup of a Browse column: the linked table's key -> the value shown."""

    def __init__(self, table, column, target, show, source_label):
        self.table, self.column, self.target, self.show = table, column, target, show
        self.source_label = source_label        # 'wa.db › wa_contacts.display_name'
        self.map = None                         # _value_key(key) -> [shown, rows]
        self.affinity = "BLOB"
        self.capped = False
        self.rows = 0
        self.error = ""

    def value(self, raw):
        """(shown, rows) for a raw value of the column, or None when no linked row has it."""
        if self.map is None or raw is None or raw == "":
            return None
        return self.map.get(_value_key(coerce(raw, self.affinity)))

    def text(self, raw):
        """'raw  → shown (db › table.column)' for the grid; None: the raw value alone."""
        hit = self.value(raw)
        if hit is None:
            return None
        shown, n = hit
        more = " (+%d more)" % (n - 1) if n > 1 else ""
        return "%s  %s %s%s  (%s)" % (short(raw, 40), ARROW, short(shown), more,
                                     self.source_label)


class LookupDialog(tk.Toplevel):
    """Choose the linked table and the column to show. result: (Target, column) or None;
    stop=True when the user chose to stop showing the lookup."""

    def __init__(self, parent, table, column, targets, multi, current=None, columns_of=None):
        tk.Toplevel.__init__(self, parent)
        self.title("Show value from linked table")
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.resizable(True, False)
        self.result, self.stop = None, False
        self.targets, self.columns_of = targets, columns_of
        tk.Label(self, text="Next to each value of %s.%s, show a column of the row it links to."
                            "\nThe raw value stays: sorting, filters, copies and exports use it."
                 % (table, column), bg=C["bg"], anchor="w", justify="left").pack(
            fill="x", padx=12, pady=(10, 6))
        tk.Label(self, text="Linked table", bg=C["bg"], font=F["body_bold"],
                 anchor="w").pack(fill="x", padx=12)
        self.target_var = tk.StringVar()
        labels = [t.label(multi) for t in targets]
        self._by_label = dict(zip(labels, targets))
        self.target_combo = SearchableCombobox(self, textvariable=self.target_var, values=labels,
                                         state="readonly", width=90)
        self.target_combo.pack(fill="x", padx=12, pady=(2, 8))
        self.target_combo.bind("<<ComboboxSelected>>", lambda e: self._fill_columns())
        tk.Label(self, text="Column to show", bg=C["bg"], font=F["body_bold"],
                 anchor="w").pack(fill="x", padx=12)
        self.show_var = tk.StringVar()
        self.show_combo = SearchableCombobox(self, textvariable=self.show_var, state="readonly",
                                       width=40)
        self.show_combo.pack(anchor="w", padx=12, pady=(2, 8))
        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Show", style="P.TButton", command=self.ok).pack(side="right",
                                                                            padx=6)
        if current is not None:
            ttk.Button(bar, text="Stop showing it", command=self.off).pack(side="left")
        pick = 0
        if current is not None:
            for i, t in enumerate(targets):
                if (t.member, t.table, t.key) == (current.target.member, current.target.table,
                                                  current.target.key):
                    pick = i
        if labels:
            self.target_var.set(labels[pick])
        self._fill_columns(current.show if current is not None else None)

    def target(self):
        return self._by_label.get(self.target_var.get())

    def _fill_columns(self, prefer=None):
        t = self.target()
        cols, default = self.columns_of(t) if t is not None else ([], None)
        self.show_combo.configure(values=cols)
        self.show_var.set(prefer if prefer in cols else (default or (cols[0] if cols else "")))

    def ok(self):
        t = self.target()
        if t is not None and self.show_var.get():
            self.result = (t, self.show_var.get())
        self.destroy()

    def off(self):
        self.stop = True
        self.destroy()


class BrowseLookups(object):
    """The lookups of the Browse grid's columns (App._browse_lookups)."""

    def __init__(self, app):
        self.app = app
        self.active = {}                # column name -> Lookup (of the table shown)
        self.runner = Runner(app, "lookup", release=app._release_worker_connection)
        self._gen = 0
        self.last_note = ""

    @property
    def grid(self):
        return self.app._browse_grid

    def table(self):
        t = self.app._browse_table_var.get()
        return t if t and not t.startswith("WAL: ") and self.app._browse_source is not None \
            else None

    def reset(self):
        """The Browse grid shows another table (or database): forget the lookups shown."""
        self._gen += 1
        self.runner.cancel()
        self.active = {}

    def stop(self):
        self._gen += 1
        self.runner.cancel()

    def worker_threads(self):
        return self.runner.threads()

    # -- what can be looked up --------------------------------------------------------------
    def targets(self, table, column):
        """The linked columns of table.column a lookup can read: its confident links in the
        database that go out to another table's key or hold the same values, and (in a case)
        those to other databases, matched by value."""
        rel = self.app.relations
        if column in (None, RID) or not rel.supported(table):
            return []
        out = []
        for r in rel.confident(table, column) or []:
            if r.direction == "in":
                continue                # others refer to this column: one-to-many
            out.append(Target(None, r.other, r.other_column, False,
                              "declared foreign key" if r.kind == "fk" else
                              "verified by values"))
        multi = rel.multi()
        for link, other, t, c in rel.cross_targets(table, column):
            out.append(Target(other, t, c, True, "matched by value (%d%% of sampled values "
                              "found)" % round(100 * link.fraction)))
        if not multi:
            out = [t for t in out if not t.cross]
        return out

    def _db(self, target):
        return target.member.db if target.member is not None else self.app.db

    def columns_of(self, target):
        """(columns to show, the preselected one) of a target: a name / title / url / label-
        like text column when there is one."""
        s = self._db(target).session
        info = s.info(target.table)
        key = target.key.lower() if target.key is not ROWID else None
        cols = [c for c in info.columns if c.hidden != 1 and c.name.lower() != key]
        names = [c.name for c in cols]
        texts = [c.name for c in cols if column_affinity(c.decl_type) == "TEXT"]
        default = next((n for n in texts if _SHOW_NAMES.search(n)), None) or \
            next((n for n in names if _SHOW_NAMES.search(n)), None) or \
            (texts[0] if texts else (names[0] if names else None))
        return names, default

    # -- saved choices -------------------------------------------------------------------------
    def saved(self, table):
        per = self.app.tags.state_section(SECTION).get(table)
        return dict((c, d) for c, d in per.items() if isinstance(d, dict)) \
            if isinstance(per, dict) else {}

    def _store(self, table, column, spec):
        st = self.app.tags.state_section(SECTION)
        per = st.get(table) if isinstance(st.get(table), dict) else {}
        if spec is None:
            per.pop(column, None)
        else:
            per[column] = spec
        if per:
            st[table] = per
        else:
            st.pop(table, None)
        self.app.tags.set_state_section(SECTION, st)

    def _spec(self, target, show):
        return {"db": target.member.path if target.member is not None else "",
                "table": target.table, "key": "" if target.key is ROWID else target.key,
                "show": show}

    def _target_of(self, table, column, spec):
        """The Target a saved spec names, among the column's current targets (None when the
        link or its database is gone)."""
        for t in self.targets(table, column):
            path = t.member.path if t.member is not None else ""
            key = "" if t.key is ROWID else t.key
            same = (path == spec.get("db", "") or
                    (path and spec.get("db") and path.lower() == spec["db"].lower()))
            if same and t.table == spec.get("table") and key.lower() == \
                    str(spec.get("key") or "").lower():
                return t
        return None

    # -- applying ---------------------------------------------------------------------------------
    def apply(self, table):
        """The Browse grid shows `table` now: its saved lookups are read again."""
        self.reset()
        notes = []
        for column, spec in self.saved(table).items():
            t = self._target_of(table, column, spec)
            if t is None:
                notes.append("%s: its linked table %s is not open or no longer linked" % (
                    column, spec.get("table")))
                continue
            self.show(table, column, t, spec.get("show"), remember=False)
        self.last_note = "; ".join(notes)
        return notes

    def show(self, table, column, target, show, remember=True):
        """Show column's values with target's `show` column (read on the worker thread)."""
        cols = self.grid.columns()
        if column not in cols or not show:
            return None
        multi = self.app.relations.multi()
        where = target.member.label(target.table) if multi and target.member is not None \
            else target.table
        lk = Lookup(table, column, target, show, "%s.%s" % (where, show))
        self.active[column] = lk
        if remember:
            self._store(table, column, self._spec(target, show))
        c = cols.index(column)
        self.grid.set_column_formatter(c, None)
        self.grid.set_column_formatter(c, lambda v: None, "%s reading %s…" % (ARROW, where),
                                       COLOR)
        gen = self._gen
        session = self._db(target).session
        limit = limits.get("lookup_rows")

        def work():
            return self._read(session, target, show, limit)

        def done(result, error):
            if gen != self._gen or self.active.get(column) is not lk:
                return
            cols_now = self.grid.columns()
            if column not in cols_now:
                return
            ci = cols_now.index(column)
            if error is not None:
                lk.error = str(error)
                self.grid.set_column_formatter(ci, None)
                self.grid.set_column_formatter(ci, lambda v: None, "%s not readable: %s" % (
                    ARROW, short(error, 40)), COLOR)
                return
            lk.map, lk.affinity, lk.capped, lk.rows = result
            badge = "%s %s" % (ARROW, lk.source_label)
            if lk.capped:
                badge += " (first %s rows only: limit lookup_rows)" % format(limit, ",")
            self.grid.set_column_formatter(ci, lk.text, badge, COLOR)
        self.runner.submit(("lookup", column), work, done)
        return lk

    @staticmethod
    def _read(session, target, show, limit):
        """(map, key affinity, capped, rows read) of the linked table: key -> [shown, n]."""
        m = relation_map(session)
        m.build()
        aff = m.affinity(target.table, target.key)
        t = session.info(target.table)
        out, n, capped = {}, 0, False
        rows = None
        if session.source(target.table) == "sql":
            key = t.rowid_name if target.key is ROWID else quote_ident(target.key)
            if key is not None:
                try:
                    cur = session.conn().execute("SELECT %s, %s FROM %s LIMIT ?" % (
                        key, quote_ident(show), quote_ident(target.table)), (limit + 1,))
                    try:
                        rows = []
                        while True:
                            chunk = cur.fetchmany(5000)
                            if not chunk:
                                break
                            rows.extend(chunk)
                    finally:
                        cur.close()
                except sqlite3.Error:
                    rows = None
        if rows is None:
            cols = session.visible_columns(target.table)
            low = [c.lower() for c in cols]
            si = low.index(show.lower())
            ki = None if target.key is ROWID else low.index(target.key.lower())
            rows = []
            for i, r in enumerate(session.iter_rows(target.table)):
                if i > limit:
                    break
                k = r.locator.value if ki is None else (r.values[ki] if ki < len(r.values)
                                                        else None)
                rows.append((k, r.values[si] if si < len(r.values) else None))
        for k, v in rows:
            n += 1
            if n > limit:
                capped = True
                break
            kk = _value_key(k)
            if kk is None:
                continue
            hit = out.get(kk)
            if hit is None:
                out[kk] = [v, 1]
            else:
                hit[1] += 1
        return out, aff, capped, min(n, limit)

    def set_off(self, table, column):
        self.active.pop(column, None)
        self._store(table, column, None)
        cols = self.grid.columns()
        if column in cols:
            self.grid.set_column_formatter(cols.index(column), None)

    # -- exports ----------------------------------------------------------------------------------
    def export_columns(self, names):
        """[(index of the raw column, name of the added column, Lookup)] for an export of these
        columns: each looked-up column adds '<column> → <db › table.column>'."""
        out = []
        for i, name in enumerate(names):
            lk = self.active.get(name)
            if lk is not None and lk.map is not None:
                out.append((i, "%s %s %s" % (name, ARROW, lk.source_label), lk))
        return out

    # -- the header menu ----------------------------------------------------------------------------
    def header_menu(self, menu, c):
        """DataGrid on_header_menu: 'Show value from linked table…' for a column with a
        confident link (nothing for the others)."""
        table = self.table()
        cols = self.grid.columns()
        if table is None or c < 1 or c >= len(cols):
            return None
        column = cols[c]
        targets = self.targets(table, column)
        if not targets and column not in self.active:
            return None
        menu.add_separator()
        menu.add_command(label="Show value from linked table…",
                         command=lambda: self.dialog(table, column))
        if column in self.active:
            menu.add_command(label="Stop showing the linked value",
                             command=lambda: self.set_off(table, column))
        return targets

    def dialog(self, table, column):
        targets = self.targets(table, column)
        if not targets:
            return None
        dlg = LookupDialog(self.app, table, column, targets, self.app.relations.multi(),
                           self.active.get(column), self.columns_of)
        self.app.wait_window(dlg)
        if dlg.stop:
            self.set_off(table, column)
        elif dlg.result is not None:
            target, show = dlg.result
            return self.show(table, column, target, show)
        return None
