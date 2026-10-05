"""Row version history across the rollback journal, the main file and every WAL frame.

Eras, oldest first (Version.era):
  journal      page images saved in a rollback journal (the state before its transaction)
  stale        WAL frames of an earlier WAL generation (salts differ from the WAL header)
  main         the main database file
  committed    WAL frames of the current generation up to the last commit, in frame order
  uncommitted  WAL frames written after the last commit (a transaction in progress or rolled
               back; SQLite never shows them)
Every copy of a leaf page of the table is read, so a row's versions include those in
superseded frames. Consecutive identical copies are one version (the others are listed as
copies). For rowid tables the committed state after each WAL commit is also looked up, which
places deletions exactly: a Version with present=False marks the commit after which the row
was gone.

Rowid (or primary key) reuse: SQLite hands out rowid max+1, so after deleting the newest row
the next insert gets the same rowid; without AUTOINCREMENT any freed key can come back. A key
is reported as reused when, between two consecutive versions,
  (a) the row was absent in a committed state in between (deleted, then inserted again), or
  (b) every stored non-key column that is non-NULL in both versions changed at once, with at
      least two such columns compared (an UPDATE normally leaves something unchanged; a
      different entity usually shares nothing).
(b) is a heuristic: an UPDATE that rewrites every column also triggers it.
"""

from ..fileformat.btree import (BTreeReader, INDEX_INTERIOR, INDEX_LEAF, TABLE_INTERIOR,
                                TABLE_LEAF, cell_pointers, parse_page_header)
from ..fileformat.record import decode_record_lenient
from ..fileformat.wal import STALE, UNCOMMITTED
from .. import limits
from ..issues import IssueLog
from ..schema import Locator
from .live import row_key
from .pages import AsOfFrameView, OverlayView, WalSnapshotView
from .provenance import JOURNAL_FILE, MAIN_FILE, WAL_FILE, json_value, value_key

ERAS = ("journal", "stale", "main", "committed", "uncommitted")
_ERA_RANK = dict((e, i) for i, e in enumerate(ERAS))
MAX_SCAN_ROWS = 2000000         # the default of the limit 'history_scan_rows'


class Version(object):
    """One version of a row. values is None for a deletion marker (present=False)."""
    __slots__ = ("values", "present", "era", "file", "frame", "frame_state", "commit_group",
                 "page", "offset", "changed", "copies", "current", "note", "order")

    def __init__(self, values, era, file, page=None, offset=None, frame=None, frame_state=None,
                 commit_group=None, present=True, note="", order=0):
        self.values, self.present, self.era, self.file = values, present, era, file
        self.page, self.offset, self.frame = page, offset, frame
        self.frame_state, self.commit_group = frame_state, commit_group
        self.changed, self.copies, self.current, self.note = [], [], False, note
        self.order = order

    def where(self):
        if self.file == WAL_FILE:
            return "WAL frame %s (%s)" % (self.frame, self.frame_state)
        return {"main": "main file", "journal": "journal"}.get(self.file, self.file)

    def as_dict(self):
        vals = None if self.values is None else [json_value(v) for v in self.values]
        return {"present": self.present, "era": self.era, "file": self.file, "page": self.page,
                "offset": self.offset, "frame": self.frame, "frame_state": self.frame_state,
                "commit_group": self.commit_group, "values": vals, "changed": list(self.changed),
                "current": self.current, "note": self.note,
                "copies": [dict(zip(("file", "frame", "frame_state", "page", "offset"), c))
                           for c in self.copies]}

    def __repr__(self):
        return "Version(%s %s %r)" % (self.era, self.where(), self.values)


class RowHistory(object):
    def __init__(self, table, columns, locator):
        self.table, self.columns, self.locator = table, list(columns), locator
        self.versions = []
        self.current = None           # current values, or None when the row is gone
        self.deleted = False
        self.reuse = []               # reasons the key looks reused
        self.notes = []               # what could not be looked at (a limit), in words

    def __iter__(self):
        return iter(self.versions)

    def __len__(self):
        return len(self.versions)

    def as_dict(self):
        return {"table": self.table, "columns": self.columns, "locator": self.locator.display(),
                "deleted": self.deleted, "reuse": list(self.reuse), "notes": list(self.notes),
                "current": None if self.current is None else [json_value(v) for v in self.current],
                "versions": [v.as_dict() for v in self.versions]}


