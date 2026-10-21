"""Is a recovered row still live? Compares recovered rows with the current state.

status(template, rowid, row) returns
  'live'           an identical row exists now (a copy left behind by a page split or move),
  'prior_version'  the row's key is live but its values differ (an older version),
  None             the key is not live (deleted), or the table no longer exists.
Only the values a record stores are compared (not VIRTUAL generated columns, and not the rowid
alias, which is stored as NULL). Rows are looked up by rowid when it is known; otherwise the
table's rows are hashed once (up to the limit live_hash_rows rows per table: a larger table
is listed in `capped`, its rows are not compared, and the carve result says so).
"""

import hashlib
import struct

from .. import limits
from ..fileformat.btree import BTreeReader
from ..fileformat.record import decode_record_lenient
from ..issues import IssueLog
from .provenance import value_key

LIVE_HASH_CAP = "live_hash_rows"        # engine.limits name
SEEKS_BEFORE_MAP = 200                  # rowid seeks into one table before its row map is made


def row_key(info, row, with_alias=False):
    """Hashable identity of the stored values of a row given in declared column order."""
    alias = None if with_alias else info.rowid_alias
    return tuple(value_key(row[ci]) if ci < len(row) else (0, None)
                 for ci in info.storage_order if ci != alias)


def key_digest(key):
    """A 16-byte digest of a row key (a tuple of value_key pairs) with the tuple's equality:
    the tag, the kind of the value and its bytes each go in, length-prefixed - text 'x' and
    a blob b'x' differ, bytes and bytearray agree. The maps of live rows keep these instead
    of the values themselves (a phone database's tables hold millions of rows); a chance
    collision (2^-128) is far below anything a row could tell."""
    h = hashlib.blake2b(digest_size=16)
    for tag, v in key:
        if v is None:
            kind, raw = b"n", b""
        elif isinstance(v, (bytes, bytearray, memoryview)):
            kind, raw = b"b", bytes(v)
        elif isinstance(v, bool):
            kind, raw = b"i", str(int(v)).encode()
        elif isinstance(v, int):
            kind, raw = b"i", str(v).encode()
        else:
            kind, raw = b"s", str(v).encode("utf-8", "surrogatepass")
        h.update(struct.pack(">BcI", tag & 0xFF, kind, len(raw)))
        h.update(raw)
    return h.digest()


def _order_key(v, encoding):
    """A sort key in SQLite's order of index values (BINARY collation): NULL, then numbers
    by value, then text by its encoded bytes, then BLOBs by bytes; None for a value of any
    other type (not compared)."""
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (1, v)
    if isinstance(v, str):
        try:
            return (2, v.encode(encoding or "utf-8", "surrogatepass"))
        except (LookupError, UnicodeError):
            return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return (3, bytes(v))
    return None


