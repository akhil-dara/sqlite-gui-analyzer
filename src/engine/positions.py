"""Position indexes: windows of a large table read in milliseconds at any position.

LIMIT/OFFSET makes SQLite step over every row before the window, so a window near the end of a
table of millions of rows takes seconds. A PositionIndex, built once in the background by
streaming the table view (table, order, filter) in its order, records where rows are:

  checkpoints   every K-th row's sort key (K adapts so at most `checkpoints_max` are kept). A
                window at position p then seeks to the checkpoint before p and steps over at
                most K rows: 'WHERE key >= checkpoint ORDER BY key LIMIT n OFFSET small'.
  position map  for a filtered view, or an order SQLite has no index for (such a seek would
                still scan the table), the rowid of every row in order, up to
                `position_map_rows` rows (8 bytes a row): a window is then 'rowid IN (...)'.

An order is split into phases so no sort key is ever NULL: sorting ascending by a column puts
its NULL rows first (by rowid), then the others by (column, rowid); descending the other way
round. Keys are compared as row values ('(a, b) >= (?, ?)') where SQLite supports them and all
terms run the same way, else as the equivalent nested OR. A checkpoint key whose type the
column's affinity could convert when compared (text that looks like a number in a numeric
column, a number in a TEXT column, text that is not valid in the database encoding) would not
compare as it sorts: such an index is not used and windows are read with OFFSET, as before.
"""

import bisect
import sqlite3
from array import array

from . import uiyield
from .fileformat.record import InvalidText
from .schema import column_affinity, quote_ident

ROW_VALUES = sqlite3.sqlite_version_info >= (3, 15, 0)
FETCH = 20000               # rows per fetchmany while building
PARTIAL_REACH = 4           # a partly built index serves windows this many K past its end


class Term(object):
    __slots__ = ("expr", "desc", "affinity")

    def __init__(self, expr, desc, affinity):
        self.expr, self.desc, self.affinity = expr, bool(desc), affinity


class Phase(object):
    __slots__ = ("where", "terms")

    def __init__(self, where, terms):
        self.where, self.terms = where, terms

    def order_sql(self):
        return ", ".join("%s %s" % (t.expr, "DESC" if t.desc else "ASC") for t in self.terms)


class OrderPlan(object):
    """How a table is ordered for a browse: the ORDER BY clause and its NULL-free phases."""

    def __init__(self, order_sql, phases, rowid):
        self.order_sql, self.phases, self.rowid = order_sql, phases, rowid


def _tiebreak(t, desc):
    """Terms that make the order total: the rowid, or the PRIMARY KEY in its index order."""
    if not t.without_rowid:
        # rowid_name is one of the literal aliases rowid/_rowid_/oid, never a user column.
        return [Term(t.rowid_name, desc, "INTEGER")]
    terms = []
    for ci, (pk_desc, coll) in zip(t.pk_columns, t.pk_index_order):
        col = t.columns[ci]
        expr = quote_ident(col.name) + (" COLLATE " + quote_ident(coll) if coll else "")
        terms.append(Term(expr, bool(pk_desc) != bool(desc), column_affinity(col.decl_type)))
    return terms


def order_plan(t, order_by, desc):
    """The OrderPlan of a table (None for views, virtual tables and tables without a usable
    key). Sorted: by the column, then by the rowid (or primary key), descending both ways
    for desc; natural: the rowid or primary key order (reversed for desc)."""
    if t.kind != "table" or (not t.without_rowid and t.rowid_name is None):
        return None
    if t.without_rowid and not t.pk_columns:
        return None
    tie = _tiebreak(t, desc)
    if not order_by:
        phase = Phase("", tie)
        return OrderPlan(phase.order_sql(), [phase], None if t.without_rowid else t.rowid_name)
    col = next((c for c in t.columns if c.name == order_by), None)
    if col is None:
        return None
    c = quote_ident(order_by)
    sort = Term(c, desc, column_affinity(col.decl_type))
    nulls = Phase("%s IS NULL" % c, tie)
    values = Phase("%s IS NOT NULL" % c, [sort] + tie)
    phases = [values, nulls] if desc else [nulls, values]
    order = ", ".join(["%s %s" % (c, "DESC" if desc else "ASC")] +
                      ["%s %s" % (x.expr, "DESC" if x.desc else "ASC") for x in tie])
    return OrderPlan(order, phases, None if t.without_rowid else t.rowid_name)