class KeyHistory(object):
    __slots__ = ("locator", "versions", "deleted", "reuse", "first", "last", "wal_only")

    def as_dict(self):
        return {"locator": self.locator.display(), "versions": self.versions,
                "deleted": self.deleted, "reuse": list(self.reuse), "first": self.first,
                "last": self.last, "wal_only": self.wal_only}


class HistorySummary(object):
    """Per-table overview: every key seen in a WAL frame or journal page, with its number of
    distinct versions. multi_version / deleted / reused are filtered views of `keys`."""

    def __init__(self, table):
        self.table = table
        self.keys = []
        self.frames_read = 0
        self.complete = True

    @property
    def multi_version(self):
        return [k for k in self.keys if k.versions > 1]

    @property
    def deleted(self):
        return [k for k in self.keys if k.deleted]

    @property
    def reused(self):
        return [k for k in self.keys if k.reuse]

    def as_dict(self):
        return {"table": self.table, "keys_seen": len(self.keys),
                "multi_version": [k.as_dict() for k in self.multi_version],
                "deleted": [k.as_dict() for k in self.deleted],
                "reused": [k.as_dict() for k in self.reused],
                "frames_read": self.frames_read, "complete": self.complete}


def reuse_reason(info, a, b):
    """Why two consecutive versions of one key look like different entities, or None."""
    skip = set(info.pk_columns)
    if info.rowid_alias is not None:
        skip.add(info.rowid_alias)
    compared = [ci for ci in info.storage_order
                if ci not in skip and ci < len(a) and ci < len(b)
                and a[ci] is not None and b[ci] is not None]
    if len(compared) >= 2 and all(value_key(a[ci]) != value_key(b[ci]) for ci in compared):
        return "every stored column changed at once (%d compared)" % len(compared)
    return None