class LiveIndex(object):
    def __init__(self, session, cap=None, cancel=None):
        self.session = session
        self.cap = limits.get(LIVE_HASH_CAP) if cap is None else cap
        self.cancel = cancel
        self.issues = IssueLog()
        self._reader = BTreeReader(session.pager, self.issues)
        self._sets = {}          # table -> (row key digests, pk digests) or None when too big
        self._near = {}          # table -> {(column slot, key without it): [rowids]} or None
        self._seeks = {}         # table -> rowid look-ups so far
        self._maps = {}          # table -> {rowid: row key digest} or None (too big / unreadable)
        self._by_key = {}        # table -> {row key digest: rowid or [rowids]} or None
        self._uniques = {}       # table -> [(column indices, names, {key digest: rowid})]
        self._master = None
        self.capped = set()

    def status(self, tpl, rowid, row):
        if tpl.kind == "master":
            return self._master_status(row)
        if tpl.dropped:
            return None
        info = self.session.schema.get(tpl.name)
        if info is None or info.root_page != tpl.info.root_page:
            return None
        if not info.without_rowid and rowid is not None:
            # many look-ups in one table (every cell of WAL-replaced pages): one pass makes a
            # rowid -> row map, and a row found identical there is live; anything else is
            # decided by the seek as before (same answers, a fraction of the seeks)
            seeks = self._seeks.get(info.name, 0) + 1
            self._seeks[info.name] = seeks
            if seeks > SEEKS_BEFORE_MAP:
                m = self._rowid_map(info)
                if m is not None:
                    got = m.get(rowid)
                    if got is None:
                        return None             # the map holds every live rowid: not live
                    if got == key_digest(row_key(info, row)):
                        return "live"
            return self._by_rowid(info, rowid, row)
        entry = self._table_set(info)
        if entry is None:
            # over live_hash_rows: the row set was not kept, but a UNIQUE index of the table
            # finds the one live row with the record's key on disk (a few pages read); live
            # when that row is identical
            if not info.without_rowid and any(
                    self._by_rowid(info, rid, row) == "live"
                    for rid, _names in self._seek_unique(info, row)):
                return "live"
            return None
        keys, pks = entry
        if key_digest(row_key(info, row)) in keys:
            return "live"
        if info.without_rowid and info.pk_columns:
            pk = key_digest(tuple(value_key(row[i]) for i in info.pk_columns))
            if pk in pks:
                return "prior_version"
        return None

    def infer_rowid(self, tpl, row):
        """A recovered row of a rowid table whose rowid was lost (a freeblock header wrote
        over it): (rowid, column) of the ONE live row holding the same values in every stored
        column but one - an older version of that row - or None: no such row, several, too
        few values to tell (at least 3 columns, 3 of them neither NULL, 0 nor empty, and one
        of the agreeing columns nearly unique in the table - an id, not a price or a flag),
        a difference in a nearly unique column (another row, not a changed one),
        or a table too large to index (a key per column per row, live_hash_rows keys in
        all)."""
        if tpl.kind == "master" or tpl.dropped:
            return None
        info = self.session.schema.get(tpl.name)
        if info is None or info.without_rowid or info.root_page != tpl.info.root_page:
            return None
        cols = [ci for ci in info.storage_order if ci != info.rowid_alias]
        if len(cols) < 3:
            return None
        key = [value_key(row[ci]) if ci < len(row) else (0, None) for ci in cols]
        if sum(1 for k in key if k[1] not in (None, 0, "", b"", "0.0")) < 3:
            return None
        built = self._near_index(info, cols)
        if not built:
            return None
        near, selective = built
        hits = {}
        for j in range(len(cols)):
            if j in selective:
                continue                # differing in a nearly unique column: another row
            for rid in near.get((j, tuple(key[:j] + key[j + 1:])), ()):
                hits.setdefault(rid, j)
        if len(hits) != 1:
            return None
        rid, j = next(iter(hits.items()))
        if not any(slot in selective for slot in range(len(cols)) if slot != j):
            return None                 # agreeing only on common values: not evidence
        return rid, info.columns[cols[j]].name

    def _near_index(self, info, cols):
        if info.name in self._near:
            return self._near[info.name]
        near, entries, rows = {}, 0, 0
        distinct = [set() for _c in cols]
        try:
            s = self.session
            names = [c.name for c in info.columns]
            visible = s.visible_columns(info.name)
            pos = [visible.index(n) if n in visible else None for n in names]
            for n, r in enumerate(s.iter_rows(info.name)):
                if n % 1024 == 0 and self.cancel is not None and self.cancel():
                    return None             # stopped: not remembered
                loc = getattr(r, "locator", None)
                if getattr(loc, "kind", None) != "rowid":
                    continue
                vals = r.values
                row = [vals[p] if p is not None and p < len(vals) else None for p in pos]
                key = [value_key(row[ci]) if ci < len(row) else (0, None) for ci in cols]
                rows += 1
                for j in range(len(cols)):
                    distinct[j].add(key[j])
                    lst = near.setdefault((j, tuple(key[:j] + key[j + 1:])), [])
                    if len(lst) < 2:            # two are enough to know it is not one
                        lst.append(loc.value)
                entries += len(cols)
                if entries > self.cap:
                    near = None
                    break
        except Exception as e:
            self.issues.add("live_rows_failed", str(e), info.name, "info")
            near = None
        # nearly unique columns: at least half as many distinct values as rows (10 rows min)
        selective = set(j for j, d in enumerate(distinct) if rows >= 10 and 2 * len(d) >= rows)
        built = (near, selective) if near else None
        self._near[info.name] = built
        return built

    def _rowid_map(self, info):
        """{rowid: row key digest} of the live rows, None when the table has more rows than
        live_hash_rows or cannot be read (the seeks then go on). Built by _scan."""
        if info.name not in self._maps:
            self._scan(info)
        return self._maps.get(info.name)

    def rowids_with_values(self, tpl, rowid, row, most=3):
        """Live rowids (at most `most`, ascending) of tpl's table whose stored values equal
        row's in every column - for a deleted row the same content inserted again under
        another rowid. [] when there are none, when the row holds fewer than 2 values that
        are neither NULL nor empty (common values match by chance), or when the table is over
        live_hash_rows (not compared: no claim)."""
        if tpl.kind == "master" or tpl.dropped:
            return []
        info = self.session.schema.get(tpl.name)
        if info is None or info.without_rowid or info.root_page != tpl.info.root_page:
            return []
        key = row_key(info, row)
        if sum(1 for k in key if k[1] not in (None, "", b"")) < 2:
            return []
        inv = self._by_key.get(info.name, False)
        if inv is False:
            m = self._rowid_map(info)
            inv = None
            if m is not None:
                inv = {}
                for rid, k in m.items():
                    got = inv.get(k)
                    if got is None:
                        inv[k] = rid                # one rowid (the usual case): no list
                    elif isinstance(got, list):
                        got.append(rid)
                    else:
                        inv[k] = [got, rid]
            elif info.name not in self._maps:
                return []               # stopped: not remembered, a later call retries
            self._by_key[info.name] = inv
        if not inv:
            return []
        got = inv.get(key_digest(key), ())
        got = got if isinstance(got, list) else [got] if got != () else []
        return sorted(r for r in got if r != rowid)[:most]

    def replaced_by(self, tpl, rowid, row):
        """(live rowid, key column names) when a UNIQUE key of tpl's table (an index on plain
        columns, binary collation, no WHERE) holds row's key values under another rowid now:
        the row was replaced (INSERT OR REPLACE deletes it and inserts anew) - an older
        version. None when no such key, a key value is NULL, or the table is over
        live_hash_rows (not compared: no claim)."""
        if tpl.kind == "master" or tpl.dropped:
            return None
        info = self.session.schema.get(tpl.name)
        if info is None or info.without_rowid or info.root_page != tpl.info.root_page:
            return None
        maps = self._unique_maps(info)
        for cids, names, keys in maps:
            if any(c >= len(row) or row[c] is None for c in cids):
                continue
            rid = keys.get(key_digest(tuple(value_key(row[c]) for c in cids)))
            if rid is not None and rid != rowid:
                return rid, names
        if not maps and info.name in self.capped:
            for rid, names in self._seek_unique(info, row):     # over the limit: on disk
                if rid != rowid:
                    return rid, names
        return None

    def _seek_unique(self, info, row):
        """[(live rowid, key column names)] of the rows that the table's plain UNIQUE indexes
        hold under row's key values, found by seeking each index B-tree (the pages on the way
        to the key only) - for tables over live_hash_rows, whose keys are not kept in memory.
        A key with a NULL, a DESC column or a value of an unexpected type is not sought; a
        damaged index finds nothing (the caller still reads the row itself to compare)."""
        out = []
        try:
            specs = self._unique_specs(info)
        except Exception:                   # noqa: BLE001 - no index usable: no claim
            return out
        enc = self.session.pager.encoding
        for cids, names, root, descs in specs:
            if not root or any(descs) or any(c >= len(row) or row[c] is None for c in cids):
                continue
            want = [_order_key(row[c], enc) for c in cids]
            if any(w is None for w in want):
                continue

            def compare(payload, want=want):
                v = decode_record_lenient(payload, enc)[0]
                got = [_order_key(x, enc) for x in v[:len(want)]]
                if len(got) < len(want) or any(g is None for g in got):
                    return -1
                return (got > want) - (got < want)
            try:
                for n, (payload, _ref) in enumerate(self._reader.seek_index(root, compare)):
                    if n >= 8:
                        break                   # a UNIQUE key: one entry (a damaged index more)
                    v = decode_record_lenient(payload, enc)[0]
                    if v and isinstance(v[-1], int) and not isinstance(v[-1], bool):
                        out.append((v[-1], names))
            except Exception:               # noqa: BLE001 - a damaged index: nothing found
                continue
        return out

    def _unique_maps(self, info):
        """[(column indices, names, {key digest: rowid})] of info's plain UNIQUE indexes.
        Built by _scan."""
        if info.name not in self._uniques:
            self._scan(info)
        return self._uniques.get(info.name, [])

    def _unique_specs(self, info):
        """[(column indices, names, root page, DESC flags)] of info's plain UNIQUE indexes
        (plain columns, binary collation, no WHERE, not the rowid)."""
        from .index_carve import index_specs     # here: index_carve imports this module
        out = []
        s = self.session
        for sp in index_specs(s.schema, s.schema.entries, tables=[info.name]):
            if not sp.unique or sp.where or sp.dropped:
                continue
            cols = [c for c in sp.columns if c.key]
            if not cols or any(c.expr is not None or c.cid is None or c.cid < 0 or
                               (c.collation or "BINARY").upper() != "BINARY" for c in cols):
                continue
            cids = [c.cid for c in cols]
            if info.rowid_alias in cids:
                continue                        # the rowid itself: decided by rowid already
            out.append((cids, [info.columns[c].name for c in cids], sp.root_page,
                        [c.desc for c in cols]))
        return out

    def _scan(self, info):
        """One pass over a table's live rows that makes all its maps at once: the set of row
        key digests (and primary-key digests of a WITHOUT ROWID table), the rowid map and the
        UNIQUE-key maps - each was a pass of its own, a table of millions of rows read three
        times. Each keeps its own limit (live_hash_rows) and failure as before; a stopped
        pass is not remembered (a later call retries)."""
        name = info.name
        keys, pks = set(), set()
        sets = (keys, pks)
        rowid_ok = not info.without_rowid
        rmap = {} if rowid_ok else None
        try:
            specs = self._unique_specs(info) if rowid_ok else []
            uniq = [(cids, names, {}) for cids, names, _root, _descs in specs]
        except Exception as e:
            self.issues.add("live_rows_failed", str(e), name, "info")
            uniq = []
        try:
            s = self.session
            names = [c.name for c in info.columns]
            visible = s.visible_columns(name)
            pos = [visible.index(n) if n in visible else None for n in names]
            fx = getattr(s, "__dict__", {}).get("_forensics")   # never created from here
            known = getattr(s, "_counts", {}).get(name)
            for n, r in enumerate(s.iter_rows(name)):
                if n % 1024 == 0:
                    if self.cancel is not None and self.cancel():
                        return                  # stopped: not remembered
                    if fx is not None and n % 8192 == 0:
                        fx.set_stage("Comparing with the live rows of %s" % name, n,
                                     known if isinstance(known, int) else None)
                vals = r.values
                row = [vals[p] if p is not None and p < len(vals) else None for p in pos]
                kd = None
                if sets is not None:
                    if len(keys) >= self.cap:
                        self.capped.add(name)
                        sets = None
                    else:
                        kd = key_digest(row_key(info, row))
                        keys.add(kd)
                        if info.without_rowid and info.pk_columns:
                            pks.add(key_digest(tuple(value_key(row[i])
                                                     for i in info.pk_columns)))
                loc = getattr(r, "locator", None)
                is_rowid = getattr(loc, "kind", None) == "rowid"
                if rmap is not None:
                    if len(rmap) >= self.cap:
                        self.capped.add(name)
                        rmap = None
                    elif is_rowid:
                        if kd is None:
                            kd = key_digest(row_key(info, row))
                        rmap[loc.value] = kd
                if uniq:
                    if n >= self.cap:
                        self.capped.add(name)
                        uniq = []
                    elif is_rowid:
                        for cids, _names, ukeys in uniq:
                            uk = key_digest(tuple(value_key(row[c]) for c in cids))
                            ukeys[uk] = loc.value
                if sets is None and rmap is None and not uniq:
                    break                       # every map over the limit: nothing to add
        except Exception as e:
            self.issues.add("live_rows_failed", str(e), name, "info")
            sets, rmap, uniq = None, None, []
        self._sets[name] = sets
        self._maps[name] = rmap
        self._uniques[name] = uniq

    def live_row(self, tpl, rowid):
        """The live row with this rowid in tpl's table (declared column order), or None."""
        if tpl.kind == "master" or tpl.dropped or rowid is None:
            return None
        info = self.session.schema.get(tpl.name)
        if info is None or info.without_rowid or info.root_page != tpl.info.root_page:
            return None
        try:
            hit = self._reader.find_rowid(info.root_page, rowid)
        except Exception:               # noqa: BLE001 - no live row found
            return None
        if hit is None:
            return None
        values, problem = decode_record_lenient(hit[1], self.session.pager.encoding)
        return info.record_to_row(hit[0], values, damaged=bool(problem))[0]

    def _by_rowid(self, info, rowid, row):
        try:
            hit = self._reader.find_rowid(info.root_page, rowid)
        except Exception:
            hit = None
        if hit is None:
            return None
        values, problem = decode_record_lenient(hit[1], self.session.pager.encoding)
        live_row, _flags = info.record_to_row(hit[0], values, damaged=bool(problem))
        return "live" if row_key(info, live_row) == row_key(info, row) else "prior_version"

    def _table_set(self, info):
        """(row key digests, pk digests) of the live rows, None when over live_hash_rows or
        unreadable. Built by _scan."""
        if info.name not in self._sets:
            self._scan(info)
        return self._sets.get(info.name)

    def _rows(self, info):
        """Every live row of a table in declared column order."""
        s = self.session
        names = [c.name for c in info.columns]
        visible = s.visible_columns(info.name)
        pos = [visible.index(n) if n in visible else None for n in names]
        for r in s.iter_rows(info.name):
            vals = r.values
            yield [vals[p] if p is not None and p < len(vals) else None for p in pos]

    def _master_status(self, row):
        if self._master is None:
            self._master = set()
            for e in self.session.schema.entries:
                self._master.add(tuple(value_key(v) for v in
                                       (e.type, e.name, e.tbl_name, e.rootpage, e.sql or None)))
        key = tuple(value_key(v) for v in (list(row) + [None] * 5)[:5])
        if key in self._master:
            return "live"
        # the same object with other values (e.g. an older CREATE of a live table)
        for e in self.session.schema.entries:
            if (e.type, e.name) == (row[0], row[1]):
                return "prior_version"
        return None