def affinity_safe(affinity, v):
    """True when comparing a column of this affinity with the bound value v cannot convert v
    (so the comparison orders v as ORDER BY does)."""
    if v is None or isinstance(v, (InvalidText, bool)):
        return False
    if affinity in ("INTEGER", "REAL", "NUMERIC") and isinstance(v, str):
        try:
            float(v)
        except ValueError:
            return True
        return False
    if affinity == "TEXT" and isinstance(v, (int, float)):
        return False
    return True


def _cmp(terms, vals, strict):
    """(sql, params) true for rows at or after the key `vals` in the order of `terms`
    (strict: only the rows before it)."""
    n = len(terms)
    same = all(t.desc == terms[0].desc for t in terms)
    if strict:
        op = ">" if terms[0].desc else "<"
    else:
        op = "<=" if terms[0].desc else ">="
    if n == 1 or (same and ROW_VALUES):
        if n == 1:
            return "%s %s ?" % (terms[0].expr, op), [vals[0]]
        return ("(%s) %s (%s)" % (", ".join(t.expr for t in terms), op, ", ".join("?" * n)),
                list(vals))
    # nested: e0 after v0 OR (e0 = v0 AND (e1 after v1 OR (e1 = v1 AND ...)))
    sql, params = None, []
    for i in range(n - 1, -1, -1):
        t = terms[i]
        if strict:
            after = "<" if not t.desc else ">"
        else:
            after = ">" if not t.desc else "<"
        if sql is None:
            last = after if strict else after + "="
            sql, params = "%s %s ?" % (t.expr, last), [vals[i]]
        else:
            sql = "(%s %s ? OR (%s = ? AND %s))" % (t.expr, after, t.expr, sql)
            params = [vals[i], vals[i]] + params
    lead = terms[0]         # a plain range on the first term lets SQLite seek an index
    bound = ("<=" if lead.desc else ">=") if not strict else (">=" if lead.desc else "<=")
    return "%s %s ? AND %s" % (lead.expr, bound, sql), [vals[0]] + params