class Historian(object):
    def __init__(self, fx):
        self.fx = fx
        self.session = fx.session
        self.issues = IssueLog()
        self._owner = None
        self._scan_capped = False       # the last _find() stopped at history_scan_rows
        self._find_error = None         # the last _find() could not read the tree (why)

    # -- helpers -----------------------------------------------------------
    def _info(self, table):
        info = self.session.schema.get(table)
        if info is None or not info.natively_readable:
            raise KeyError(table)
        return info

    def key_of(self, info, rowid, row):
        if info.without_rowid:
            return ("pk", tuple(value_key(row[i]) for i in info.pk_columns))
        return ("rowid", rowid)

    def _key_of_locator(self, info, locator):
        if locator.kind == "pk":
            return ("pk", tuple(value_key(v) for v in locator.value))
        return ("rowid", locator.value)

    def locator(self, info, rowid, row):
        if info.without_rowid:
            return Locator("pk", tuple(row[i] for i in info.pk_columns))
        return Locator("rowid", rowid)

    def page_owner(self):
        """page -> table name for every page a table ever used in the main file, the current
        state or the WAL (following WAL copies of interior pages to pages only the WAL has)."""
        if self._owner is not None:
            return self._owner
        owner = {}
        for pmap in (self.fx.main_map, self.fx.eff_map):
            for p, (name, _kind) in pmap.owner.items():
                owner.setdefault(p, name)
        wal = self.session.wal
        if wal is not None:
            usable = self.session.pager.usable_size
            for _ in range(8):
                changed = False
                for fr in wal.frames:
                    if fr.page_type not in (TABLE_INTERIOR, INDEX_INTERIOR):
                        continue
                    name = owner.get(fr.page_no)
                    if name is None:
                        continue
                    for child in _children(wal.page_data(fr.index), fr.page_no, usable):
                        if child not in owner:
                            owner[child] = name
                            changed = True
                if not changed:
                    break
        self._owner = owner
        return owner

    def _rows_of_image(self, data, page_no, info, view):
        """[(rowid, row, offset)] for the cells of one leaf image of the table."""
        try:
            h = parse_page_header(data, page_no)
        except Exception:
            return []
        wr = info.without_rowid
        if h.type not in ((INDEX_LEAF, INDEX_INTERIOR) if wr else (TABLE_LEAF,)):
            return []
        reader = BTreeReader(OverlayView(view, {page_no: data}), self.issues)
        enc = self.session.pager.encoding
        out = []
        try:
            if h.type == INDEX_INTERIOR:
                segs = [(i,) for i in range(len(reader._offsets(data, h)))]
                items = [x for (i,) in segs for x in reader.read_segment(page_no, i, True)]
            else:
                items = list(reader.read_segment(page_no, None, wr))
        except Exception:
            return out
        n = len(info.storage_order)
        full = False
        for rowid, payload, ref in items:
            values, problem = decode_record_lenient(payload, enc)
            if problem or len(values) > n:
                continue
            full = full or len(values) == n
            row, _flags = info.record_to_row(rowid, values)
            out.append((rowid, row, ref.offset))
        return out if full else []      # no record of this table's width: another table's page

    def _frames_of(self, info):
        wal = self.session.wal
        if wal is None:
            return []
        owner = self.page_owner()
        return [fr for fr in wal.frames if owner.get(fr.page_no) == info.name]

    @staticmethod
    def _era(fr):
        if fr.state == STALE:
            return "stale"
        if fr.state == UNCOMMITTED:
            return "uncommitted"
        return "committed"

    # -- one row -----------------------------------------------------------
    def row_history(self, table, locator, cancel=None):
        info = self._info(table)
        key = self._key_of_locator(info, locator)
        fx = self.fx
        hist = RowHistory(table, info.column_names, locator)
        obs = []                                  # (rank, order, Version)
        # journal pre-state
        journal = fx.journal()
        if journal is not None:
            jv = fx.journal_view()
            if jv is not None:
                for n in journal.pages():
                    if self.page_owner().get(n) != info.name:
                        continue
                    for rowid, row, off in self._rows_of_image(journal.page(n), n, info, jv):
                        if self.key_of(info, rowid, row) == key:
                            obs.append(Version(row, "journal", JOURNAL_FILE, n, off, order=n))
        # WAL frames (every state)
        wal = self.session.wal
        for fr in self._frames_of(info):
            if cancel is not None and cancel():
                break
            view = AsOfFrameView(fx.main, fx.timeline, fr.index)
            for rowid, row, off in self._rows_of_image(wal.page_data(fr.index), fr.page_no,
                                                       info, view):
                if self.key_of(info, rowid, row) == key:
                    obs.append(Version(row, self._era(fr), WAL_FILE, fr.page_no, off, fr.index,
                                       fr.state, fr.commit_group, order=fr.index))
        # main file
        self._scan_capped = False
        self._find_error = None
        main_hit = self._find(info, fx.main, key, self._main_root(info),
                              hint_pages=set(v.page for v in obs), cancel=cancel)
        if self._find_error:
            # a read error is not 'not in the main file': say it
            hist.notes.append("the main file could not be read to look for the row: %s "
                              "(it may be there)" % self._find_error)
            self.issues.add("history_read_failed", self._find_error, table)
        if self._scan_capped:
            hist.notes.append("the main file's copy was not found in the first %s rows "
                              "scanned (limit history_scan_rows): raise it to look further"
                              % format(limits.get("history_scan_rows"), ","))
        if main_hit is not None:
            rowid, row, page, off = main_hit
            obs.append(Version(row, "main", MAIN_FILE, page, off))
        # committed states after each WAL commit (rowid tables): where deletions happened
        markers = self._presence_markers(info, key, main_hit is not None, cancel)
        obs.extend(markers)
        obs.sort(key=lambda v: (_ERA_RANK[v.era], v.order, 0 if v.present else 1))
        self._build(info, hist, obs)
        cur = self.session.row(table, locator)
        hist.current = self._declared(info, cur.values) if cur is not None else None
        if hist.current is None:
            hist.deleted = bool(hist.versions)
            if hist.versions and hist.versions[-1].present:
                last = hist.versions[-1]
                hist.versions.append(Version(None, last.era, last.file, present=False,
                                             note="not in the current state", order=last.order))
        else:
            ck = row_key(info, hist.current, with_alias=True)
            for v in reversed(hist.versions):
                if v.present:
                    v.current = row_key(info, v.values, with_alias=True) == ck
                    break
        return hist

    def _declared(self, info, values):
        visible = self.session.visible_columns(info.name)
        return [values[visible.index(c)] if c in visible else None for c in info.column_names]

    def _main_root(self, info):
        """The table's root page in the main file's own schema (normally the same)."""
        for e in self.fx.main_entries():
            if e.type == "table" and e.name == info.name and isinstance(e.rootpage, int):
                return e.rootpage
        return info.root_page

    def _find(self, info, view, key, root, hint_pages=(), cancel=None):
        """(rowid, row, page, offset) of the key in a view's tree, or None."""
        if root is None or root < 1:
            return None
        reader = BTreeReader(view, self.issues)
        enc = self.session.pager.encoding
        if key[0] == "rowid" and not info.without_rowid:
            try:
                hit = reader.find_rowid(root, key[1])
            except Exception as e:      # noqa: BLE001 - said in the history's notes
                self._find_error = str(e) or e.__class__.__name__
                return None
            if hit is None:
                return None
            values, problem = decode_record_lenient(hit[1], enc)
            row, _f = info.record_to_row(hit[0], values, damaged=bool(problem))
            return hit[0], row, hit[2].page, hit[2].offset
        for n in sorted(p for p in hint_pages if p):
            data = view.safe_page(n)
            if data is None:
                continue
            for rowid, row, off in self._rows_of_image(data, n, info, view):
                if self.key_of(info, rowid, row) == key:
                    return rowid, row, n, off
        count = 0
        cap = limits.get("history_scan_rows")
        try:
            for payload, ref in reader.iter_index(root):
                count += 1
                if count > cap:
                    self._scan_capped = True        # said in the history's notes
                    return None
                if cancel is not None and count % 4096 == 0 and cancel():
                    return None
                values, problem = decode_record_lenient(payload, enc)
                row, _f = info.record_to_row(None, values, damaged=bool(problem))
                if self.key_of(info, None, row) == key:
                    return None, row, ref.page, ref.offset
        except Exception as e:          # noqa: BLE001 - said in the history's notes
            self._find_error = str(e) or e.__class__.__name__
            return None
        return None

    def _presence_markers(self, info, key, in_main, cancel):
        """Deletion markers (and re-insertions) from the committed state after each WAL
        commit. Only rowid tables (a rowid seek per commit)."""
        fx = self.fx
        tl = fx.timeline
        if info.without_rowid or not tl.commit_ends or key[0] != "rowid":
            return []
        out = []
        present = in_main
        for g in range(len(tl.commit_ends)):
            if cancel is not None and g % 256 == 0 and cancel():
                break
            snap = WalSnapshotView(fx.main, tl, g)
            try:
                hit = BTreeReader(snap, self.issues).find_rowid(info.root_page, key[1])
            except Exception:
                hit = None
            now = hit is not None
            end = tl.commit_ends[g]
            if present and not now:
                out.append(Version(None, "committed", WAL_FILE, frame=end,
                                   frame_state=fx.session.wal.frames[end].state, commit_group=g,
                                   present=False, order=end,
                                   note="gone after WAL commit %d (frame %d)" % (g, end)))
            present = now
        return out

    def _build(self, info, hist, obs):
        prev_values = None
        gap = False
        for v in obs:
            if not v.present:
                if hist.versions and hist.versions[-1].present:
                    hist.versions.append(v)
                    gap = True
                continue
            key = tuple(value_key(x) for x in v.values)
            last = hist.versions[-1] if hist.versions else None
            if last is not None and last.present and \
                    tuple(value_key(x) for x in last.values) == key:
                last.copies.append((v.file, v.frame, v.frame_state, v.page, v.offset))
                continue
            if prev_values is not None:
                v.changed = [c for c, a, b in zip(info.column_names, prev_values, v.values)
                             if value_key(a) != value_key(b)]
                why = reuse_reason(info, prev_values, v.values)
                if gap:
                    hist.reuse.append("deleted, then present again (%s)" % v.where())
                elif why:
                    hist.reuse.append("%s (%s)" % (why, v.where()))
            gap = False
            prev_values = v.values
            hist.versions.append(v)

    # -- whole table ---------------------------------------------------------
    def summary(self, table, cancel=None):
        info = self._info(table)
        fx = self.fx
        out = HistorySummary(table)
        seen = {}                                   # key -> [(rank, order, values)]
        locs = {}

        def add(rank, order, rowid, row):
            k = self.key_of(info, rowid, row)
            seen.setdefault(k, []).append((rank, order, row))
            locs.setdefault(k, self.locator(info, rowid, row))

        wal = self.session.wal
        wal_pages = set()
        for fr in self._frames_of(info):
            if cancel is not None and cancel():
                out.complete = False
                break
            out.frames_read += 1
            wal_pages.add(fr.page_no)
            view = AsOfFrameView(fx.main, fx.timeline, fr.index)
            for rowid, row, _off in self._rows_of_image(wal.page_data(fr.index), fr.page_no,
                                                        info, view):
                add(_ERA_RANK[self._era(fr)], fr.index, rowid, row)
        journal = fx.journal()
        journal_pages = set()
        jv = fx.journal_view() if journal is not None else None
        if jv is not None:
            owner = self.page_owner()
            for n in journal.pages():
                if owner.get(n) == info.name:
                    journal_pages.add(n)
                    for rowid, row, _off in self._rows_of_image(journal.page(n), n, info, jv):
                        add(_ERA_RANK["journal"], n, rowid, row)
        wal_keys = set(seen)
        for n in sorted(wal_pages | journal_pages):
            data = fx.main.safe_page(n)
            if data is None:
                continue
            for rowid, row, _off in self._rows_of_image(data, n, info, fx.main):
                add(_ERA_RANK["main"], 0, rowid, row)
        live = self._live_lookup(info)
        for k, items in seen.items():
            items.sort(key=lambda x: (x[0], x[1]))
            versions, prev = [], None
            reuse = []
            for rank, order, row in items:
                vk = tuple(value_key(x) for x in row)
                if prev is not None and vk == prev[0]:
                    continue
                if prev is not None:
                    why = reuse_reason(info, prev[1], row)
                    if why:
                        reuse.append(why)
                versions.append(vk)
                prev = (vk, row)
            kh = KeyHistory()
            kh.locator = locs[k]
            kh.versions = len(versions)
            kh.deleted = not live(k, locs[k])
            kh.reuse = reuse
            kh.first, kh.last = ERAS[items[0][0]], ERAS[items[-1][0]]
            kh.wal_only = k in wal_keys and all(r >= _ERA_RANK["committed"] for r, _o, _v in items)
            out.keys.append(kh)
        out.keys.sort(key=lambda kh: _sort_key(kh.locator))
        return out

    def _live_lookup(self, info):
        """live(key, locator) -> bool for the current state."""
        reader = BTreeReader(self.session.pager, self.issues)
        if not info.without_rowid:
            def live(k, loc):
                try:
                    return reader.find_rowid(info.root_page, k[1]) is not None
                except Exception:
                    return False
            return live

        def live_pk(k, loc):
            try:
                return self.session.row(info.name, loc) is not None
            except Exception:
                return False
        return live_pk


def _children(data, page_no, usable):
    try:
        h = parse_page_header(data, page_no)
    except Exception:
        return []
    if h.right_child is None:
        return []
    kids = [h.right_child]
    for off in cell_pointers(data, h, usable):
        if off + 4 <= usable:
            kids.append(int.from_bytes(data[off:off + 4], "big"))
    return kids


def _sort_key(loc):
    if loc.kind == "rowid":
        return (0, loc.value if isinstance(loc.value, int) else 0, ())
    return (1, 0, tuple(value_key(v) for v in loc.value))
