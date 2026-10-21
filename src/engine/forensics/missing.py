"""Missing row ids: the rowids a table no longer has, which point to deleted rows - also when
nothing of those rows can be recovered (the gap itself is evidence).

A rowid table numbers its rows; with AUTOINCREMENT, SQLite never hands a number out twice and
sqlite_sequence keeps the highest one ever given. So, for a table:
  - gaps: numbers between the smallest and the largest live rowid that no live row has;
  - after: numbers above the largest live rowid up to sqlite_sequence (AUTOINCREMENT only):
    rows that were added and then deleted (the most recent rows, often);
  - before: numbers from 1 below the smallest live rowid. With AUTOINCREMENT numbering starts
    at 1, so these are deleted rows too and are listed; without it a table may start
    anywhere (and a rowid can be any number), so they are only counted, not listed.
What is not evidence of a deletion is said: without AUTOINCREMENT a deleted largest row
leaves no trace (its number may be given again); an UPDATE of the rowid also moves a row.
The live rowids are read through the session (WAL applied). Never raises for a table it can
read; a WITHOUT ROWID table has no row numbers and gets a reason instead.
"""

import bisect
from ..schema import quote_ident


class MissingKeys(object):
    """The answer for one table (see the module doc). keys: the missing numbers listed, at
    most `limit` (cut: there were more); ranges: [(first, last)] of consecutive missing
    numbers (all of them, never cut); count: how many are missing in all."""

    def __init__(self, table):
        self.table = table
        self.keys, self.ranges, self.cut = [], [], False
        self.count = self.gaps = self.after = self.before = 0
        self.live = 0
        self.lo = self.hi = self.sequence = None
        self.autoincrement = False
        self.reason = None              # why there is no answer (WITHOUT ROWID, unreadable)
        self.recovered = {}             # missing number -> [Record] the recovery found

    def text(self):
        """One sentence of what was found, in plain words."""
        if self.reason:
            return self.reason
        if not self.live and self.sequence is None:
            return "%s has no rows." % self.table
        parts = ["%s of the row numbers %s are missing from %s" % (
            format(self.count, ","), "1–%s" % format(self.top(), ",") if self.lo is not None
            else "up to %s" % format(self.top(), ","), self.table)]
        bits = []
        if self.gaps:
            bits.append("%s inside the live rows (%s–%s)" % (
                format(self.gaps, ","), format(self.lo, ","), format(self.hi, ",")))
        if self.after:
            bits.append("%s after the last live row, up to %s (sqlite_sequence: the highest "
                        "number ever given)" % (format(self.after, ","),
                                                format(self.sequence, ",")))
        if self.before and self.autoincrement:
            bits.append("%s before the first live row" % format(self.before, ","))
        text = parts[0] + (": " + "; ".join(bits) if bits else "") + "."
        if self.before and not self.autoincrement:
            text += " Numbers below %s are not counted: without AUTOINCREMENT a table need " \
                    "not start at 1." % format(self.lo, ",")
        if not self.autoincrement:
            text += " Without AUTOINCREMENT a deleted last row leaves no gap."
        if self.recovered:
            text += " %s of the missing rows were recovered (Forensics)." % format(
                len(self.recovered), ",")
        return text

    def top(self):
        return max(x for x in (self.hi, self.sequence, 0) if x is not None)


def _sequence(session, table):
    """sqlite_sequence's number for the table, or None (no AUTOINCREMENT)."""
    try:
        if "sqlite_sequence" not in session.tables():
            return None
        for r in session.iter_rows("sqlite_sequence"):
            vals = r.values
            if len(vals) >= 2 and vals[0] == table and isinstance(vals[1], int):
                return vals[1]
    except Exception:                   # noqa: BLE001 - an unreadable sequence: no number
        return None
    return None


def _live_rowids(session, table):
    """The live rowids in order (SQL when SQLite reads the table, else natively)."""
    if session.source(table) == "sql" and getattr(session, "sql", None) is not None:
        cur = session.conn().execute("SELECT rowid FROM %s ORDER BY rowid" % quote_ident(table))
        try:
            return [r[0] for r in cur if isinstance(r[0], int)]
        finally:
            cur.close()
    out = []
    for r in session.iter_rows(table):
        loc = r.locator
        if getattr(loc, "kind", None) == "rowid" and isinstance(loc.value, int):
            out.append(loc.value)
    out.sort()
    return out


def missing_keys(session, table, limit=100000, records=()):
    """MissingKeys of a table; records: recovered Records to say which missing rows came
    back."""
    m = MissingKeys(table)
    try:
        info = session.info(table)
    except Exception as e:              # noqa: BLE001
        m.reason = "%s cannot be read: %s" % (table, e)
        return m
    if info.without_rowid or not info.rowid_name and info.rowid_alias is None:
        m.reason = "%s is a WITHOUT ROWID table: its rows have no numbers to miss." % table
        return m
    try:
        live = _live_rowids(session, table)
    except Exception as e:              # noqa: BLE001
        m.reason = "The row numbers of %s could not be read: %s" % (table, e)
        return m
    m.live = len(live)
    m.sequence = _sequence(session, table)
    m.autoincrement = m.sequence is not None or "autoincrement" in (info.sql or "").lower()
    if live:
        m.lo, m.hi = live[0], live[-1]
    ranges = []
    if live and m.lo > 1:
        m.before = m.lo - 1
        if m.autoincrement:
            ranges.append((1, m.lo - 1))
    prev = None
    for k in live:
        if prev is not None and k > prev + 1:
            ranges.append((prev + 1, k - 1))
            m.gaps += k - prev - 1
        prev = k
    top = m.hi if m.hi is not None else 0
    if m.sequence is not None and m.sequence > top:
        ranges.append((top + 1, m.sequence))
        m.after = m.sequence - top
    m.ranges = ranges
    m.count = sum(b - a + 1 for a, b in ranges)
    for a, b in ranges:
        for k in range(a, b + 1):
            if len(m.keys) >= limit:
                m.cut = True
                break
            m.keys.append(k)
        if m.cut:
            break
    if records:
        # the ranges are in order: a rowid is looked up among them (any size of range)
        starts = [a for a, _b in ranges]
        for r in records:
            if r.table != table or getattr(r, "index", None) is not None or \
                    not isinstance(r.rowid, int):
                continue
            i = bisect.bisect_right(starts, r.rowid) - 1
            if i >= 0 and r.rowid <= ranges[i][1]:
                m.recovered.setdefault(r.rowid, []).append(r)
    return m
