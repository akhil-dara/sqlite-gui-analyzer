"""Row tagging in the UI.

Tagging owns the tag store of the open database (engine.tags.TagStore) and everything the UI
does with it: the Tag menus of the Browse grid and the search results (and of any other tab,
through App.tag_menu / App.tag_entries), the Ctrl+T / Ctrl+1..9 keys, tagging many rows on a
worker thread with progress and Cancel, saving (debounced, and on close), the Browse view
state saved per table, the evidence-changed warning, exports and the Recent databases menu.

Worker threads never call into Tk: a Job's progress and result are polled with after().
"""

import itertools
import os
import threading
import tkinter as tk
from collections import OrderedDict
from tkinter import messagebox, simpledialog, ttk

from constants import C, VERSION, wal_state_label
from tokens import FONT as F
from engine import limits
from engine.tag_export import (evidence_files, export_csv, export_html, export_info,
                               export_json)
from engine.evidence import inside_any, is_network_path
from engine.tags import (TagDef, TagEntry, TagError, TagStore, add_recent, database_info, db_key,
                         entry_from_db_row, entry_from_group, entry_from_wal_record, group_key,
                         inside, load_settings, save_settings, settings_path, tint, wal_key,
                         write_json_atomic)
from jobs import Job  # noqa: F401 - tagging jobs (and callers importing it from here)
from search_results import source_kind

BULK_CONFIRM = 100        # tagging more rows than this at once asks first
SYNC_ROWS = 500           # selections up to this many rows are read on the Tk thread
SYNC_READS = 200          # search results needing more row reads than this use a worker
SAVE_DELAY_MS = 1000      # changes are saved this long after the last one
KEY_CACHE = 20000


def recent_entry(path, text=None):
    """(label, state) of a Recent ▾ entry: text (default 'name — folder'), '(not found)' and
    disabled when the file is gone. A network path (UNC, a mapped network drive) is not
    checked (that would make Windows connect to the other computer with the user's sign-in)
    and says so; it opens only when chosen."""
    if text is None:
        text = "%s   — %s" % (os.path.basename(path), os.path.dirname(path))
    if is_network_path(path):
        return "%s  — network path, not checked" % text, "normal"
    there = os.path.isfile(path)
    return "%s%s" % (text, "" if there else "  (not found)"), "normal" if there else "disabled"


class HitGroup(object):
    """One search hit shaped like a search_results.RowGroup (the flat result list)."""
    __slots__ = ("table", "locator", "source", "row", "hits", "frames", "dbid", "database")

    def __init__(self, hit):
        self.table, self.locator = hit["table"], hit.get("locator")
        self.source, self.row = source_kind(hit), hit.get("row")
        self.hits, self.frames = [hit], list(hit.get("frames") or ())
        self.dbid, self.database = hit.get("dbid"), hit.get("database", "")

    def source_label(self):
        return self.hits[0].get("source", "DB")


class NoteDialog(tk.Toplevel):
    """Edit a note: returns the text through .result (None when cancelled)."""

    def __init__(self, parent, title, text, info=""):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.result = None
        if info:
            tk.Label(self, text=info, bg=C["bg"], fg=C["text2"], anchor="w", justify="left",
                     wraplength=460).pack(fill="x", padx=10, pady=(8, 2))
        self.text = tk.Text(self, width=60, height=8, wrap="word", font=F["label"],
                            relief="solid", bd=1)
        self.text.pack(fill="both", expand=True, padx=10, pady=6)
        self.text.insert("1.0", text or "")
        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Save", style="P.TButton", command=self._ok).pack(side="right",
                                                                              padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        self.text.bind("<Control-Return>", lambda e: (self._ok(), "break")[1])
        self.text.focus_set()
        try:
            self.grab_set()
        except tk.TclError:             # not viewable yet on some window managers
            pass

    def _ok(self):
        self.result = self.text.get("1.0", "end-1c").strip()
        self.destroy()


