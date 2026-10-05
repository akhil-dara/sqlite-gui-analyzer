"""Row sources for the Browse grid (the protocol is described in grid.py).

TableSource reads a table or view through the engine, one window at a time, with the sort and
filters applied by the engine (in SQL, or natively for tables SQLite cannot serve). ListSource
holds rows in memory (WAL-only tables) and applies the same filter rules in Python.

Both also serve the grid's column filter popover (colfilter.py) through optional methods that
run on the grid's filter worker thread: distinct_values() (the value checklist),
count_matching() / estimate_matching() (the live count), nth_value() (top N) and date_bins()
(the date histogram). Each takes a Filter of the OTHER columns' filters (or None) and a
cancel() -> True that stops a scan made in Python; an SQL read is stopped by interrupt().
"""

import sqlite3

from database import RID
from engine import timeline as tl
from engine.backends import Filter
from engine.filters import parse_words
from engine.schema import quote_ident
from engine.session import is_interrupt, sort_key


class TableSource(object):
    """A table or view of a database.DB; rows() runs on the grid's worker thread."""

    threaded = True

    def __init__(self, db, table, total=None):
        self.db, self.table = db, table
        self._cols = [RID] + [name for name, _type in db.columns(table)]
        self.order, self.desc, self.flt = None, False, None
        self.total = total          # rows of the table, when known
        self.count = total          # rows passing the current filter, when known
        self.note = ""              # the engine's note for the last window read

    def columns(self):
        return list(self._cols)

    def row_count(self):
        return self.count

    def rows(self, start, count):
        _cols, rows, note = self.db.browse_window(self.table, start, count, self.order,
                                                  self.desc, self.flt)
        self.note = note
        return [(r, r.flags) for r in rows]

    def sort(self, column, desc):
        self.order = None if column in (None, RID) else column
        self.desc = bool(desc)

    def set_filters(self, col_exprs, global_text):
        flt = Filter(col_exprs=col_exprs, words=parse_words(global_text))
        self.flt = flt if flt else None
        self.count = self.total if self.flt is None else None

    @property
    def filtered(self):
        return self.flt is not None

    def count_rows(self, cancel=None):
        """(filter, count) for the filter in force now; slow: call it on a worker thread.
        A filtered view is counted by indexing its positions (one pass gives both)."""
        flt, order, desc = self.flt, self.order, self.desc
        if flt is None:
            return flt, self.db.count(self.table)
        n = self.db.build_positions(self.table, order, desc, flt, cancel)
        if n is None:
            n = self.db.count_filtered(self.table, flt)
        return flt, n

    def build_positions(self, cancel=None):
        """Index the positions of the view in force now (see engine.positions), so windows
        anywhere in it read fast; slow: call it on a worker thread."""
        return self.db.build_positions(self.table, self.order, self.desc, self.flt, cancel)

    def set_count(self, flt, n):
        """Record a count_rows() result, unless the filter changed meanwhile."""
        if flt is None:
            self.total = n
        if flt is self.flt:
            self.count = n

    def iter_rows(self):
        """Every row passing the filter, in the grid's order, as [locator, values...]."""
        return self.db.iter_filtered(self.table, self.flt, self.order, self.desc)

    def iter_all(self):
        """Every row of the table, filters ignored, as [locator, values...]."""
        return self.db.iter_rows(self.table)

    def release_thread(self):
        session = self.db.session
        if session is not None:
            session.release_thread_connection()

    def interrupt(self, thread):
        """Stop the read running on the grid worker `thread` (the view moved on)."""
        self.db.interrupt(thread)

    @staticmethod
    def retryable(error):
        """A read stopped by interrupt() may be read again."""
        return is_interrupt(error)

    # -- column filters (worker thread) ----------------------------------------------------
    @property
    def encoding(self):
        return self.db.encoding

    def column_types(self):
        """{column: declared type} (for the filter popover's type detection)."""
        try:
            return dict(self.db.columns(self.table))
        except Exception:               # noqa: BLE001 - no declared types then
            return {}

    def _sql(self):
        """The session when SQLite serves this table or view, else None."""
        s = self.db.session
        try:
            return s if s is not None and s.source(self.table) == "sql" else None
        except KeyError:
            return None

    def _where(self, session, flt, extra=()):
        """(' WHERE ...', params) of a Filter plus extra SQL conditions ('' when none)."""
        if flt:
            where, params = flt.where_sql(session.visible_columns(self.table), session.encoding)
        else:
            where, params = "", []
        parts = (["(%s)" % where] if where else []) + list(extra)
        return (" WHERE " + " AND ".join(parts)) if parts else "", list(params)

    def _native_rows(self, flt):
        """Rows passing flt as the engine reads them ([locator, values...], table order)."""
        return self.db.iter_filtered(self.table, flt)

    def distinct_values(self, column, flt, limit, scan_rows, cancel=None):
        """colfilter.Distinct of a column over the rows passing flt: at most `limit` values,
        most frequent first, counted over at most `scan_rows` rows (SQL GROUP BY; tables
        SQLite cannot read are read natively)."""
        import colfilter
        s = self._sql()
        if s is not None:
            try:
                return self._sql_distinct(s, column, flt, limit, scan_rows)
            except sqlite3.Error as e:
                if is_interrupt(e):
                    raise
        return colfilter.distinct_from_rows(self._native_rows(flt), self._cols.index(column),
                                            limit, scan_rows, cancel)

    def _sql_distinct(self, s, column, flt, limit, scan_rows):
        import colfilter
        q, t = quote_ident(column), quote_ident(self.table)
        where, params = self._where(s, flt)
        conn = s.conn()
        big = colfilter.BLOB_VALUE_MAX
        if self.total is not None and self.total <= scan_rows and self._binary_only(s):
            # every row is read anyway: GROUP BY the column itself, which SQLite serves from
            # an index on it (several times faster); equal INTEGER and REAL values share a
            # group then, which the result says (merged)
            x, frm, fparams = q, " FROM %s%s GROUP BY %s" % (t, where, q), params
            mixed = "MIN(typeof(%s)) <> MAX(typeof(%s))" % (q, q)
        else:
            inner = "SELECT %s AS v FROM %s%s LIMIT ?" % (q, t, where)
            x, frm = "v", " FROM (%s) GROUP BY typeof(v), v COLLATE BINARY" % inner
            fparams, mixed = params + [scan_rows], "0"
        head = ("SELECT CASE WHEN typeof(%s) = 'blob' AND length(%s) > %d THEN NULL ELSE %s END, "
                "CASE WHEN typeof(%s) = 'blob' AND length(%s) > %d THEN length(%s) END, "
                "COUNT(*) AS n, %s" % (x, x, big, x, x, x, big, x, mixed))
        try:
            rows = conn.execute(head + ", COUNT(*) OVER (), SUM(COUNT(*)) OVER ()" + frm
                                + " ORDER BY n DESC LIMIT ?", fparams + [limit]).fetchall()
            total, scanned = (rows[0][4], rows[0][5]) if rows else (0, 0)
        except sqlite3.OperationalError as e:
            if is_interrupt(e):
                raise
            # no window functions (SQLite before 3.25): count the groups and rows apart
            rows = conn.execute(head + frm + " ORDER BY n DESC LIMIT ?",
                                fparams + [limit]).fetchall()
            total, scanned = conn.execute("SELECT COUNT(*), SUM(n) FROM (SELECT COUNT(*) AS n"
                                          + frm + ")", fparams).fetchone()
        scanned = int(scanned or 0)
        total = int(total or 0)
        values = [(colfilter.LargeBlob(r[1]) if r[1] is not None else r[0], r[2]) for r in rows]
        scan_capped = False
        if scanned >= scan_rows:
            more = conn.execute("SELECT 1 FROM %s%s LIMIT 1 OFFSET ?" % (t, where),
                                params + [scan_rows]).fetchone()
            scan_capped = more is not None
        return colfilter.Distinct(colfilter.order_values(values), total, total > len(values),
                                  scanned, scan_capped, any(r[3] for r in rows))

    def _binary_only(self, s):
        """True for a table whose columns all compare as BINARY (no COLLATE in its schema):
        grouping by the column then groups exactly the values that are equal."""
        try:
            return s.info(self.table).kind == "table" and \
                "COLLATE" not in (self.db.create_sql(self.table) or "").upper()
        except Exception:               # noqa: BLE001 - then the exact form
            return False

    def count_matching(self, flt, cancel=None):
        """Rows passing flt (the engine's count: SQL, or the natively read rows)."""
        if not flt:
            return self.total if self.total is not None else self.db.count(self.table)
        return self.db.count_filtered(self.table, flt)

    def estimate_matching(self, flt, cancel=None, slices=None, per_slice=None):
        """(approximate rows passing flt, rows sampled), or None when there is no quick
        estimate (only for large rowid tables served by SQLite). The rows sampled are evenly
        spread rowid ranges, read through the rowid B-tree."""
        import colfilter
        s = self._sql()
        if s is None or not flt:
            return None
        info = s.info(self.table)
        if info.kind != "table" or info.without_rowid or not info.rowid_name:
            return None
        slices = slices or colfilter.ESTIMATE_SLICES
        per_slice = per_slice or colfilter.ESTIMATE_SLICE_ROWS
        # rowid_name is one of the literal aliases rowid / _rowid_ / oid (never user text)
        rid, t = info.rowid_name, quote_ident(self.table)
        conn = s.conn()
        lo, hi = conn.execute("SELECT min(%s), max(%s) FROM %s" % (rid, rid, t)).fetchone()
        if lo is None:
            return 0, 0
        span = hi - lo + 1
        if span <= slices * per_slice * 4:
            return None                 # small: the exact count is as quick
        total = self.total if self.total is not None else self.db.count(self.table)
        where, params = self._where(s, flt, ["%s BETWEEN ? AND ?" % rid])
        seen = hits = 0
        for k in range(slices):
            if cancel is not None and cancel():
                return None
            a = lo + (span * k) // slices
            b = a + per_slice - 1
            seen += conn.execute("SELECT COUNT(*) FROM %s WHERE %s BETWEEN ? AND ?"
                                 % (t, rid), (a, b)).fetchone()[0]
            hits += conn.execute("SELECT COUNT(*) FROM %s%s" % (t, where),
                                 params + [a, b]).fetchone()[0]
        if not seen or hits < colfilter.ESTIMATE_MIN_HITS:
            return None                 # too few rows matched in the sample to say anything
        return int(round(hits * float(total) / seen)), seen

    def nth_value(self, column, flt, n, desc, cancel=None):
        """The n-th largest (desc) or smallest number of a column among the rows passing flt
        (the last one when fewer rows hold numbers); None when none does."""
        import colfilter
        s = self._sql()
        if s is not None:
            q, t = quote_ident(column), quote_ident(self.table)
            where, params = self._where(s, flt, ["typeof(%s) IN ('integer', 'real')" % q])
            try:
                return s.conn().execute(
                    "SELECT %s(v) FROM (SELECT %s AS v FROM %s%s ORDER BY %s %s LIMIT ?)"
                    % ("min" if desc else "max", q, t, where, q, "DESC" if desc else "ASC"),
                    params + [int(n)]).fetchone()[0]
            except sqlite3.Error as e:
                if is_interrupt(e):
                    raise
        return colfilter.nth_from_rows(self._native_rows(flt), self._cols.index(column), n,
                                       desc, cancel)

    def date_bins(self, column, flt, kind, most, scan_rows, cancel=None):
        """colfilter.DateBins of a column read as dates of `kind`: numbers are counted per
        time bin by SQLite (two passes); date text and natively read tables in Python."""
        import colfilter
        s = self._sql()
        if s is not None:
            try:
                if kind in tl.NUMERIC_KINDS:
                    return self._sql_date_bins(s, column, flt, kind, most, scan_rows)
                q, t = quote_ident(column), quote_ident(self.table)
                where, params = self._where(s, flt)
                cur = s.conn().execute("SELECT %s FROM %s%s LIMIT ?" % (q, t, where),
                                       params + [scan_rows + 1])
                try:
                    rows = [[r[0]] for r in cur.fetchall()]
                finally:
                    cur.close()
                return colfilter.date_bins_from_rows(rows, 0, kind, most, scan_rows, cancel)
            except sqlite3.Error as e:
                if is_interrupt(e):
                    raise
        return colfilter.date_bins_from_rows(self._native_rows(flt), self._cols.index(column),
                                             kind, most, scan_rows, cancel)

    def _sql_date_bins(self, s, column, flt, kind, most, scan_rows):
        import colfilter
        q, t = quote_ident(column), quote_ident(self.table)
        lo_raw, hi_raw = colfilter.plausible_raw(kind)
        where, params = self._where(s, flt)
        inner = "SELECT %s AS v FROM %s%s LIMIT ?" % (q, t, where)
        iparams = params + [scan_rows]
        ok = "typeof(v) IN ('integer', 'real') AND v >= ? AND v < ?"
        conn = s.conn()
        lo, hi, n, scanned = conn.execute(
            "SELECT min(CASE WHEN %s THEN v END), max(CASE WHEN %s THEN v END), "
            "SUM(CASE WHEN %s THEN 1 ELSE 0 END), COUNT(*) FROM (%s)" % (ok, ok, ok, inner),
            [lo_raw, hi_raw] * 3 + iparams).fetchone()
        scanned = int(scanned or 0)
        scan_capped = False
        if scanned >= scan_rows:
            scan_capped = conn.execute("SELECT 1 FROM %s%s LIMIT 1 OFFSET ?" % (t, where),
                                       params + [scan_rows]).fetchone() is not None
        first_dt, last_dt = tl.to_utc(lo, kind), tl.to_utc(hi, kind)
        if not n or first_dt is None or last_dt is None:
            return colfilter.DateBins([], [], None, "", None, None, 0, scanned, scan_capped)
        first, step, name, raw0, width, nbins = colfilter.bin_plan(first_dt, last_dt, kind,
                                                                   most)
        totals = [0] * nbins
        for b, c in conn.execute("SELECT CAST((v - ?) / ? AS INTEGER) AS b, COUNT(*) FROM (%s) "
                                 "WHERE %s GROUP BY b" % (inner, ok),
                                 [raw0, float(width)] + iparams + [lo_raw, hi_raw]):
            if b is not None:
                totals[max(0, min(nbins - 1, int(b)))] += c
        edges = [first + step * i for i in range(nbins + 1)]
        return colfilter.DateBins(edges, totals, step, name, first_dt, last_dt, int(n),
                                  scanned, scan_capped)