class PositionIndex(object):
    """Where the rows of one table view are (see the module docstring).

    Built on one thread by build(); read by others with window() at any time: the lists only
    grow (a thinning replaces them whole), so a reader sees a consistent prefix.
    """

    def __init__(self, key, table_sql, plan, every, max_checkpoints, map_limit, dense):
        self.key, self.plan = key, plan
        self._table_sql = table_sql     # the quoted table name
        self.every = max(1, every)
        self.max_checkpoints = max(2, max_checkpoints)
        self.map_limit = map_limit
        self.dense = array("q") if dense else None
        self.seekable = [True] * len(plan.phases)
        self._cp = ([], [])             # (positions, (phase, key values)) of the checkpoints
        self.phase_starts = [None] * len(plan.phases)
        self.count = 0                  # rows seen so far
        self.complete = False
        self.usable = True              # False: a checkpoint key could compare unlike it sorts
        self.map_skipped = False        # the view had more rows than map_limit
        self.error = None

    # -- building ----------------------------------------------------------------------
    def _add_checkpoint(self, pos, phase, vals):
        terms = self.plan.phases[phase].terms
        for term, v in zip(terms, vals):
            if not affinity_safe(term.affinity, v):
                self.usable = False
        positions, entries = self._cp
        entries.append((phase, vals))
        positions.append(pos)           # after its entry: a reader never sees one without it
        if len(positions) > self.max_checkpoints:
            self._thin()

    def _thin(self):
        positions, entries = self._cp
        every = self.every * 2
        starts = set(p for p in self.phase_starts if p is not None)
        keep = [i for i, p in enumerate(positions) if p % every == 0 or p in starts]
        self._cp = ([positions[i] for i in keep], [entries[i] for i in keep])
        self.every = every

    def build(self, run_query, base_where, base_params, explain, cancel=None):
        """Stream the view in order and record its positions. run_query(sql, params) returns a
        cursor; explain(sql, params) the EXPLAIN QUERY PLAN detail strings. cancel() -> True
        stops (the index stays incomplete). Returns True when complete."""
        pos = 0
        for pi, phase in enumerate(self.plan.phases):
            where = " AND ".join(w for w in (base_where, phase.where) if w)
            sql = "SELECT %s FROM %s%s ORDER BY %s" % (
                ", ".join(t.expr for t in phase.terms), self._table_sql,
                (" WHERE " + where) if where else "", phase.order_sql())
            try:
                plan = explain(sql, base_params)
            except sqlite3.Error:
                plan = ["TEMP B-TREE"]
            self.seekable[pi] = not base_where and not any("TEMP B-TREE" in d for d in plan)
            self.phase_starts[pi] = pos
            first = True
            cur = run_query(sql, base_params)
            try:
                while True:
                    if cancel is not None and cancel():
                        return False
                    uiyield.pause()
                    chunk = cur.fetchmany(FETCH)
                    if not chunk:
                        break
                    dense = self.dense
                    if dense is not None:
                        if len(dense) + len(chunk) > self.map_limit:
                            self.dense = dense = None
                            self.map_skipped = True
                        else:
                            dense.extend(r[-1] for r in chunk)
                    every = self.every
                    for r in chunk:
                        if first or pos % every == 0:
                            self._add_checkpoint(pos, pi, tuple(r))
                            every = self.every
                            first = False
                        pos += 1
                    self.count = pos
            finally:
                try:
                    cur.close()
                except sqlite3.Error:
                    pass
        self.count = pos
        self.complete = True
        return True

    # -- reading ---------------------------------------------------------------------------
    def total(self):
        """Rows of the view once complete, else None."""
        return self.count if self.complete else None

    def window(self, p, limit, select, base_where, base_params, run, lead):
        """Raw rows p .. p+limit-1 of the view, or None when this index cannot serve them
        (not built that far, or not usable). run(sql, params) returns a list of rows; select
        is the SELECT list, whose first `lead` values are the rowid (lead 1) or none."""
        got = self._window(p, limit, select, base_where, base_params, run, lead)
        if got is None:
            self.missed += 1
        else:
            self.served += 1
        return got

    served = missed = 0         # windows served / left to OFFSET (for tests and measurements)

    def _window(self, p, limit, select, base_where, base_params, run, lead):
        if p < 0 or limit <= 0:
            return None
        if self.complete and p >= self.count:
            return []
        dense = self.dense
        if dense is not None and lead == 1 and self.plan.rowid is not None:
            have = len(dense)
            if self.complete or p + limit <= have:
                return self._window_dense(dense[p:p + limit], select, run)
        if not self.usable:
            return None
        return self._window_keys(p, limit, select, base_where, base_params, run)

    def _window_dense(self, ids, select, run):
        if not ids:
            return []
        ids = list(ids)
        rows = {}
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            for r in run("SELECT %s FROM %s WHERE %s IN (%s)"
                         % (select, self._table_sql, self.plan.rowid, ",".join("?" * len(part))),
                         part):
                rows[r[0]] = r
        out = [rows.get(i) for i in ids]
        return None if any(r is None for r in out) else out

    def _window_keys(self, p, limit, select, base_where, base_params, run):
        positions, entries = self._cp
        n = min(len(positions), len(entries))
        if n == 0:
            return None
        i = bisect.bisect_right(positions, p, 0, n) - 1
        if i < 0:
            return None
        cp_pos = positions[i]
        phase_i, vals = entries[i]
        if not self.complete and i == n - 1 and p - cp_pos > PARTIAL_REACH * self.every:
            return None
        out = []
        skip = p - cp_pos
        want = limit
        while True:
            phase = self.plan.phases[phase_i]
            conds, params = [], list(base_params)
            if base_where:
                conds.append(base_where)
            if phase.where:
                conds.append(phase.where)
            if vals is not None:
                c, ps = _cmp(phase.terms, vals, False)
                conds.append(c)
                params.extend(ps)
                if not self.seekable[phase_i]:
                    # bound the scan: the rows needed all lie before a later checkpoint
                    j = bisect.bisect_left(positions, p + limit, 0, n)
                    if j < n and entries[j][0] == phase_i:
                        c, ps = _cmp(phase.terms, entries[j][1], True)
                        conds.append(c)
                        params.extend(ps)
            sql = "SELECT %s FROM %s%s ORDER BY %s LIMIT ? OFFSET ?" % (
                select, self._table_sql, (" WHERE " + " AND ".join(conds)) if conds else "",
                phase.order_sql())
            got = run(sql, params + [want, skip])
            out.extend(got)
            want = limit - len(out)
            if want <= 0 or phase_i + 1 >= len(self.plan.phases):
                return out
            # this phase ended: the next rows come from the start of the next one
            nxt = self.phase_starts[phase_i + 1]
            reached = p + len(out)
            if nxt is None or reached < nxt:
                return None             # not built that far, or the table changed under us
            phase_i += 1
            vals = None
            skip = reached - nxt

    def checkpoint_count(self):
        return len(self._cp[0])

    def memory_bytes(self):
        """Rough bytes held: 8 per mapped row, ~120 per checkpoint."""
        dense = self.dense
        return (len(dense) * 8 if dense is not None else 0) + 120 * len(self._cp[0])
