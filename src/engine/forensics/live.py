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

from .. import limits
from ..fileformat.btree import BTreeReader
from ..fileformat.record import decode_record_lenient
from ..issues import IssueLog
from .provenance import value_key

LIVE_HASH_CAP = "live_hash_rows"        # engine.limits name


def row_key(info, row, with_alias=False):
    """Hashable identity of the stored values of a row given in declared column order."""
    alias = None if with_alias else info.rowid_alias
    return tuple(value_key(row[ci]) if ci < len(row) else (0, None)
                 for ci in info.storage_order if ci != alias)


class LiveIndex(object):
    def __init__(self, session, cap=None, cancel=None):
        self.session = session
        self.cap = limits.get(LIVE_HASH_CAP) if cap is None else cap
        self.cancel = cancel
        self.issues = IssueLog()
        self._reader = BTreeReader(session.pager, self.issues)
        self._sets = {}          # table -> (set of row keys, {pk: key}) or None when too big
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
            return self._by_rowid(info, rowid, row)
        entry = self._table_set(info)
        if entry is None:
            return None
        keys, pks = entry
        key = row_key(info, row)
        if key in keys:
            return "live"
        if info.without_rowid and info.pk_columns:
            pk = tuple(value_key(row[i]) for i in info.pk_columns)
            if pk in pks:
                return "prior_version"
        return None

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
        if info.name in self._sets:
            return self._sets[info.name]
        keys, pks = set(), set()
        entry = (keys, pks)
        try:
            for n, row in enumerate(self._rows(info)):
                if n % 1024 == 0 and self.cancel is not None and self.cancel():
                    return None             # stopped: not remembered, a later call retries
                if len(keys) >= self.cap:
                    self.capped.add(info.name)
                    entry = None
                    break
                keys.add(row_key(info, row))
                if info.without_rowid and info.pk_columns:
                    pks.add(tuple(value_key(row[i]) for i in info.pk_columns))
        except Exception as e:
            self.issues.add("live_rows_failed", str(e), info.name, "info")
            entry = None
        self._sets[info.name] = entry
        return entry

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
