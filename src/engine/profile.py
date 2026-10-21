"""What a column holds, for the Query tab's Filter panel: how many rows, how many have no
value or empty text, its most frequent values with their counts, the smallest and largest
number, and for a date column the rows per month (read as the kind the column is stored as).
SQLite does the work (one read-only statement per part); a list of values (recovered rows,
a natively read table) is profiled the same way in Python.

Never raises for a value it cannot read: a value that is not a date of the kind is counted
as 'not a date' (unset dates - 0, -1 - included), never guessed.
"""

from datetime import datetime

from . import timeline as tl
from .schema import quote_ident

TOP = 200                       # most frequent values listed (the panel says when cut)

# a stored number of each kind as Unix seconds, in SQL ({c}: the column)
UNIX_SECONDS = {
    "unix_s": "{c}", "unix_ms": "{c} / 1000.0", "unix_us": "{c} / 1000000.0",
    "unix_ns": "{c} / 1000000000.0", "cocoa_s": "{c} + 978307200",
    "cocoa_ns": "{c} / 1000000000.0 + 978307200", "webkit_us": "{c} / 1000000.0 - 11644473600",
    "filetime": "{c} / 10000000.0 - 11644473600", "hfs_s": "{c} - 2082844800",
    "dotnet_ticks": "{c} / 10000000.0 - 62135596800", "ole_days": "({c} - 25569) * 86400.0",
    "gps_s": "{c} + 315964800"}


class Profile(object):
    """total, nulls, empties, top [(value, rows)] (top_cut: there were more), distinct (None
    when more than TOP), numbers (rows holding a number), lo / hi (smallest / largest number),
    months [('YYYY-MM', rows)], not_dates (rows that are no date of the kind), first / last
    (datetimes)."""

    def __init__(self):
        self.total = self.nulls = self.empties = self.numbers = self.not_dates = 0
        self.top, self.top_cut, self.distinct = [], False, None
        self.lo = self.hi = None
        self.months = []
        self.first = self.last = None
        self.kind = None

    def looks(self):
        """'date', 'list' (few values to pick from), 'number' or 'text'."""
        if self.kind:
            return "date"
        if self.distinct is not None and self.distinct <= 60:
            return "list"
        if self.numbers and self.numbers >= 0.9 * (self.total - self.nulls - self.empties):
            return "number"
        return "text"


def _month_expr(c, kind):
    if kind == tl.ISO:
        return "strftime('%Y-%m', {c})".format(c=c)
    return "strftime('%Y-%m', {s}, 'unixepoch')".format(s=UNIX_SECONDS[kind].format(c=c))


def profile_sql(conn, table, column, kind=None, top=TOP):
    """The Profile of table.column through a connection (read-only statements only)."""
    t, c = quote_ident(table), quote_ident(column)
    p = Profile()
    p.kind = kind if kind in tl.KINDS and (kind == tl.ISO or kind in UNIX_SECONDS) else None
    one = conn.execute(
        "SELECT COUNT(*), SUM(%s IS NULL), SUM(%s = ''), "
        "SUM(typeof(%s) IN ('integer', 'real')), MIN(CASE WHEN typeof(%s) IN ('integer', "
        "'real') THEN %s END), MAX(CASE WHEN typeof(%s) IN ('integer', 'real') THEN %s END) "
        "FROM %s" % (c, c, c, c, c, c, c, t)).fetchone()
    p.total, p.nulls, p.empties, p.numbers = (int(x or 0) for x in one[:4])
    p.lo, p.hi = one[4], one[5]
    if not p.kind:                  # a date column is shown by month, not by value
        rows = conn.execute("SELECT %s, COUNT(*) FROM %s GROUP BY 1 ORDER BY 2 DESC, 1 "
                            "LIMIT %d" % (c, t, top + 1)).fetchall()
        p.top_cut = len(rows) > top
        p.top = [(r[0], r[1]) for r in rows[:top]]
        p.distinct = None if p.top_cut else len(p.top)
    if p.kind:
        m = _month_expr(c, p.kind)
        num = "typeof(%s) IN ('integer', 'real') AND %s > 0" % (c, c) if p.kind != tl.ISO \
            else "typeof(%s) = 'text'" % c
        months = conn.execute("SELECT %s AS m, COUNT(*) FROM %s WHERE %s GROUP BY m "
                              "ORDER BY m" % (m, t, num)).fetchall()
        p.months = [(ym, n) for ym, n in months if ym and "1990" <= ym <= "2100"]
        dated = sum(n for _ym, n in p.months)
        p.not_dates = p.total - p.nulls - dated
        if p.kind != tl.ISO:
            lo_hi = conn.execute("SELECT MIN(%s), MAX(%s) FROM %s WHERE %s AND %s BETWEEN "
                                 "'1990' AND '2100'" % (c, c, t, num, m)).fetchone()
            p.first, p.last = tl.to_utc(lo_hi[0], p.kind), tl.to_utc(lo_hi[1], p.kind)
        elif p.months:
            p.first = datetime.strptime(p.months[0][0] + "-01", "%Y-%m-%d")
            p.last = datetime.strptime(p.months[-1][0] + "-01", "%Y-%m-%d")
    return p


def profile_values(values, kind=None, top=TOP):
    """The Profile of a list of values (no SQL)."""
    p = Profile()
    p.kind = kind if kind in tl.KINDS else None
    counts, months = {}, {}
    for v in values:
        p.total += 1
        if v is None:
            p.nulls += 1
        elif v == "":
            p.empties += 1
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            p.numbers += 1
            p.lo = v if p.lo is None else min(p.lo, v)
            p.hi = v if p.hi is None else max(p.hi, v)
        key = v if not isinstance(v, (bytes, bytearray)) else bytes(v)
        counts[key] = counts.get(key, 0) + 1
        if p.kind and v is not None:
            dt = tl.to_utc(v, p.kind) if not tl.is_unset(v) else None
            if dt is not None and 1990 <= dt.year <= 2100:
                ym = "%04d-%02d" % (dt.year, dt.month)
                months[ym] = months.get(ym, 0) + 1
                p.first = dt if p.first is None else min(p.first, dt)
                p.last = dt if p.last is None else max(p.last, dt)
            else:
                p.not_dates += 1
    got = sorted(counts.items(), key=lambda x: (-x[1], str(x[0])))
    p.top_cut = len(got) > top
    p.top = got[:top]
    p.distinct = None if p.top_cut else len(got)
    p.months = sorted(months.items())
    return p


def month_range(ym_from, ym_to):
    """(first instant, last instant) of the months ym_from..ym_to ('YYYY-MM')."""
    y, m = int(ym_from[:4]), int(ym_from[5:7])
    start = datetime(y, m, 1)
    y, m = int(ym_to[:4]), int(ym_to[5:7])
    nxt = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1)
    from datetime import timedelta
    return start, nxt - timedelta(seconds=1)