class ListSource(object):
    """Rows held in memory, sorted and filtered in Python by the engine's rules.

    rows is a list of (values, flags). A first column named '_rid' is the row locator: the
    global filter does not search it, and sorting by it restores the original order.
    """

    threaded = False

    def __init__(self, columns, rows, encoding="utf-8", note=""):
        self._cols = list(columns)
        self._all = list(rows)
        self.encoding = encoding
        self.note = note
        self.order, self.desc, self.flt = None, False, None
        self._view = self._all

    def columns(self):
        return list(self._cols)

    def row_count(self):
        return len(self._view)

    def rows(self, start, count):
        return self._view[start:start + count]

    def sort(self, column, desc):
        self.order = None if column in (None, RID) else column
        self.desc = bool(desc)
        self._rebuild()

    def set_filters(self, col_exprs, global_text):
        flt = Filter(col_exprs=col_exprs, words=parse_words(global_text))
        self.flt = flt if flt else None
        self._rebuild()

    @property
    def filtered(self):
        return self.flt is not None

    @property
    def total(self):
        return len(self._all)

    def count_rows(self, cancel=None):
        return self.flt, len(self._view)

    def distinct_values(self, column, flt, limit, scan_rows, cancel=None):
        """colfilter.Distinct of a column over the rows in memory passing flt."""
        import colfilter
        return colfilter.generic_distinct(self, column, flt, limit, scan_rows, cancel)

    def set_count(self, flt, n):
        pass

    def iter_rows(self):
        for values, _flags in self._view:
            yield values

    def iter_all(self):
        for values, _flags in self._all:
            yield values

    def _rebuild(self):
        lead = 1 if self._cols and self._cols[0] == RID else 0
        data_cols = self._cols[lead:]
        rows = self._all
        if self.flt is not None:
            flt, enc = self.flt, self.encoding
            rows = [r for r in rows if flt.matches(data_cols, list(r[0][lead:]), enc)]
        if self.order in self._cols:
            ci = self._cols.index(self.order)
            rows = sorted(rows, key=lambda r: sort_key(r[0][ci] if ci < len(r[0]) else None),
                          reverse=self.desc)
        elif self.desc:
            rows = list(reversed(rows))
        self._view = rows