class Tagging(object):
    """The tags of the open databases and every place the UI shows or changes them (App.tags).
    It reads the App's Browse grid and source, search results and database; nothing here runs
    before the App built its widgets, and a store exists only while its database is open.

    Each database of a case keeps its own tag file (a TagStore, as when it is opened alone);
    `store` is the active database's. Tag entries name their database, and every change goes
    to the store of the entry's database. The tag definitions (names, colours, order) are
    shared: a change to them is made in every store."""

    def __init__(self, app):
        self.app = app
        self.stores = OrderedDict()      # member uid -> TagStore
        self._active = None              # uid of the active database's store
        self.tab = None                  # the Tagged tab (tags_tab.TagsTab) registers itself
        self.job = None
        self.show_progress = True
        self.warn_with_dialogs = True    # ui_smoke collects the warnings instead
        self.messages = []               # every warning given (kept for tests and the tab)
        self.save_error = ""
        self._save_after = self._refresh_after = self._hash_after = None
        self._saver = None               # thread writing the tag file
        self._view_table = None
        self._wal_index = {}             # WAL-only table -> {(locator, frame): record}
        self._key_cache = {}
        self._swatches = {}
        self.settings = load_settings()
        limits.load(self.settings)       # the named limits (bad values: default + Issue)

    # -- the stores ---------------------------------------------------------------------------
    @property
    def store(self):
        """The tag store of the active database (None when no database is open)."""
        return self.stores.get(self._active)

    @store.setter
    def store(self, value):
        """Replace the active database's store (tests)."""
        if value is None:
            self.stores.pop(self._active, None)
            return
        if self._active is None:
            self._active = 0
        self.stores[self._active] = value

    def _members(self):
        case = getattr(self.app, "case", None)
        return list(case) if case is not None else []

    def _member_of(self, uid):
        for m in self._members():
            if m.uid == uid:
                return m
        return None

    def multi(self):
        """True when tags of several databases are shown (a case of 2+ databases)."""
        return len(self.stores) > 1

    def store_for(self, entry):
        """The store an entry belongs to: the one of its database, else the active one."""
        ident = entry.identity if entry is not None else ""
        if ident and len(self.stores) > 1:
            for s in self.stores.values():
                if s.database.get("identity") == ident:
                    return s
            path = (entry.database or {}).get("path")
            if path:
                norm = os.path.normcase(os.path.abspath(path))
                for s in self.stores.values():
                    if os.path.normcase(s.db_path) == norm:
                        return s
        return self.store

    def store_of_member(self, member):
        return self.stores.get(getattr(member, "uid", None))

    def member_of_store(self, store):
        for uid, s in self.stores.items():
            if s is store:
                return self._member_of(uid)
        return None

    def database_name(self, entry):
        """The case's name for an entry's database (e.g. 'wa.db'), '' when unknown."""
        m = self.member_of_store(self.store_for(entry))
        if m is not None:
            return m.name
        return (entry.database or {}).get("name") or ""

    def all_entries(self, tag=None):
        """Entries of every open database (the active one's first), optionally of one tag."""
        out = []
        for s in self._ordered_stores():
            out.extend(s.entries(tag))
        return out

    def _ordered_stores(self):
        return list(self.stores.values())

    def total(self):
        return sum(len(s) for s in self.stores.values())

    def counts(self):
        """{tag: rows having it} over every database, in definition order."""
        store = self.store
        out = OrderedDict((d.name, 0) for d in (store.defs if store is not None else ()))
        for s in self.stores.values():
            for name, n in s.counts().items():
                out[name] = out.get(name, 0) + n
        return out

    def entry_by_case_key(self, case_key):
        ident, _sep, key = case_key.partition("\x1f")
        for s in self.stores.values():
            e = s.get(key)
            if e is not None and (not ident or e.identity == ident or len(self.stores) == 1):
                return e
        return None

    def def_op(self, op, *args):
        """Change the tag definitions in every store: op is a TagStore method name
        (add_def, rename_def, recolor_def, delete_def, move_def). Errors of the active store
        propagate; the other stores follow it. Returns the active store's result."""
        store = self.store
        result = getattr(store, op)(*args)
        for s in self.stores.values():
            if s is not store:
                try:
                    getattr(s, op)(*args)
                except TagError:
                    pass
        self._sync_defs()
        return result

    def _sync_defs(self):
        """Give every store the active store's definitions (order and colours), keeping a
        store's own extra tags after them."""
        store = self.store
        if store is None:
            return
        for s in self.stores.values():
            if s is store:
                continue
            extra = [d for d in s.defs if store.def_of(d.name) is None]
            before = [(d.name, d.color) for d in s.defs]
            s.defs = [TagDef(d.name, d.color) for d in store.defs] + extra
            if [(d.name, d.color) for d in s.defs] != before:
                for e in s.entries():
                    s._sort_tags(e)
                s._touch()
        for s in self.stores.values():          # the active store learns the others' tags
            for d in s.defs:
                if store.def_of(d.name) is None:
                    store.defs.append(TagDef(d.name, d.color))
                    store._touch()

    def merge_file(self, path):
        """Merge a tag file or a JSON export: each entry goes to the store of its database
        (the active one when it names none, or one not open). Returns (added, merged)."""
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if len(self.stores) <= 1 or not isinstance(data, dict):
            return self.store.merge_data(data)
        parts = OrderedDict()
        for d in data.get("entries") or ():
            try:
                s = self.store_for(TagEntry.from_dict(d))
            except TagError:
                s = self.store
            parts.setdefault(id(s), (s, []))[1].append(d)
        added = merged = 0
        for s, entries in parts.values() or [(self.store, [])]:
            a, m = s.merge_data(dict(data, entries=entries))
            added, merged = added + a, merged + m
        self._sync_defs()
        return added, merged

    # -- lifecycle --------------------------------------------------------------------------
    def _load_store(self, db):
        store = TagStore(db.evidence.main)
        try:
            store.load()
        except Exception as e:          # noqa: BLE001 - tags must never keep a database shut
            store.warnings.append("Tags could not be loaded: %s" % e)
        fp = db.evidence.fingerprints["main"]
        diffs = store.set_evidence(db.evidence.main, fp.size, fp.mtime_ns, fp.sha256)
        return store, diffs

    def opened(self, path, member=None):
        """A database was opened (the first of a case): load its tags and saved view state,
        remember it in the Recent list. Returns the table browsed last time (None when
        unknown)."""
        member = member if member is not None else getattr(
            getattr(self.app, "case", None), "active", None)
        uid = member.uid if member is not None else 0
        db = member.db if member is not None else self.app.db
        store, diffs = self._load_store(db)
        self.stores = OrderedDict([(uid, store)])
        self._active = uid
        self._view_table = None
        self._wal_index, self._key_cache = {}, {}
        for text in store.warnings:
            self.warn(text)
        if diffs:
            self._evidence_warning(diffs, store)
        self._remember_recent(db.evidence.main)
        self._schedule_hash_poll()
        self.refresh_views(now=True)
        return store.last_table

    def add_member(self, member):
        """Another database joined the case: load its tags (they keep their own file)."""
        if member.uid in self.stores:
            return self.stores[member.uid]
        store, diffs = self._load_store(member.db)
        self.stores[member.uid] = store
        for text in store.warnings:
            self.warn(text)
        if diffs:
            self._evidence_warning(diffs, store)
        self._sync_defs()
        self._schedule_hash_poll()
        self.refresh_views()
        return store

    def activate(self, member):
        """The active database changed: keep the Browse view state of the one left, then the
        store of `member` is the active one. Returns the table browsed last in it."""
        if member.uid == self._active:
            return self.store.last_table if self.store is not None else None
        self.remember_view()
        if member.uid not in self.stores:
            self.add_member(member)
        self._active = member.uid
        self._view_table = None
        self._wal_index, self._key_cache = {}, {}
        self.refresh_views(now=True)
        return self.store.last_table

    def remove_member(self, member):
        """A database leaves the case: save its tags and forget them here."""
        if self.job is not None and member.uid == self._active:
            self.job.cancel()
        store = self.stores.pop(member.uid, None)
        if store is not None:
            if member.uid == self._active:
                self.remember_view()
                self._view_table = None
            self._save(store)
        if member.uid == self._active:
            self._active = next(iter(self.stores), None)
        self._key_cache = {}
        self.refresh_views(now=True)

    def closing(self):
        """Every database is closing: stop a tagging job, keep the view state and save."""
        if self.job is not None:
            self.job.cancel()
        for name in ("_save_after", "_refresh_after", "_hash_after"):
            aid = getattr(self, name)
            if aid is not None:
                try:
                    self.app.after_cancel(aid)
                except tk.TclError:
                    pass
                setattr(self, name, None)
        if self.store is not None:
            self.remember_view()
        for store in list(self.stores.values()):
            self._save(store)
        self.stores = OrderedDict()
        self._active = None
        self._view_table = None
        self._wal_index, self._key_cache = {}, {}
        self.refresh_views(now=True)

    def worker_threads(self):
        """Threads of a running tagging or export job (closing waits for them)."""
        job = self.job
        return [job.thread] if job is not None and job.thread.is_alive() else []

    def busy(self):
        return self.job is not None

    # -- warnings and saving -----------------------------------------------------------------
    def warn(self, text):
        self.messages.append(text)
        if self.tab is not None:
            self.tab.show_warning(text)
        if self.warn_with_dialogs:
            self.app.after(200, lambda: messagebox.showwarning("Tags", text, parent=self.app))

    def _evidence_warning(self, diffs, store=None):
        store = store if store is not None else self.store
        name = os.path.basename(store.db_path) if store is not None else "The database"
        self.warn("%s changed since its tags were saved (%s). The %d tagged rows are "
                  "kept with the values they had when tagged; the rows themselves may differ "
                  "now." % (name, "; ".join(diffs), len(store) if store is not None else 0))

    def _schedule_hash_poll(self):
        if self._hash_after is None:
            self._hash_after = self.app.after(1000, self._poll_hash)

    def _db_of_store(self, store):
        m = self.member_of_store(store)
        if m is not None:
            return m.db
        return self.app.db if store is self.store else None

    def _poll_hash(self):
        """Once each database's evidence hash is known, compare it with the one its tags were
        saved for."""
        self._hash_after = None
        waiting = False
        for store in list(self.stores.values()):
            db = self._db_of_store(store)
            if db is None or not db.ok or db.evidence.hash_error or \
                    getattr(store, "hash_checked", False):
                continue
            fp = db.evidence.fingerprints.get("main")
            if fp is None or not fp.sha256:
                waiting = True
                continue
            before = store.evidence_differences()
            store.evidence["sha256"] = fp.sha256
            diffs = store.evidence_differences()
            if diffs and not before:
                self._evidence_warning(diffs, store)
            store.hash_checked = True
        if waiting:
            self._hash_after = self.app.after(1000, self._poll_hash)

    def _refuse_dirs(self):
        case = getattr(self.app, "case", None)
        if case is not None and len(case):
            return case.evidence_dirs()
        return [self.app.db.evidence.directory] if self.app.db.ok else []

    def save_settings(self):
        """Write the app settings (Recent lists); refused inside any evidence folder."""
        try:
            path = settings_path()
            d = inside_any(path, self._refuse_dirs())
            if d is not None:
                raise TagError("refusing to write %s: it is inside the evidence folder %s"
                               % (path, d))
            save_settings(self.settings)
        except (OSError, TagError) as e:
            self.messages.append("Recent list not saved: %s" % e)

    def _remember_recent(self, path):
        add_recent(self.settings, path)
        self.save_settings()

    def changed(self):
        """The tags changed: save soon and redraw what shows them."""
        self._schedule_save()
        self.refresh_views()

    def _schedule_save(self):
        if self._save_after is not None:
            self.app.after_cancel(self._save_after)
        self._save_after = self.app.after(SAVE_DELAY_MS, self._save_soon)

    def _save_soon(self):
        self._save_after = None
        for store in list(self.stores.values()):
            if store.dirty:
                self._save(store, background=True)

    def save_now(self):
        """Write the tags now (waits for a save still running on its thread)."""
        if self._save_after is not None:
            self.app.after_cancel(self._save_after)
            self._save_after = None
        for store in list(self.stores.values()):
            self._save(store)

    def _save(self, store, background=False):
        """Write the tag file if it changed. A background save takes the content here and
        writes it on a thread, so a slow disk or many tagged rows never freeze the window.
        One write at a time; a closing save waits for the running one."""
        saver = self._saver
        if saver is not None and saver.is_alive():
            if background:
                self._schedule_save()           # after the running write
                return
            saver.join()
        if background and store.dirty:
            data, path, refuse = store.to_data(), store.path, store.evidence_dir
            store.dirty = False

            def write():
                try:
                    write_json_atomic(path, data, refuse_in=refuse)
                    self.save_error = ""
                except (OSError, TagError) as e:
                    store.dirty = True          # the next change or the close tries again
                    self.save_error = "Tags not saved: %s" % e
            self._saver = threading.Thread(target=write, name="tag-save", daemon=True)
            self._saver.start()
            return
        try:
            store.save()
            self.save_error = ""
        except (OSError, TagError) as e:
            self.save_error = "Tags not saved: %s" % e
        if self.tab is not None:
            self.tab.show_status()

    def refresh_views(self, now=False):
        """Redraw the Browse marks, the search markers and (soon, or now) the Tagged tab."""
        self.app._browse_grid.restyle()
        self.refresh_search_markers()
        if self.tab is None:
            return
        if now:
            if self._refresh_after is not None:
                self.app.after_cancel(self._refresh_after)
                self._refresh_after = None
            self.tab.refresh()
        elif self._refresh_after is None:
            self._refresh_after = self.app.after(150, self._refresh_tab)

    def _refresh_tab(self):
        self._refresh_after = None
        if self.tab is not None:
            self.tab.refresh()

    # -- saved Browse view state -------------------------------------------------------------
    def state_section(self, name):
        """A named part of the open database's saved state ({} with no database)."""
        return self.store.section(name) if self.store is not None else {}

    def set_state_section(self, name, value):
        """Keep a named part of the saved state (saved soon, and on close)."""
        store = self.store
        if store is None:
            return
        store.set_section(name, value)
        if store.dirty:
            self._schedule_save()

    def member_section(self, member, name):
        """A named part of one database's saved state (the active one when member is None)."""
        store = self.store_of_member(member) if member is not None else self.store
        if store is None and member is not None and member.uid == self._active:
            store = self.store
        return store.section(name) if store is not None else {}

    def set_member_section(self, member, name, value):
        store = self.store_of_member(member) if member is not None else self.store
        if store is None:
            return
        store.set_section(name, value)
        if store.dirty:
            self._schedule_save()

    def remember_view(self):
        """Keep the widths, hidden columns and sort of the table the Browse grid shows."""
        store, table, grid = self.store, self._view_table, self.app._browse_grid
        if store is None or table is None or grid.source is None:
            return
        store.set_table_state(table, grid.view_state())
        if store.dirty:
            self._schedule_save()

    def restore_view(self, table):
        """The Browse grid shows `table` now: give it the view state saved for it."""
        self._view_table = table
        self._key_cache = {}
        if self.store is None:
            return
        self.app._browse_grid.apply_view_state(self.store.table_state(table))
        self.store.last_table = table

    # -- keys of rows ---------------------------------------------------------------------------
    def _cached(self, obj, make):
        hit = self._key_cache.get(id(obj))
        if hit is not None and hit[0] is obj:
            return hit[1]
        key = make()
        if len(self._key_cache) > KEY_CACHE:
            self._key_cache.clear()
        self._key_cache[id(obj)] = (obj, key)
        return key

    def _wal_record(self, table, locator, frame):
        """The WAL record behind a row of a WAL-only table (its grid shows display text; a tag
        keeps the values)."""
        idx = self._wal_index.get(table)
        if idx is None:
            idx = {}
            db = self.app.db
            if db.has_wal:
                for rec in db.wal.recover_all_records(table_filter=table):
                    idx.setdefault((rec["locator"], rec["frame_idx"]), rec)
            self._wal_index[table] = idx
        return idx.get((locator, frame))

    def browse_key(self, table, values):
        """Tag key of a Browse row (values = [locator, value, ...])."""
        loc = values[0]
        if table.startswith("WAL: "):
            def make():
                rec = self._wal_record(table[5:], loc, values[-3])
                return wal_key(rec["table"], rec["locator"], rec["raw_values"]) if rec else None
            return self._cached(values, make)
        if getattr(loc, "kind", None) == "ordinal":
            return self._cached(values, lambda: db_key(table, loc, list(values[1:])))
        return db_key(table, loc)

    def browse_entry(self, table, columns, values, flags=None):
        """Tag entry for a Browse row (columns and values include the locator column)."""
        if table.startswith("WAL: "):
            rec = self._wal_record(table[5:], values[0], values[-3])
            return entry_from_wal_record(rec) if rec is not None else None
        return entry_from_db_row(table, values[0], list(columns[1:]), list(values[1:]), flags)

    def browse_row_style(self, _row, values, _flags):
        """DataGrid row_style: tagged rows get their first tag's colour as a tint and marker."""
        store, table = self.store, self._view_table
        if store is None or not len(store) or table is None or not values:
            return None, None
        if not store.has_table(table[5:] if table.startswith("WAL: ") else table):
            return None, None
        key = self.browse_key(table, values)
        tags = store.tags_of(key) if key else []
        if not tags:
            return None, None
        color = store.color_of(tags[0])
        return tint(color), color

    def _group_key(self, g):
        first = g.hits[0] if g.hits else None
        if first is None:
            return None
        return self._cached(first, lambda: group_key(g))

    # -- entries of the Browse grid and the search results -------------------------------------
    def browse_range(self):
        """(first, last) row of the Browse selection, or of the current row."""
        grid = self.app._browse_grid
        sel = grid.selected_rows()
        if sel is None and grid.current_cell() is not None:
            sel = (grid.current_cell()[0], grid.current_cell()[0])
        return sel

    def browse_entries(self, lo, hi):
        src, table = self.app._browse_source, self._view_table
        if src is None or table is None:
            return []
        cols = src.columns()
        out = []
        for values, flags in self.app._browse_grid.fetch_rows(lo, hi):
            e = self.browse_entry(table, cols, list(values), flags)
            if e is not None:
                out.append(e)
        return out

    def _read_db_row(self, table, loc, db=None):
        """(values, columns, flags) of a database row read now (on the calling thread)."""
        s = (db if db is not None else self.app.db).session
        row = s.row(table, loc) if s is not None else None
        if row is None:
            return None, None, None
        snap = row.locator.snapshot
        cols = list(snap[0]) if snap is not None else s.visible_columns(table)
        return list(row.values), cols, row.flags

    def database_of(self, member):
        """What an entry keeps of a database of the case (None: the active one decides)."""
        if member is None:
            return None
        store = self.store_of_member(member)
        if store is not None:
            return dict(store.database)
        return database_info(member.path, member.size)

    def mark(self, entry, member):
        """Make an entry belong to `member`'s database (rows read from a database that may not
        be the active one); returns the entry."""
        if entry is not None and member is not None:
            entry.database = self.database_of(member)
        return entry

    def search_entry(self, g):
        """Entry for a search result line (a database row's values are read now)."""
        if g is None or (g.locator is None and g.source != "Freelist"):
            return None
        m = self._member_of(getattr(g, "dbid", None))
        try:
            if g.source == "DB" and getattr(g.locator, "snapshot", None) is None:
                values, cols, flags = self._read_db_row(g.table, g.locator,
                                                        m.db if m is not None else None)
                if values is None:
                    return None
                return self.mark(entry_from_group(g, cols, values, flags), m)
            return self.mark(entry_from_group(g), m)
        except TagError:
            return None

    # -- changing tags ---------------------------------------------------------------------------
    def by_store(self, entries):
        """[(store, entries)] of the given entries, each with the store of its database."""
        out = OrderedDict()
        for e in entries:
            if e is None:
                continue
            s = self.store_for(e)
            if s is not None:
                out.setdefault(id(s), (s, []))[1].append(e)
        return list(out.values())

    def tags_of(self, entry):
        """The tags a row has now (in the store of its database)."""
        s = self.store_for(entry)
        return s.tags_of(entry.key) if s is not None else []

    def is_tagged(self, entry):
        s = self.store_for(entry)
        return s is not None and entry.key in s

    def current(self, entry):
        """The stored entry of a row (with its tags and note), or None."""
        s = self.store_for(entry)
        return s.get(entry.key) if s is not None else None

    def log(self, action, entries, tag=None, changed=None, **more):
        """Note a tag action in the activity log: which rows (database, table, source, row
        key), the tag, how many changed."""
        activity = getattr(self.app, "activity", None)
        if activity is None:
            return
        rows = ["%s%s %s %s" % (((e.database or {}).get("name") + " › ")
                                if (e.database or {}).get("name") else "",
                                e.table, e.source, e.rowid or e.key)
                for e in entries if e is not None]
        activity("tag", action=action, tag=tag, rows=rows, changed=changed, **more)

    def tag_entries(self, entries, tag):
        """Give these rows `tag` (a tag of that name is made when unknown); returns the number
        of rows newly tagged."""
        if self.store is None:
            return 0
        if self.store.def_of(tag) is None:
            try:
                self.def_op("add_def", tag)
            except TagError:
                pass
        n = sum(s.add(es, tag) for s, es in self.by_store(entries))
        self.changed()
        self.log("tag", entries, tag, n)
        return n

    def set_tag(self, entries, tag, on):
        if self.store is None:
            return 0
        if on and self.store.def_of(tag) is None:
            try:
                self.def_op("add_def", tag)
            except TagError:
                pass
        n = sum(s.set_tags(es, tag, on) for s, es in self.by_store(entries))
        self.changed()
        self.log("tag" if on else "untag", entries, tag, n)
        return n

    def toggle(self, entries, index):
        """Ctrl+T / Ctrl+N: tag N on the rows, or off when every one of them has it."""
        store = self.store
        entries = [e for e in entries if e is not None]
        if store is None or not entries or not 0 <= index < len(store.defs):
            return
        name = store.defs[index].name
        on = not all(name in self.tags_of(e) for e in entries)
        self.set_tag(entries, name, on)

    def remove_all(self, entries):
        if self.store is not None:
            for s, es in self.by_store(entries):
                s.remove([e.key for e in es])
            self.changed()
            self.log("remove all tags", entries)

    def new_tag(self, entries=None, name=None):
        """Make a tag (asking its name) and give it to the rows; returns its name."""
        store = self.store
        if store is None:
            return None
        if name is None:
            name = simpledialog.askstring("New tag", "Name of the new tag:", parent=self.app)
        if not name or not name.strip():
            return None
        name = name.strip()
        try:
            if store.def_of(name) is None:
                self.def_op("add_def", name)
        except TagError as e:
            messagebox.showerror("New tag", str(e), parent=self.app)
            return None
        if entries:
            for s, es in self.by_store(entries):
                s.add(es, name)
        self.changed()
        return name

    def edit_note(self, entries, text=None):
        """Edit the note of the rows (asked in a dialog unless text is given). Rows not tagged
        yet get the first tag, as a note belongs to a tagged row."""
        store = self.store
        entries = [e for e in entries if e is not None]
        if store is None or not entries or not store.defs:
            return
        untagged = [e for e in entries if not self.is_tagged(e)]
        if text is None:
            current = next((self.current(e).note for e in entries
                            if self.is_tagged(e) and self.current(e).note), "")
            info = []
            if len(entries) > 1:
                info.append("The note is set on %d rows." % len(entries))
            if untagged:
                info.append("Rows not tagged yet are tagged '%s'." % store.defs[0].name)
            dlg = NoteDialog(self.app, "Note", current, " ".join(info))
            self.app.wait_window(dlg)
            text = dlg.result
            if text is None or self.store is not store:
                return
        for s, es in self.by_store(untagged):
            s.add(es, store.defs[0].name)
        for s, es in self.by_store(entries):
            for e in es:
                s.set_note(e.key, text)
        self.changed()
        self.log("note", entries, note=text)

    # -- menus ---------------------------------------------------------------------------------------
    def swatch(self, color):
        """A small square of a tag's colour for menus and lists."""
        img = self._swatches.get(color)
        if img is None:
            img = tk.PhotoImage(master=self.app, width=12, height=12)
            img.put(color, to=(1, 1, 11, 11))
            self._swatches[color] = img
        return img

    def tag_menu(self, parent, get_entries, label="Tag", accelerators=False):
        """Add a 'Tag' submenu to parent for the rows get_entries() returns (called now): a
        check item per tag (ticked when every row has it; choosing it gives the tag to all of
        them or takes it off), New tag..., Edit note... and Remove all tags."""
        sub = tk.Menu(parent, tearoff=0)
        store = self.store
        entries = []
        if store is not None:
            try:
                entries = [e for e in (get_entries() or ()) if e is not None]
            except Exception:           # noqa: BLE001 - a row that cannot be read is not tagged
                entries = []
        if store is None or not entries:
            parent.add_cascade(label=label, menu=sub, state="disabled")
            return sub
        have_tags = [self.tags_of(e) for e in entries]
        sub.tag_vars = []
        for i, d in enumerate(store.defs):
            have = all(d.name in t for t in have_tags)
            var = tk.BooleanVar(master=sub, value=have)
            sub.tag_vars.append(var)
            sub.add_checkbutton(label=d.name, variable=var, image=self.swatch(d.color),
                                compound="left",
                                accelerator="Ctrl+%d" % (i + 1) if accelerators and i < 9 else "",
                                command=lambda name=d.name, on=not have:
                                self.set_tag(entries, name, on))
        sub.add_separator()
        sub.add_command(label="New tag…", command=lambda: self.new_tag(entries))
        sub.add_command(label="Edit note…", command=lambda: self.edit_note(entries))
        sub.add_command(label="Remove all tags",
                        state="normal" if any(have_tags) else "disabled",
                        command=lambda: self.remove_all(entries))
        parent.add_cascade(label=label, menu=sub)
        return sub

    def _bulk_menu(self, parent, label, apply, count):
        """A submenu giving one tag to many rows: apply(tag)."""
        sub = tk.Menu(parent, tearoff=0)
        store = self.store
        for d in store.defs:
            sub.add_command(label=d.name, image=self.swatch(d.color), compound="left",
                            command=lambda name=d.name: apply(name))
        sub.add_separator()
        sub.add_command(label="New tag…", command=lambda: apply(self.new_tag()))
        parent.add_cascade(label=label, menu=sub, state="normal" if count else "disabled")
        return sub

    def browse_menu(self, menu, row, _col):
        """DataGrid on_context_menu: Tag the selected rows, or every row the filters keep."""
        src = self.app._browse_source
        if self.store is None or src is None or self._view_table is None:
            return
        lo, hi = self.app._browse_grid.selected_rows() or (row, row)
        n = hi - lo + 1
        menu.add_separator()
        if n <= SYNC_ROWS:
            self.tag_menu(menu, lambda: self.browse_entries(lo, hi), accelerators=True)
        else:
            self._bulk_menu(menu, "Tag %s selected rows" % format(n, ","),
                            lambda tag: self.tag_browse_range(lo, hi, tag), n)
        total = src.row_count()
        what = "filtered " if src.filtered else ""
        self._bulk_menu(menu, "Tag all %srows (%s)" % (what, format(total, ",")
                                                       if total is not None else "counting…"),
                        self.tag_all_filtered, total is None or total > 0)

    def search_menu(self, menu, group, hit):
        """Tag items of the search results' right-click menu."""
        if self.store is None:
            return
        g = group if group is not None else (HitGroup(hit) if hit is not None else None)
        menu.add_separator()
        self.tag_menu(menu, lambda: [self.search_entry(g)])
        n = len(self.app._sr_groups_filtered)
        self._bulk_menu(menu, "Tag all results (filtered, %s rows)" % format(n, ","),
                        self.tag_search_results, n)

    # -- many rows at once -------------------------------------------------------------------------
    def run_job(self, title, work, total, apply):
        """Run work(job) on a worker thread; apply(result) on the Tk thread when it finished,
        was not cancelled and the same database is still open."""
        if self.job is not None:
            messagebox.showinfo("Tags", "Another tagging job is still running.", parent=self.app)
            return None
        store = self.store

        def done(result, error, cancelled):
            self.job = None
            if self.store is not store:
                return
            if error is not None:
                messagebox.showerror("Tags", "%s failed:\n%s" % (title, error), parent=self.app)
            elif not cancelled and result is not None:
                apply(result)
        self.job = Job(self.app, title, work, done, total, show=self.show_progress,
                       release=self.app._release_worker_connection)
        return self.job

    def _confirm(self, n, what, tag):
        if n is not None and n <= BULK_CONFIRM:
            return True
        return messagebox.askyesno(
            "Tag rows", "Tag %s %s as '%s'?\n\nEach tagged row is saved with its values, so many "
            "rows make a large tag file." % ("all" if n is None else format(n, ","), what, tag),
            parent=self.app)

    def tag_all_filtered(self, tag, confirm=True):
        """Give `tag` to every row the Browse filters keep (a worker reads them)."""
        src, table, db = self.app._browse_source, self._view_table, self.app.db
        if not tag or self.store is None or src is None or table is None:
            return None
        n = src.row_count()
        if confirm and not self._confirm(n, "rows of %s" % table, tag):
            return None
        cols = src.columns()
        if not getattr(src, "threaded", False):          # a WAL-only table: rows in memory
            self.tag_entries([self.browse_entry(table, cols, list(v)) for v in src.iter_rows()],
                             tag)
            return None
        flt, order, desc = src.flt, src.order, src.desc

        def work(job):
            out = []
            for r in db.iter_filtered(table, flt, order, desc):
                if job.cancelled:
                    break
                out.append(entry_from_db_row(table, r[0], cols[1:], r[1:], r.flags))
                job.done += 1
            return out
        return self.run_job("Tagging rows of %s" % table, work, n,
                            lambda entries: self.tag_entries(entries, tag))

    def tag_browse_range(self, lo, hi, tag):
        """Give `tag` to Browse rows lo..hi of the current order (a large selection)."""
        src, table = self.app._browse_source, self._view_table
        if not tag or self.store is None or src is None or table is None:
            return None
        cols = src.columns()

        def work(job):
            out = []
            for r in itertools.islice(src.iter_rows(), lo, hi + 1):
                if job.cancelled:
                    break
                out.append(self.browse_entry(table, cols, list(r), getattr(r, "flags", None)))
                job.done += 1
            return out
        return self.run_job("Tagging rows of %s" % table, work, hi - lo + 1,
                            lambda entries: self.tag_entries(entries, tag))

    def tag_search_results(self, tag, confirm=True):
        """Give `tag` to every search result line the result filters keep."""
        groups = [g for g in self.app._sr_groups_filtered
                  if g.locator is not None or g.source == "Freelist"]
        if not tag or self.store is None or not groups:
            return None
        if confirm and not self._confirm(len(groups), "search results", tag):
            return None
        reads = sum(1 for g in groups
                    if g.source == "DB" and getattr(g.locator, "snapshot", None) is None)
        if reads <= SYNC_READS:
            self.tag_entries([self.search_entry(g) for g in groups], tag)
            return None

        def work(job):
            out = []
            for g in groups:
                if job.cancelled:
                    break
                out.append(self.search_entry(g))
                job.done += 1
            return out
        return self.run_job("Tagging search results", work, len(groups),
                            lambda entries: self.tag_entries(entries, tag))

    # -- keys ------------------------------------------------------------------------------------------
    def bind_keys(self):
        """Ctrl+T toggles the first tag, Ctrl+1..9 tag N, on the Browse selection and on the
        selected search result."""
        grid, tree = self.app._browse_grid, self.app._search_tree
        for seq, index in (("<Control-t>", 0), ("<Control-T>", 0)):
            grid.bind_key(seq, lambda i=index: self.toggle_browse(i))
            tree.bind(seq, lambda e, i=index: (self.toggle_search(i), "break")[1])
        for n in range(1, 10):
            grid.bind_key("<Control-Key-%d>" % n, lambda i=n - 1: self.toggle_browse(i))
            tree.bind("<Control-Key-%d>" % n,
                      lambda e, i=n - 1: (self.toggle_search(i), "break")[1])

    def toggle_browse(self, index):
        sel = self.browse_range()
        if sel is None or self.store is None or not 0 <= index < len(self.store.defs):
            return
        lo, hi = sel
        if hi - lo + 1 > SYNC_ROWS:
            self.tag_browse_range(lo, hi, self.store.defs[index].name)
        else:
            self.toggle(self.browse_entries(lo, hi), index)

    def toggle_search(self, index):
        sel = self.app._search_tree.selection()
        hit, group = self.app._sr_iid_map.get(sel[0], (None, None)) if sel else (None, None)
        if hit is not None:
            self.toggle([self.search_entry(group if group is not None else HitGroup(hit))],
                        index)

    # -- search result markers ------------------------------------------------------------------------
    def refresh_search_markers(self):
        """Mark tagged lines of the search results: a dot before the source and (lines not
        coloured by a WAL frame state) the first tag's tint."""
        app, store = self.app, self.store
        tree = app._search_tree
        defs = store.defs if store is not None else []
        index = dict((d.name, i) for i, d in enumerate(defs))
        for i, d in enumerate(defs):
            tree.tag_configure("tagcol%d" % i, background=tint(d.color))
        for pos, iid in enumerate(tree.get_children("")):
            hit, g = app._sr_iid_map.get(iid, (None, None))
            if hit is None:
                continue
            grp = g if g is not None else HitGroup(hit)
            label = app.result_source_label(grp) if hasattr(app, "result_source_label") \
                else grp.source_label()
            s = self.stores.get(getattr(grp, "dbid", None), store) if len(self.stores) > 1 \
                else store
            key = self._group_key(grp) if s is not None and len(s) else None
            names = s.tags_of(key) if key else []
            tags = [t for t in tree.item(iid, "tags") or () if not str(t).startswith("tagcol")]
            wal = any(str(t).startswith("wal_") for t in tags)
            if names:
                label = "● " + label
                if not wal:
                    tags = [t for t in tags if t not in ("odd", "even")]
                    tags.append("tagcol%d" % index.get(names[0], 0))
            elif not wal and "odd" not in tags and "even" not in tags:
                tags.append("odd" if pos % 2 else "even")
            tree.set(iid, "Source", label)
            tree.item(iid, tags=tags)

    # -- opening a tagged row -------------------------------------------------------------------------
    def open_entry(self, entry):
        """Show a tagged row: the row window for a database row, the WAL record window for a
        WAL version, the recovered-record window for anything else."""
        app = self.app
        if not app.db.ok:
            return None
        values = entry.row_values()
        member = self.member_of_store(self.store_for(entry)) if len(self.stores) > 1 else None
        if entry.source == "DB":
            from dialogs import RowWin
            loc = entry.row_locator()
            db = member.db if member is not None else app.db
            return RowWin.show(app, db, entry.table, loc) if loc is not None else None
        if member is not None and member is not getattr(app.case, "active", None):
            app.activate_member(member)     # WAL and recovered records: that database's tabs
        prov = entry.provenance
        if entry.source == "WAL":
            from wal_parser import display_value
            state = prov.get("frame_state")
            return app._show_wal_row_detail(
                source="WAL (%s)" % wal_state_label(state) if state else "WAL",
                table=entry.table, match_col="", rowid=entry.rowid, match_val="",
                row_data=OrderedDict((c, display_value(v)) for c, v in zip(entry.columns,
                                                                             values)),
                frame_idx=prov.get("frame"), page_num=prov.get("page"), category=state,
                locator=entry.row_locator(), row_values=values)
        from engine.schema import Locator
        hit = {"table": entry.table, "source": entry.source, "rowid": entry.rowid or "-",
               "locator": Locator("ordinal", 0, snapshot=(list(entry.columns), values)),
               "page": prov.get("page", "?"),
               "cell_offset": prov.get("cell_offset", prov.get("offset", "?")),
               "confidence": prov.get("confidence", "?"), "row": values}
        return app._show_record_detail(hit, "")

    # -- exports ------------------------------------------------------------------------------------------
    def export(self, fmt, target, entries, layout="tag", scope="", on_done=None):
        """Export entries ('html', 'csv' or 'json') on a worker thread: the evidence hashes may
        still be computing and a large BLOB may be read again. on_done(result) on the Tk
        thread. The caller has checked the target with utils.write_allowed; the engine refuses
        the evidence folder again."""
        store, db = self.store, self.app.db
        if store is None or not db.ok:
            return None
        defs = list(store.defs)
        # the database of each entry: every one of them is described with its hashes, and in
        # a case a table is written 'wa.db › table' (the JSON keeps the table and the
        # database apart, so it loads again)
        sessions, dbs = {}, []
        for s, es in self.by_store(entries):
            m = self.member_of_store(s)
            mdb = m.db if m is not None else db
            if mdb.ok and mdb not in dbs:
                dbs.append(mdb)
            for e in es:
                sessions[id(e)] = (mdb.session, m)
        multi = len(dbs) > 1
        copies = []
        for e in entries:
            c = e.copy()
            session, m = sessions.get(id(e), (db.session, None))
            sessions[id(c)] = (session, m)
            if multi and fmt != "json" and m is not None:
                c.table = m.label(c.table)
            copies.append(c)
        entries = copies
        case = getattr(self.app, "case", None)
        protected = case.is_protected if case is not None and len(case) else \
            db.evidence.is_protected

        def blob_loader(entry, ci):
            """The whole value of a BLOB the tag kept in part, read from the database again."""
            session, m = sessions.get(id(entry), (None, None))
            loc = entry.row_locator() if entry.source == "DB" else None
            if loc is None or loc.kind == "ordinal" or session is None:
                return None
            table = entry.table
            if m is not None and multi:
                table = table[len(m.label("")):]
            row = session.row(table, loc)
            return row.values[ci] if row is not None and ci < len(row.values) else None

        def work(job):
            files = []
            for d in dbs or [db]:
                files.extend(evidence_files(d.evidence))
            info = export_info(VERSION, files, "; ".join(d.evidence.main for d in dbs or [db]),
                               scope)
            if job.cancelled:
                return None
            if fmt == "html":
                return export_html(target, entries, defs, info, layout, protected)
            if fmt == "json":
                return export_json(target, entries, defs, info, protected)
            return export_csv(target, entries, defs, info, protected, blob_loader)
        def finished(result):
            activity = getattr(self.app, "activity", None)
            if activity is not None:
                activity("export", what="tagged rows (%s)" % fmt, path=target,
                         rows=len(entries), scope=scope,
                         result=result if isinstance(result, (dict, str)) else str(result))
            if on_done is not None:
                on_done(result)
        return self.run_job("Exporting %s tagged rows" % format(len(entries), ","), work, None,
                            finished)

    # -- Recent databases --------------------------------------------------------------------------------
    def recent_button(self, parent):
        """Header button listing the databases opened last."""
        btn = ttk.Button(parent, text="Recent ▾", style="HB.TButton")
        btn.configure(command=lambda: self._post_recent(btn))
        return btn

    def recent_menu(self):
        m = tk.Menu(self.app, tearoff=0)
        recent = [p for p in self.settings.get("recent") or () if isinstance(p, str)]
        cases = [c for c in self.settings.get("recent_cases") or () if isinstance(c, dict)
                 and isinstance(c.get("path"), str)]
        if not recent and not cases:
            m.add_command(label="(no recent databases)", state="disabled")
        for p in recent:
            label, state = recent_entry(p)
            m.add_command(label=label, state=state, command=lambda p=p: self.app.open_recent(p))
        if cases:
            m.add_separator()
            m.add_command(label="Cases (several databases)", state="disabled")
            for c in cases:
                names = [str(n) for n in c.get("names") or ()]
                shown = ", ".join(names[:4]) + (", …" if len(names) > 4 else "")
                label, state = recent_entry(c["path"], "Case of %d: %s" % (len(names), shown))
                m.add_command(label=label, state=state,
                              command=lambda p=c["path"]: self.app.open_recent_case(p))
        m.add_separator()
        m.add_command(label="Clear list", state="normal" if recent or cases else "disabled",
                      command=self.clear_recent)
        return m

    def _post_recent(self, btn):
        m = self.recent_menu()
        try:
            m.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
        finally:
            m.grab_release()

    def clear_recent(self):
        self.settings["recent"] = []
        self.settings["recent_cases"] = []
        self.save_settings()
