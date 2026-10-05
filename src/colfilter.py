"""Column filters of the DataGrid: the filter popover of a column, the chips bar above the grid
and the helpers they share (no Tk in the helpers: they are tested on their own).

Every filter is written in the engine's filter language (engine.filters) with
condition_text() and combine(), so what the popover builds is what the inline filter row, the
saved filters and 'Copy as filter text' show, and the engine selects the same rows in SQL and
in Python.

  detect_type(name, decl, values)   ColumnType: text / number / date / bool / enum / blob /
                                    json, from the declared type and a sample of values
  compile_condition(ct, op, args)   one condition of the builder as filter text (or None)
  checklist_text(entries, ...)      the ticked values as IN (...) / NOT IN (...)
  describe_filter(column, text)     the words of a chip ('status = 3', 'ts: 1 Mar – 5 Mar 2026')
  builder_state(text, ct)           the builder's fields for a filter written before
  sql_where_text(flt, columns)      'WHERE ...' with the values written in (Copy as SQL WHERE)

  source_distinct / source_count / source_estimate / source_nth / source_date_bins
      the data a popover reads, on the grid's filter worker thread: the source's own method
      when it has one (browse_sources.TableSource: SQL), else by iterating its rows

  FilterPopover   the popover of one column (DataGrid.open_filter_popover)
  FilterBar       the bar above the grid: the search box, Back / Forward, the chips of the
                  filters in force with Clear all, Save filter…, Saved ▾, Copy ▾ and the count

Limits: the checklist lists at most limits 'filter_distinct_values' values, counted over at
most 'filter_distinct_scan_rows' rows; the popover says so when either cut something.
"""

import collections
import heapq
import json
import sys
import threading
from datetime import datetime, timedelta

import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont

from engine import limits
from engine import timeline as tl
from engine.backends import Filter
from engine.filters import (FilterError, ascii_lower, combine, condition_text, parse_expr,
                            parse_words, real_text, sql_literal)
from engine.fileformat.record import InvalidText
from engine.schema import Locator
from engine.session import sort_key
from tokens import COLOR as K, FONT as F, XS, S, M

RID = "_rid"
ENUM_MAX = 12               # a column with at most this many distinct values offers them as chips
BLOB_VALUE_MAX = 256        # the longest BLOB engine.filters.value_expr writes as a value
ESTIMATE_SLICES = 16        # rowid ranges read for a first, approximate count
ESTIMATE_SLICE_ROWS = 1000  # rows per range (an estimate only: the exact count follows)
ESTIMATE_MIN_HITS = 20      # fewer matching rows sampled: no estimate shown (the exact follows)
COUNT_DELAY_MS = 250        # quiet time after an edit before the live count is read
BAR_VALUES = 10             # the most frequent values drawn with a distribution bar
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

TYPE_LABELS = {"text": "Text", "number": "Number", "date": "Date", "bool": "Yes / no (0 or 1)",
               "enum": "Few values", "blob": "BLOB", "json": "JSON text"}

TEXT_OPS = [("contains", "contains"), ("not_contains", "does not contain"),
            ("equals", "equals"), ("not_equals", "does not equal"),
            ("starts", "starts with"), ("ends", "ends with"), ("regex", "matches regex"),
            ("empty", "is empty"), ("not_empty", "is not empty"),
            ("expr", "filter expression")]
NUMBER_OPS = [("equals", "="), ("not_equals", "≠"), ("gt", ">"), ("ge", "≥"),
              ("lt", "<"), ("le", "≤"), ("between", "between"), ("top", "top N"),
              ("bottom", "bottom N"), ("empty", "is empty"), ("not_empty", "is not empty"),
              ("expr", "filter expression")]
DATE_OPS = [("between", "between"), ("before", "before"), ("after", "after"),
            ("on", "on day"), ("last_days", "in the last N days"),
            ("this_month", "this month"), ("empty", "is empty"),
            ("not_empty", "is not empty"), ("expr", "filter expression")]
BLOB_OPS = [("not_empty", "is not empty"), ("empty", "is empty"), ("null", "is NULL"),
            ("not_null", "is not NULL"), ("equals", "equals (hex)"),
            ("expr", "filter expression")]
NO_ARG = frozenset(("empty", "not_empty", "null", "not_null", "this_month"))
_CMP_SIGN = {"=": "=", "<>": "≠", ">": ">", ">=": "≥", "<": "<", "<=": "≤"}
_CMP_OP = {"=": "equals", "<>": "not_equals", ">": "gt", ">=": "ge", "<": "lt", "<=": "le"}


class Cancelled(Exception):
    """A popover job whose input changed (or whose popover closed) before it finished."""


class NothingTicked(ValueError):
    """The value checklist has no value ticked: no row could match."""


class LargeBlob(object):
    """A BLOB value too long to write in a filter (BLOB_VALUE_MAX); size in bytes."""
    __slots__ = ("size",)

    def __init__(self, size):
        self.size = int(size or 0)

    def __eq__(self, other):
        return isinstance(other, LargeBlob) and other.size == self.size

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(("LargeBlob", self.size))

    def __repr__(self):
        return "LargeBlob(%d)" % self.size


# the checklist's data: values most frequent first, how many distinct values there are (among
# the rows read), whether the list was cut to the limit, rows read, whether the rows were cut,
# whether equal INTEGER and REAL values were counted as one value (merged)
Distinct = collections.namedtuple("Distinct", "values total capped scanned scan_capped merged")
Distinct.__new__.__defaults__ = (False,)
# a date histogram: bin edges (len(totals) + 1), counts, bin width and its name, first and
# last date, rows holding a date, rows read, whether the rows were cut
DateBins = collections.namedtuple("DateBins", "edges totals step step_name first last rows "
                                              "scanned scan_capped")


# -- formatting ---------------------------------------------------------------------------------
def fmt_int(n):
    return format(int(n), ",")


def fmt_compact(n):
    """2460000 -> '2.46M', 12345 -> '12.3K', 950 -> '950'."""
    n = int(n)
    for div, suffix in ((10 ** 9, "B"), (10 ** 6, "M"), (10 ** 4, "K")):
        if abs(n) >= div:
            unit = 1000 if suffix == "K" else div
            v = n / float(unit)
            text = ("%.2f" % v) if abs(v) < 10 else ("%.1f" % v) if abs(v) < 100 else "%d" % v
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return text + suffix
    return fmt_int(n)


def fmt_day(dt, year=True):
    return "%d %s%s" % (dt.day, MONTHS[dt.month - 1], (" %d" % dt.year) if year else "")


def fmt_when(dt, year=True):
    """'1 Mar 2026', '1 Mar 2026 10:15', '1 Mar 2026 10:15:30' (UTC)."""
    text = fmt_day(dt, year)
    if dt.hour or dt.minute or dt.second or dt.microsecond:
        text += " %02d:%02d" % (dt.hour, dt.minute)
        if dt.second or dt.microsecond:
            text += ":%02d" % dt.second
    return text


def _midnight(dt):
    return dt.hour == dt.minute == dt.second == dt.microsecond == 0


def fmt_range(lo, hi):
    """Words for the inclusive range lo .. hi (None: open end)."""
    if lo is None and hi is None:
        return "any time"
    if lo is None:
        return "until %s" % fmt_when(hi)
    if hi is None:
        return "from %s" % fmt_when(lo)
    if _midnight(lo) and _midnight(hi) and lo.year == hi.year:
        if lo == hi:
            return "on %s" % fmt_day(lo)
        return "%s – %s" % (fmt_day(lo, False), fmt_day(hi))
    return "%s – %s" % (fmt_when(lo), fmt_when(hi))


def short_value(v, width=30):
    """A value as a chip shows it: 3, 2.5, 'text', x'89504e47…', NULL."""
    if v is None:
        return "NULL"
    if isinstance(v, LargeBlob):
        return "[BLOB %s bytes]" % fmt_int(v.size)
    if isinstance(v, InvalidText):
        return "invalid text"
    if isinstance(v, (bytes, bytearray)):
        h = bytes(v[:8]).hex()
        return "x'%s%s'" % (h, "…" if len(v) > 8 else "")
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, float):
        return real_text(v) if v == int(v) and abs(v) < 1e15 else repr(v)
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("\r", " ").replace("\n", " ")
    if len(s) > width:
        s = s[:width - 1] + "…"
    return "'%s'" % s


# -- type detection -------------------------------------------------------------------------------
class ColumnType(object):
    """What the popover builds conditions for: kind is text, number, date, bool, enum, blob
    or json; base the kind an enum column's values are (text or number); date_kind the
    engine.timeline kind of a date column; iso_style ' ', 'T' or 'date' (date text written
    'YYYY-MM-DD HH:MM:SS', with a T, or as the date alone)."""
    __slots__ = ("kind", "base", "date_kind", "iso_style", "reason")

    def __init__(self, kind, base=None, date_kind=None, iso_style=" ", reason=""):
        self.kind, self.base, self.date_kind = kind, base or kind, date_kind
        self.iso_style, self.reason = iso_style, reason

    @property
    def label(self):
        if self.kind == "date" and self.date_kind:
            return "Date · %s" % tl.SHORT.get(self.date_kind, self.date_kind)
        if self.kind == "enum":
            return "Few values · %s" % TYPE_LABELS.get(self.base, self.base).lower()
        return TYPE_LABELS.get(self.kind, self.kind)

    @property
    def ops(self):
        k = self.base if self.kind == "enum" else self.kind
        if k == "date":
            return DATE_OPS
        if k in ("number", "bool"):
            return NUMBER_OPS
        if k == "blob":
            return BLOB_OPS
        return TEXT_OPS

    def __repr__(self):
        return "ColumnType(%s%s)" % (self.kind, (", " + self.date_kind) if self.date_kind else "")


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _looks_json(v):
    s = v.strip()
    if len(s) < 2 or s[0] not in "{[" or s[-1] not in "}]":
        return False
    try:
        json.loads(s)
    except (ValueError, RecursionError):
        return False
    return True


def iso_style_of(values):
    """How date text values are written: 'T' (2024-03-01T10:00), 'date' (the day alone) or
    ' ' (2024-03-01 10:00:00)."""
    t = sp = day = 0
    for v in values:
        if not isinstance(v, str) or len(v) < 10:
            continue
        if len(v.strip()) == 10:
            day += 1
        elif v[10:11] == "T":
            t += 1
        else:
            sp += 1
    if day > t + sp:
        return "date"
    return "T" if t > sp else " "


def detect_type(name, decl="", values=(), date_kind=None):
    """ColumnType of a column from its declared type and a sample of its values. date_kind:
    the kind the column is already shown as (Show as date), taken as it is."""
    vals = [v for v in values if v is not None and not isinstance(v, Locator)]
    decl_u = (decl or "").upper()
    if date_kind in tl.KINDS:
        return ColumnType("date", date_kind=date_kind, iso_style=iso_style_of(vals),
                          reason="shown as dates of that kind")
    nonempty = [v for v in vals if not (isinstance(v, str) and v == "")]
    blobs = sum(1 for v in nonempty if isinstance(v, (bytes, bytearray, LargeBlob))
                and not isinstance(v, InvalidText))
    if (nonempty and blobs * 2 > len(nonempty)) or (not nonempty and "BLOB" in decl_u):
        return ColumnType("blob", reason="most values are BLOBs")
    if nonempty and all(isinstance(v, int) and not isinstance(v, bool) and v in (0, 1)
                        for v in nonempty) and (len(nonempty) >= 2 or "BOOL" in decl_u):
        return ColumnType("bool", base="number", reason="every value is 0 or 1")
    try:
        guess = tl.judge(name, vals, decl or "")
    except Exception:                   # noqa: BLE001 - the detector is a courtesy
        guess = None
    if guess is not None and guess.kind:
        return ColumnType("date", date_kind=guess.kind, iso_style=iso_style_of(vals),
                          reason=guess.reason)
    strs = [v for v in nonempty if isinstance(v, str)]
    nums = [v for v in nonempty if _is_number(v)]
    if strs and len(strs) * 2 > len(nonempty):
        jsons = sum(1 for v in strs[:200] if _looks_json(v))
        if jsons * 2 > min(len(strs), 200):
            return ColumnType("json", base="text", reason="the text values are JSON")
    numeric_decl = any(w in decl_u for w in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))
    base = "number" if (nums and len(nums) * 2 > len(nonempty)) or \
        (not nonempty and numeric_decl) else "text"
    distinct = set(_vkey(v) for v in nonempty)
    if nonempty and len(distinct) <= ENUM_MAX and len(nonempty) >= max(8, 2 * len(distinct)):
        return ColumnType("enum", base=base,
                          reason="%d distinct values in %d sampled" % (len(distinct),
                                                                      len(nonempty)))
    return ColumnType(base, reason="most values are %s" % ("numbers" if base == "number"
                                                           else "text"))


# -- values as keys, for counting -----------------------------------------------------------------
def _vkey(v):
    """A hashable key per distinct stored value, as SQLite groups them by typeof and value
    (5 and 5.0 stay apart, as 'x' and x'78')."""
    if isinstance(v, bool):
        v = int(v)
    if v is None:
        return ("z", None)
    if isinstance(v, LargeBlob):
        return ("L", v.size)
    if isinstance(v, InvalidText):
        return ("i", bytes(v))
    if isinstance(v, (bytes, bytearray, memoryview)):
        return ("b", bytes(v))
    if isinstance(v, float):
        return ("r", v if v == v else "nan")
    if isinstance(v, int):
        return ("n", v)
    if isinstance(v, str):
        return ("t", v)
    return ("o", str(v))


def _order_key(item):
    v, n = item
    try:
        return (-n, sort_key(v if not isinstance(v, LargeBlob) else b""))
    except Exception:                   # noqa: BLE001 - an odd value sorts last
        return (-n, (9,))


def order_values(pairs):
    """(value, count) pairs most frequent first, equal counts in the engine's value order."""
    return sorted(pairs, key=_order_key)


def _layout(source, column):
    cols = list(source.columns())
    lead = 1 if cols and cols[0] == RID else 0
    return cols.index(column), cols[lead:], lead


def source_rows(source):
    """Every row of a source (its value lists), filters ignored where it can say so."""
    it = getattr(source, "iter_all", None)
    if it is not None:
        return it()

    def pages():
        step = max(1, limits.get("grid_window_rows"))
        start = 0
        while True:
            got = source.rows(start, step)
            for values, _flags in got:
                yield values
            if len(got) < step:
                return
            start += step
    return pages()


def _filtered(rows, flt, data_cols, lead, encoding, cancel):
    for i, values in enumerate(rows):
        if cancel is not None and i % 2000 == 0 and cancel():
            raise Cancelled()
        if flt is None or flt.matches(data_cols, list(values[lead:]), encoding):
            yield values


def distinct_from_rows(rows, index, limit, scan_rows, cancel=None):
    """Distinct of the values at `index` of the rows (already filtered), reading at most
    scan_rows rows."""
    counts, first = {}, {}
    scanned, scan_capped = 0, False
    for values in rows:
        if scanned >= scan_rows:
            scan_capped = True
            break
        if cancel is not None and scanned % 2000 == 0 and cancel():
            raise Cancelled()
        scanned += 1
        v = values[index] if index < len(values) else None
        if isinstance(v, (bytes, bytearray)) and not isinstance(v, InvalidText) \
                and len(v) > BLOB_VALUE_MAX:
            v = LargeBlob(len(v))
        k = _vkey(v)
        n = counts.get(k)
        if n is None:
            counts[k] = 1
            first[k] = v
        else:
            counts[k] = n + 1
    top = heapq.nlargest(max(0, limit), counts.items(), key=lambda kv: kv[1])
    values = order_values([(first[k], n) for k, n in top])
    return Distinct(values, len(counts), len(counts) > len(values), scanned, scan_capped)


def generic_distinct(source, column, flt, limit, scan_rows, cancel=None):
    """Distinct of a source's column by iterating its rows (in-memory sources)."""
    index, data_cols, lead = _layout(source, column)
    enc = getattr(source, "encoding", "utf-8") or "utf-8"
    rows = _filtered(source_rows(source), flt, data_cols, lead, enc, cancel)
    return distinct_from_rows(rows, index, limit, scan_rows, cancel)


def nth_from_rows(rows, index, n, desc, cancel=None):
    nums = []
    for i, values in enumerate(rows):
        if cancel is not None and i % 2000 == 0 and cancel():
            raise Cancelled()
        v = values[index] if index < len(values) else None
        if _is_number(v) and v == v:
            nums.append(v)
    if not nums:
        return None
    pick = heapq.nlargest(n, nums) if desc else heapq.nsmallest(n, nums)
    return pick[-1]


def plausible_raw(kind):
    """(low, high) raw values of `kind` for the years the tool takes as dates."""
    return (tl.from_utc(datetime(tl.LO_YEAR, 1, 1), kind),
            tl.from_utc(datetime(tl.HI_YEAR + 1, 1, 1), kind))


def bin_plan(first_dt, last_dt, kind, most):
    """(first edge, step, step name, raw value of the first edge, raw width of a bin, bins)
    for dates first_dt .. last_dt of a numeric kind."""
    from density import floor_time, nice_bins
    step, name = nice_bins(first_dt, last_dt, most)
    first = floor_time(first_dt, step)
    raw0 = tl.from_utc(first, kind)
    width = tl.from_utc(first + step, kind) - raw0
    nbins = int((last_dt - first) // step) + 1
    return first, step, name, raw0, width or 1, nbins


def weighted_bins(pairs, most):
    """(edges, totals, step, step name) of (datetime, count) pairs."""
    from density import floor_time, nice_bins
    pairs = [(d, c) for d, c in pairs if d is not None]
    if not pairs:
        return [], [], None, ""
    lo = min(d for d, _c in pairs)
    hi = max(d for d, _c in pairs)
    step, name = nice_bins(lo, hi, most)
    first = floor_time(lo, step)
    n = int((hi - first) // step) + 1
    totals = [0] * n
    for d, c in pairs:
        totals[min(n - 1, int((d - first) // step))] += c
    return [first + step * i for i in range(n + 1)], totals, step, name


def date_bins_from_rows(rows, index, kind, most, scan_rows, cancel=None):
    """DateBins of the values at `index` of the rows (already filtered) read as `kind`."""
    counts = collections.Counter()
    scanned, scan_capped = 0, False
    for values in rows:
        if scanned >= scan_rows:
            scan_capped = True
            break
        if cancel is not None and scanned % 2000 == 0 and cancel():
            raise Cancelled()
        scanned += 1
        v = values[index] if index < len(values) else None
        if v is not None and not isinstance(v, (bytes, bytearray)):
            counts[v] += 1
    lo_y, hi_y = datetime(tl.LO_YEAR, 1, 1), datetime(tl.HI_YEAR + 1, 1, 1)
    pairs = []
    for v, c in counts.items():
        dt = tl.to_utc(v, kind)
        if dt is not None and lo_y <= dt < hi_y:
            pairs.append((dt, c))
    edges, totals, step, name = weighted_bins(pairs, most)
    rows_n = sum(c for _d, c in pairs)
    return DateBins(edges, totals, step, name, edges and min(d for d, _c in pairs) or None,
                    edges and max(d for d, _c in pairs) or None, rows_n, scanned, scan_capped)


# -- data a popover reads (worker thread) -----------------------------------------------------------
def source_distinct(source, column, flt, limit, scan_rows, cancel=None):
    fn = getattr(source, "distinct_values", None)
    if fn is not None:
        return fn(column, flt, limit, scan_rows, cancel)
    return generic_distinct(source, column, flt, limit, scan_rows, cancel)


def source_count(source, flt, cancel=None):
    """Exact number of rows passing flt."""
    fn = getattr(source, "count_matching", None)
    if fn is not None:
        return fn(flt, cancel)
    _i, data_cols, lead = _layout(source, source.columns()[0])
    enc = getattr(source, "encoding", "utf-8") or "utf-8"
    n = 0
    for _v in _filtered(source_rows(source), flt, data_cols, lead, enc, cancel):
        n += 1
    return n


def source_estimate(source, flt, cancel=None):
    """(approximate count, rows sampled) or None."""
    fn = getattr(source, "estimate_matching", None)
    return fn(flt, cancel) if fn is not None else None


def source_nth(source, column, flt, n, desc, cancel=None):
    fn = getattr(source, "nth_value", None)
    if fn is not None:
        return fn(column, flt, n, desc, cancel)
    index, data_cols, lead = _layout(source, column)
    enc = getattr(source, "encoding", "utf-8") or "utf-8"
    return nth_from_rows(_filtered(source_rows(source), flt, data_cols, lead, enc, cancel),
                         index, n, desc, cancel)


def source_date_bins(source, column, flt, kind, most, scan_rows, cancel=None):
    fn = getattr(source, "date_bins", None)
    if fn is not None:
        return fn(column, flt, kind, most, scan_rows, cancel)
    index, data_cols, lead = _layout(source, column)
    enc = getattr(source, "encoding", "utf-8") or "utf-8"
    return date_bins_from_rows(_filtered(source_rows(source), flt, data_cols, lead, enc, cancel),
                               index, kind, most, scan_rows, cancel)


# -- conditions -------------------------------------------------------------------------------------
def parse_number(text):
    """int or float of typed text; ValueError (said for the user) otherwise."""
    s = (text or "").strip()
    try:
        v = int(s)
        if -(1 << 63) <= v < (1 << 63):
            return v
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        raise ValueError("'%s' is not a number" % s)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("'%s' is not a number that can be compared" % s)
    return f


def parse_count(text, what="N"):
    s = (text or "").strip()
    try:
        n = int(s)
    except ValueError:
        raise ValueError("%s must be a whole number, not '%s'" % (what, s))
    if n < 1:
        raise ValueError("%s must be 1 or more" % what)
    return n


def raw_date(dt, ct):
    """The value a date column of ColumnType ct stores for a naive UTC datetime."""
    kind = ct.date_kind
    if kind == tl.ISO:
        text = tl.from_utc(dt, kind)
        if ct.iso_style == "date":
            return text[:10]
        return text[:10] + ("T" if ct.iso_style == "T" else " ") + text[11:]
    return tl.from_utc(dt, kind)


def date_bounds(op, args, now=None):
    """(start, end, end_inclusive) in UTC for a date condition; None ends are open."""
    now = now or tl.utc_now()
    if op == "before":
        return None, args[0], False
    if op == "after":
        return args[0], None, None            # strictly after (see date_condition)
    if op == "between":
        return args[0], args[1], True
    if op == "on":
        day = datetime(args[0].year, args[0].month, args[0].day)
        return day, day + timedelta(days=1), False
    if op == "hour":
        h = args[0].replace(minute=0, second=0, microsecond=0)
        return h, h + timedelta(hours=1), False
    if op == "last_days":
        return now - timedelta(days=parse_count(args[0], "The number of days")), None, True
    if op == "this_month":
        start = datetime(now.year, now.month, 1)
        nxt = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
        return start, nxt, False
    raise ValueError("unknown date condition %r" % op)


def date_condition(ct, op, args, now=None):
    """Filter text of a date condition on the column's raw stored values, or None."""
    if op in ("before", "after", "on", "hour") and (not args or args[0] is None):
        return None
    if op == "between" and (len(args) < 2 or (args[0] is None and args[1] is None)):
        return None
    if op == "last_days" and not str(args[0] if args else "").strip():
        return None
    lo, hi, incl = date_bounds(op, args, now)
    if op == "after":
        return condition_text("gt", raw_date(lo, ct))
    if incl and hi is not None and hi.microsecond == 999999:
        # an end read as 'the end of that second / day' (23:59:59.999999): before the next
        # instant instead, so the stored whole numbers are compared with a whole number
        hi, incl = hi + timedelta(microseconds=1), False
    if lo is not None and hi is not None:
        if hi < lo:
            raise ValueError("the end is before the start")
        if incl:
            return condition_text("between", raw_date(lo, ct), raw_date(hi, ct))
        return combine(condition_text("ge", raw_date(lo, ct)), "AND",
                       condition_text("lt", raw_date(hi, ct)))
    if lo is not None:
        return condition_text("ge", raw_date(lo, ct))
    return condition_text("le" if incl else "lt", raw_date(hi, ct))


def compile_condition(ct, op, args, now=None, threshold=None):
    """One builder condition as filter text; None when it asks nothing yet (an empty value).
    Raises ValueError with a message for the user when the input cannot be used.

    args by op: text ops (value,); regex (pattern, any_case); number ops (value,) or
    between (low, high); top / bottom (N,) with threshold the value found for it; date ops
    datetimes (between: start, end; None for an open end) or last_days (N,); blob equals
    (hex digits,); expr (filter text,)."""
    args = tuple(args or ())
    if op in NO_ARG:
        if op == "this_month":
            return date_condition(ct, op, args, now)
        return condition_text(op)
    if op == "expr":
        text = (args[0] if args else "") or ""
        if not text.strip():
            return None
        try:
            parse_expr(text)
        except FilterError as e:
            raise ValueError(str(e))
        return text.strip()
    kind = ct.base if ct.kind == "enum" else ct.kind
    if kind == "date":
        return date_condition(ct, op, args, now)
    if op in ("top", "bottom"):
        if not str(args[0] if args else "").strip():
            return None
        parse_count(args[0])
        if threshold is None:
            return None
        return condition_text("ge" if op == "top" else "le", threshold)
    if kind == "blob":
        if op != "equals":
            raise ValueError("unknown condition %r" % op)
        h = "".join((args[0] or "").split()) if args else ""
        if h.lower().startswith("x'") and h.endswith("'"):
            h = h[2:-1]
        if not h:
            return None
        try:
            b = bytes.fromhex(h)
        except ValueError:
            raise ValueError("'%s' is not hex digits (e.g. 89504e47)" % h)
        return condition_text("equals", b)
    if op == "regex":
        pattern = args[0] if args else ""
        if not pattern:
            return None
        try:
            return condition_text("regex", pattern, bool(args[1]) if len(args) > 1 else False)
        except FilterError as e:
            raise ValueError(str(e))
    if kind in ("number", "bool"):
        if op == "between":
            a, b = (args + ("", ""))[:2]
            if not str(a).strip() and not str(b).strip():
                return None
            if not str(a).strip():
                return condition_text("le", parse_number(b))
            if not str(b).strip():
                return condition_text("ge", parse_number(a))
            lo, hi = parse_number(a), parse_number(b)
            if hi < lo:
                raise ValueError("the second number is smaller than the first")
            return condition_text("between", lo, hi)
        if op not in _CMP_OP.values():
            raise ValueError("unknown condition %r" % op)
        if not str(args[0] if args else "").strip():
            return None
        return condition_text(op, parse_number(args[0]))
    # text and JSON text
    value = args[0] if args else ""
    if value is None or value == "":
        return None
    try:
        if op in ("contains", "not_contains", "starts", "ends", "equals", "not_equals"):
            return condition_text(op, str(value))
    except FilterError as e:
        raise ValueError(str(e))
    raise ValueError("unknown condition %r" % op)


def join_conditions(parts, join="AND"):
    """Combine the conditions that are not None ('{a} AND {b}'); None when there are none."""
    parts = [p for p in parts if p]
    if not parts:
        return None
    text = parts[0]
    for p in parts[1:]:
        text = combine(text, join, p)
    return text


# -- the value checklist ------------------------------------------------------------------------------
class ValueEntry(object):
    """One line of the checklist: the values it stands for ((Blanks): NULL and ''), their
    count, whether they can be written in a filter, the line's label."""
    __slots__ = ("key", "label", "values", "count", "expressible", "rank", "blank")

    def __init__(self, key, label, values, count, expressible, rank=0, blank=False):
        self.key, self.label, self.values, self.count = key, label, list(values), count
        self.expressible, self.rank, self.blank = expressible, rank, blank

    def __repr__(self):
        return "ValueEntry(%r, %d)" % (self.label, self.count)


BLANKS = "(Blanks)"


def expressible(v):
    """True when a value can be written in a filter (IN / =)."""
    if isinstance(v, (LargeBlob, InvalidText, Locator)):
        return False
    if isinstance(v, (bytes, bytearray)):
        return len(v) <= BLOB_VALUE_MAX
    if isinstance(v, float):
        return v == v
    if isinstance(v, str):
        return "\x00" not in v
    return v is None or isinstance(v, int)


def build_entries(pairs, label_of=None):
    """ValueEntry lines from (value, count) pairs: NULL and the empty text as one (Blanks)
    line first, then the values in the order given."""
    label_of = label_of or short_value
    out = []
    blank_vals, blank_n = [], 0
    for v, n in pairs:
        if v is None or (isinstance(v, str) and v == ""):
            blank_vals.append(v)
            blank_n += n
    if blank_vals:
        out.append(ValueEntry(("blanks",), BLANKS, blank_vals, blank_n, True, blank=True))
    rank = 0
    for v, n in pairs:
        if v is None or (isinstance(v, str) and v == ""):
            continue
        try:
            label = label_of(v)
        except Exception:               # noqa: BLE001 - a label must not break the list
            label = short_value(v)
        out.append(ValueEntry(_vkey(v), label, [v], n, expressible(v), rank))
        rank += 1
    return out


def checklist_text(entries, ticked, complete, only_ticked=False):
    """Filter text keeping the rows whose value is ticked; None when every value is kept.

    entries: every ValueEntry listed; ticked: the keys ticked; complete: the list holds every
    distinct value (not cut by a limit); only_ticked: values not listed are left out too
    (after Select none). Writes IN (...) or NOT IN (...), whichever is shorter and says
    what is meant. Raises NothingTicked when no value is ticked, ValueError when the values
    cannot be written (e.g. a BLOB too long to write)."""
    tick = [e for e in entries if e.key in ticked]
    untick = [e for e in entries if e.key not in ticked]
    if not tick:
        raise NothingTicked("Tick at least one value (or use Clear filter)")
    if not untick and (complete or not only_ticked):
        return None
    inc = [v for e in tick for v in e.values]
    exc = [v for e in untick for v in e.values]
    can_in = all(e.expressible for e in tick)
    can_out = all(e.expressible for e in untick)
    if not complete:
        use_in = only_ticked
    else:
        use_in = len(inc) <= len(exc)
        if use_in and not can_in:
            use_in = False
        elif not use_in and not can_out:
            use_in = True
    if use_in and not can_in:
        raise ValueError("A ticked value cannot be written in a filter (a BLOB longer than "
                         "%d bytes or invalid text): untick the others instead" % BLOB_VALUE_MAX)
    if not use_in and not can_out:
        raise ValueError("An unticked value cannot be written in a filter (a BLOB longer "
                         "than %d bytes or invalid text)" % BLOB_VALUE_MAX)
    return condition_text("in" if use_in else "not_in", inc if use_in else exc)


def count_from_distinct(result, text, encoding="utf-8"):
    """Rows the filter text selects among a complete Distinct (None when it cannot tell)."""
    if result is None or result.capped or result.scan_capped or result.merged:
        return None
    if any(isinstance(v, LargeBlob) for v, _n in result.values):
        return None
    if not text:
        return sum(n for _v, n in result.values)
    try:
        e = parse_expr(text)
    except FilterError:
        return None
    return sum(n for v, n in result.values if e.match(v, encoding))


# -- reading a filter back into the builder ----------------------------------------------------------
_SIMPLE_SKIP = frozenset(("and", "or", "in", "notin"))


def _operand_value(operand):
    return operand[1]


def _decode_date(operand, ct):
    kind, x = operand
    if ct.date_kind == tl.ISO:
        return tl.parse_date_text(x) if kind == "text" else None
    return tl.to_utc(x, ct.date_kind) if kind == "num" else None


def _simple_op(e, ct):
    """(op, args) of one simple Expr for the builder of ct, or None."""
    k = e.kind
    base = ct.base if ct.kind == "enum" else ct.kind
    if k in ("empty", "notempty"):
        return ("empty" if k == "empty" else "not_empty"), ()
    if k in ("null", "notnull") and base == "blob":
        return ("null" if k == "null" else "not_null"), ()
    if base == "date":
        if k == "cmp":
            dt = _decode_date(e.operand, ct)
            if dt is None:
                return None
            return {"<": ("before", (dt,)), ">": ("after", (dt,)),
                    ">=": ("between", (dt, None)), "<=": ("between", (None, dt))}.get(e.op)
        if k == "range":
            a, b = _decode_date(e.lo, ct), _decode_date(e.hi, ct)
            return ("between", (a, b)) if a is not None and b is not None else None
        return None
    if base in ("number", "bool"):
        if k == "cmp" and e.operand[0] == "num":
            return _CMP_OP[e.op], (_num_text(e.operand[1]),)
        if k == "range" and e.lo[0] == e.hi[0] == "num":
            return "between", (_num_text(e.lo[1]), _num_text(e.hi[1]))
        return None
    if base == "blob":
        if k == "cmp" and e.op == "=" and e.operand[0] == "blob":
            return "equals", (e.operand[1].hex(),)
        return None
    if k in ("contains", "notcontains", "starts", "ends"):
        return {"contains": "contains", "notcontains": "not_contains", "starts": "starts",
                "ends": "ends"}[k], (e.term,)
    if k == "cmp" and e.op in ("=", "<>") and e.operand[0] == "text":
        return ("equals" if e.op == "=" else "not_equals"), (e.operand[1],)
    if k == "regex":
        t = e.text.strip()
        icase = t.endswith("/i") or t.endswith("/I")
        return "regex", (t[1:-2] if icase else t[1:-1], icase)
    return None


def _num_text(x):
    return repr(x) if isinstance(x, float) else str(x)


def builder_state(text, ct):
    """The builder's fields for filter text: {'conds': [(op, args), ...] (any number),
    'join': the first join ('AND' or 'OR'), 'joins': [the join before each extra cond],
    'list': the IN / NOT IN Expr or None}. Text of another shape comes back as one
    'expr' condition holding it."""
    out = {"conds": [], "join": "AND", "joins": [], "list": None}
    if not text or not text.strip():
        return out
    try:
        e = parse_expr(text)
    except FilterError:
        out["conds"] = [("expr", (text,))]
        return out
    cond = e
    if e.kind in ("in", "notin"):
        out["list"] = e
        return out
    if e.kind == "and" and e.right.kind in ("in", "notin") and e.left.kind not in ("in", "notin"):
        out["list"] = e.right
        cond = e.left
    parts, joins = _flatten_join(cond)
    if len(parts) == 2 and joins == ["AND"]:
        base = ct.base if ct.kind == "enum" else ct.kind
        if base == "date":
            merged = _date_pair(parts[0], parts[1], ct)
            if merged is not None:
                out["conds"] = [merged]
                return out
    mapped = [_simple_op(p, ct) if p.kind not in _SIMPLE_SKIP else None for p in parts]
    if any(m is None for m in mapped):
        out["conds"] = [("expr", (cond.text,))]
        out["join"] = "AND"
        return out
    out["conds"], out["joins"] = mapped, joins
    out["join"] = joins[0] if joins else "AND"
    return out


def _flatten_join(e):
    """A '{{a} AND {b}} OR {c}' tree as ([a, b, c], ['AND', 'OR']). combine() builds
    left-deep trees, so only a left child of boolean kind is flattened (recompiling
    left-deep then reads back exactly); any other compound child stays one piece."""
    if e.kind in ("and", "or") and e.left.kind in ("and", "or"):
        lp, lj = _flatten_join(e.left)
        return lp + [e.right], lj + [e.kind.upper()]
    if e.kind in ("and", "or"):
        return [e.left, e.right], [e.kind.upper()]
    return [e], []


def _date_pair(a, b, ct):
    """A '>= lo' and '< hi' (or <= hi) pair of a date column as one builder condition."""
    if a.kind != "cmp" or b.kind != "cmp" or a.op not in (">=", ">") or b.op not in ("<", "<="):
        return None
    lo, hi = _decode_date(a.operand, ct), _decode_date(b.operand, ct)
    if lo is None or hi is None:
        return None
    if a.op == ">=" and b.op == "<" and _midnight(lo) and hi - lo == timedelta(days=1):
        return "on", (lo,)
    if b.op == "<":
        hi = hi - timedelta(microseconds=1)
    return "between", (lo, hi)


# -- chips: the words of a filter ----------------------------------------------------------------------
def _date_phrase(e, ct):
    """Words for a date condition Expr, or None."""
    k = e.kind
    if k == "cmp":
        dt = _decode_date(e.operand, ct)
        if dt is None:
            return None
        return {"<": "before %s", "<=": "until %s", ">": "after %s", ">=": "from %s",
                "=": "= %s", "<>": "≠ %s"}[e.op] % fmt_when(dt)
    if k == "range":
        a, b = _decode_date(e.lo, ct), _decode_date(e.hi, ct)
        if a is None or b is None:
            return None
        if _midnight(a) and b - a == timedelta(days=1) - timedelta(microseconds=1):
            return "on %s" % fmt_day(a)
        if b.hour == 23 and b.minute == 59 and b.second == 59 and _midnight(a):
            return fmt_range(a, datetime(b.year, b.month, b.day))
        return fmt_range(a, b)
    if k == "and" and e.left.kind == "cmp" and e.right.kind == "cmp" \
            and e.left.op in (">=", ">") and e.right.op in ("<", "<="):
        lo, hi = _decode_date(e.left.operand, ct), _decode_date(e.right.operand, ct)
        if lo is None or hi is None:
            return None
        if e.right.op == "<" and _midnight(lo) and _midnight(hi):
            if hi - lo == timedelta(days=1):
                return "on %s" % fmt_day(lo)
            if lo.day == 1 and hi.day == 1 and (hi.month - lo.month) % 12 == 1 \
                    and hi - lo <= timedelta(days=31):
                return "%s %d" % (MONTHS[lo.month - 1], lo.year)
            return fmt_range(lo, hi - timedelta(days=1))
        if e.right.op == "<" and hi - lo == timedelta(hours=1) and lo.minute == 0:
            return "%s %02d:00–%02d:00" % (fmt_day(lo), lo.hour, hi.hour)
        return fmt_range(lo, hi)
    return None


def _phrase(e, ct=None):
    """(words, as_value): as_value True when the words follow 'column' directly ('= 3',
    'contains ...'), False for a date range written after a colon."""
    k = e.kind
    if ct is not None and ct.kind == "date" and ct.date_kind:
        d = _date_phrase(e, ct)
        if d is not None:
            return d, False
    if k == "cmp":
        return "%s %s" % (_CMP_SIGN[e.op], short_value(_operand_value(e.operand))), True
    if k == "range":
        return "between %s and %s" % (short_value(e.lo[1]), short_value(e.hi[1])), True
    if k in ("contains", "notcontains", "starts", "notstarts", "ends", "notends"):
        verb = {"contains": "contains", "notcontains": "does not contain",
                "starts": "starts with", "notstarts": "does not start with",
                "ends": "ends with", "notends": "does not end with"}[k]
        return "%s %s" % (verb, short_value(e.term)), True
    if k in ("like", "notlike"):
        return "%s '%s'" % ("like" if k == "like" else "not like", e.pattern), True
    if k == "regex":
        return "matches %s" % e.text.strip(), True
    if k in ("null", "notnull", "empty", "notempty"):
        return {"null": "is NULL", "notnull": "is not NULL", "empty": "is empty",
                "notempty": "is not empty"}[k], True
    if k in ("in", "notin"):
        vals = [short_value(x) for _kind, x in e.items] + (["NULL"] if e.has_null else [])
        n = len(vals)
        if n == 1:
            return "%s %s" % ("=" if k == "in" else "≠", vals[0]), True
        if n <= 3:
            return "%s %s" % ("is" if k == "in" else "is not", ", ".join(vals[:-1]) + " or "
                              + vals[-1]), True
        return "%s %s, %s (+%d more)" % ("is one of" if k == "in" else "is none of",
                                         vals[0], vals[1], n - 2), True
    if k in ("and", "or"):
        a, _x = _phrase(e.left, ct)
        b, _y = _phrase(e.right, ct)
        return "%s %s %s" % (a, k, b), True
    return e.text, True


def describe_filter(column, text, ct=None, words=None):
    """Chip words for a column filter ('status = 3', 'body contains 'otp'', 'ts: 1 Mar –
    5 Mar 2026'); for the search of all columns column is None and words the search."""
    if column is None:
        ws = parse_words(words or "")
        return "rows contain %s" % " and ".join("'%s'" % w for w in ws[:4]) + \
            (" (+%d more)" % (len(ws) - 4) if len(ws) > 4 else "")
    try:
        e = parse_expr(text)
    except FilterError:
        return "%s: %s" % (column, text)
    if e is None:
        return column
    if e.kind == "and" and e.right.kind in ("in", "notin") and ct is not None:
        a, _as_value = _phrase(e.left, ct)
        b, _x = _phrase(e.right, ct)
        return "%s: %s, %s" % (column, a, b)
    words_, as_value = _phrase(e, ct)
    return ("%s %s" if as_value else "%s: %s") % (column, words_)


def cut(text, width=48):
    return text if len(text) <= width else text[:width - 1] + "…"


# -- Copy as SQL WHERE ---------------------------------------------------------------------------------
def sql_value(v):
    """An SQL literal for a query parameter."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return "%d" % v
    if isinstance(v, float):
        if v != v:
            return "NULL"
        if v in (float("inf"), float("-inf")):
            return "9e999" if v > 0 else "-9e999"
        return repr(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "X'%s'" % bytes(v).hex()
    return sql_literal(str(v))


def inline_params(sql, params):
    """sql with each ? outside quotes replaced by the literal of the next parameter."""
    out, it, i, n = [], iter(params), 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`[":
            close = "]" if ch == "[" else ch
            j = i + 1
            while True:
                k = sql.find(close, j)
                if k < 0:
                    k = n - 1
                    break
                if close != "]" and sql[k + 1:k + 2] == close:
                    j = k + 2
                    continue
                break
            out.append(sql[i:k + 1])
            i = k + 1
            continue
        if ch == "?":
            try:
                out.append(sql_value(next(it)))
            except StopIteration:
                out.append("?")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def sql_where_text(exprs, words, columns, encoding="utf-8"):
    """'WHERE ...' for {column: filter text} and the words of the search of all columns over
    `columns` (values written in); '' when nothing filters. sga_regexp() is the tool's own
    SQL function for /regex/ filters."""
    try:
        flt = Filter(col_exprs=exprs, words=parse_words(words or ""))
    except FilterError:
        return ""
    if not flt:
        return ""
    where, params = flt.where_sql(list(columns), encoding)
    return ("WHERE " + inline_params(where, params)) if where else ""


def filter_lines(exprs, words):
    """'Copy as filter text': one 'column: expression' line per column filter."""
    lines = ["%s: %s" % (c, t) for c, t in exprs]
    if words:
        lines.append("search: %s" % words)
    return "\n".join(lines)


# ==================================================================================================
# Tk: the checklist, the date histogram, the popover and the bar
# ==================================================================================================
_FONTS = {}


def shared_font(widget, spec):
    """A Font of the spec shared by the interpreter, never deleted (see grid.grid_fonts)."""
    key = (widget.tk, tuple(spec))
    f = _FONTS.get(key)
    if f is None:
        f = _FONTS[key] = tkfont.Font(widget._root(), font=spec)
    return f


def _share_chip_font(chip):
    """parts.Chip makes a Font per chip; a chip in a reference cycle (its tooltip) would be
    freed later by the garbage collector, maybe on a worker thread, where deleting a Font
    waits for the Tk thread. Its Font is swapped for a shared one here (the chip's own is
    freed at once, on this thread)."""
    try:
        chip._font = shared_font(chip, F["small"])
    except (AttributeError, tk.TclError):
        pass
    return chip


def _exists(w):
    try:
        return bool(w.winfo_exists())
    except (tk.TclError, AttributeError, RuntimeError):
        return False


class ValueList(tk.Frame):
    """The checklist: a virtual list drawn on a canvas (only the lines in view exist), each
    line a check box, the value, its count and, for the most frequent values, a bar of the
    distribution. Click or Space toggles a line; Up / Down / Home / End / Page keys move."""

    ROW = 20

    def __init__(self, master, on_toggle=None, rows=8, **kw):
        kw.setdefault("background", K["card"])
        tk.Frame.__init__(self, master, **kw)
        self.on_toggle = on_toggle
        self.cv = tk.Canvas(self, height=rows * self.ROW, background=K["card"],
                            highlightthickness=1, highlightbackground=K["border"],
                            highlightcolor=K["ring"], borderwidth=0, takefocus=1)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.cv.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.entries, self.shown, self.ticked = [], [], set()
        self.query = ""
        self._top, self._active = 0, 0
        self._maxn = 1
        self._font = shared_font(self, F["body"])
        self._small = shared_font(self, F["small"])
        self.cv.bind("<Configure>", lambda e: self.draw())
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>", lambda e: self.scroll(-3))
        self.cv.bind("<Button-5>", lambda e: self.scroll(3))
        for seq, fn in (("<Up>", lambda: self.move(-1)), ("<Down>", lambda: self.move(1)),
                        ("<Prior>", lambda: self.move(-self.page())),
                        ("<Next>", lambda: self.move(self.page())),
                        ("<Home>", lambda: self.move(-len(self.shown))),
                        ("<End>", lambda: self.move(len(self.shown))),
                        ("<space>", self.toggle_active)):
            self.cv.bind(seq, lambda e, f=fn: (f(), "break")[1])
        self.cv.bind("<FocusIn>", lambda e: self.draw())
        self.cv.bind("<FocusOut>", lambda e: self.draw())

    def set_entries(self, entries, ticked=None):
        self.entries = list(entries)
        self.ticked = set(ticked) if ticked is not None else set(e.key for e in self.entries)
        self._maxn = max([e.count for e in self.entries] or [1]) or 1
        self._filter()

    def set_query(self, q):
        self.query = (q or "").strip().lower()
        self._filter()

    def _filter(self):
        q = self.query
        self.shown = [i for i, e in enumerate(self.entries) if not q or q in e.label.lower()]
        self._top = 0
        self._active = 0
        self.draw()

    def page(self):
        return max(1, int(self.cv.winfo_height()) // self.ROW - 1)

    def shown_entries(self):
        return [self.entries[i] for i in self.shown]

    def toggle(self, key):
        if key in self.ticked:
            self.ticked.discard(key)
        else:
            self.ticked.add(key)
        self.draw()
        if self.on_toggle is not None:
            self.on_toggle()

    def toggle_active(self):
        if 0 <= self._active < len(self.shown):
            self.toggle(self.entries[self.shown[self._active]].key)

    def tick_shown(self, on):
        for i in self.shown:
            k = self.entries[i].key
            if on:
                self.ticked.add(k)
            else:
                self.ticked.discard(k)
        self.draw()
        if self.on_toggle is not None:
            self.on_toggle()

    def move(self, d):
        if not self.shown:
            return
        self._active = max(0, min(len(self.shown) - 1, self._active + d))
        vis = self.page()
        if self._active < self._top:
            self._top = self._active
        elif self._active >= self._top + vis:
            self._top = self._active - vis + 1
        self.draw()

    def scroll(self, d):
        vis = self.page()
        self._top = max(0, min(max(0, len(self.shown) - vis), self._top + d))
        self.draw()

    def _wheel(self, e):
        self.scroll(-3 if e.delta > 0 else 3)
        return "break"

    def _yview(self, *args):
        n = max(1, len(self.shown))
        if args and args[0] == "moveto":
            self._top = int(float(args[1]) * n)
        elif args and args[0] == "scroll":
            self._top += int(args[1]) * (self.page() if args[2] == "pages" else 1)
        self.scroll(0)

    def _click(self, e):
        self.cv.focus_set()
        i = self._top + int(e.y // self.ROW)
        if 0 <= i < len(self.shown):
            self._active = i
            self.toggle(self.entries[self.shown[i]].key)

    def visible_lines(self):
        """(label, count text, ticked) of the lines drawn now (for tests and tools)."""
        out = []
        for i in range(self._top, min(len(self.shown), self._top + self.page() + 1)):
            e = self.entries[self.shown[i]]
            out.append((e.label, fmt_int(e.count), e.key in self.ticked))
        return out

    def draw(self):
        cv = self.cv
        try:
            cv.delete("all")
        except tk.TclError:
            return
        W = max(120, int(cv.winfo_width()))
        H = max(self.ROW, int(cv.winfo_height()))
        vis = H // self.ROW + 1
        n = len(self.shown)
        if not n:
            cv.create_text(W / 2, 18, text="No value matches" if self.entries else "",
                           fill=K["muted_text"], font=F["small"])
            self.sb.set(0, 1)
            return
        focus = False
        try:
            focus = cv.focus_get() is cv
        except (tk.TclError, KeyError):
            pass
        bar_w = 48
        for row in range(vis):
            i = self._top + row
            if i >= n:
                break
            e = self.entries[self.shown[i]]
            y = row * self.ROW
            if focus and i == self._active:
                cv.create_rectangle(1, y, W - 1, y + self.ROW, fill=K["selection"], outline="")
            on = e.key in self.ticked
            bx, by = 6, y + 5
            cv.create_rectangle(bx, by, bx + 10, by + 10, outline=K["primary"] if on else
                                K["muted_text"], fill=K["primary"] if on else K["card"])
            if on:
                cv.create_line(bx + 2, by + 5, bx + 4, by + 8, bx + 9, by + 2,
                               fill=K["on_primary"], width=2)
            count = fmt_int(e.count)
            cw = self._small.measure(count)
            right = W - 8
            cv.create_text(right, y + self.ROW / 2, text=count, anchor="e",
                           fill=K["muted_text"], font=F["small"])
            avail = right - cw - 8 - 24
            if not e.blank and e.rank < BAR_VALUES and e.count:
                bw = max(2, int(bar_w * e.count / float(self._maxn)))
                x1 = right - cw - 6
                cv.create_rectangle(x1 - bw, y + 7, x1, y + self.ROW - 7, fill=K["secondary"],
                                    outline="", tags=("bar",))
                avail -= bar_w + 4
            label = e.label if e.expressible else e.label + " (cannot be filtered)"
            cv.create_text(24, y + self.ROW / 2, text=_elide(label, self._font, max(20, avail)),
                           anchor="w", font=F["body"],
                           fill=K["muted_text"] if e.blank or not e.expressible else K["text"])
        f0 = self._top / float(n)
        self.sb.set(f0, min(1.0, (self._top + H // self.ROW) / float(n)))


def _elide(text, font, avail):
    if font.measure(text) <= avail:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= avail:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def _density_class():
    from density import DensityChart

    class ValueDensity(DensityChart):
        """The popover's histogram: bins counted by the source (not a list of times)."""

        def set_bins(self, bins):
            self.keys, self.per, self.selection = None, {}, None
            if bins is None or not bins.edges:
                self.times, self.edges, self.totals = [], [], []
                self.step, self.step_name = None, ""
            else:
                self.times = [bins.edges[0], bins.edges[-1]]
                self.edges, self.totals = list(bins.edges), list(bins.totals)
                self.step, self.step_name = bins.step, bins.step_name
            self.redraw()

        def _bin(self):
            pass                        # the source binned the values

        def redraw(self):
            if not self.totals:
                self.delete("all")
                W, H = self.winfo_width(), self.winfo_height()
                self.create_text(max(W, 200) / 2, max(H, 40) / 2, text=self.empty_text,
                                 fill=K["muted_text"], font=F["small"])
                return
            DensityChart.redraw(self)

        def _motion(self, e):
            DensityChart._motion(self, e)
            for item in self.find_withtag("hover"):
                if self.type(item) == "text":
                    t = self.itemcget(item, "text")
                    self.itemconfigure(item, text=t.replace(" events", " rows")
                                       .replace(" event", " row"))

        empty_text = "Reading the dates…"

    return ValueDensity


class _Line(object):
    """One condition line of the builder: an operator dropdown and its value fields."""

    def __init__(self, pop, parent, ops, index):
        from combobox import SearchableCombobox
        self.pop, self.index, self.ops = pop, index, ops
        self.frame = ttk.Frame(parent, style="Plain.TFrame")
        self.labels = [label for _op, label in ops]
        self.op_var = tk.StringVar(self.frame, value=self.labels[0])
        self.op_box = SearchableCombobox(self.frame, textvariable=self.op_var,
                                         values=self.labels, state="readonly", width=16)
        self.op_box.pack(side="left")
        self.op_box.bind("<<ComboboxSelected>>", lambda e: self._op_changed())
        self.op_var.trace_add("write", lambda *_a: self._op_changed())
        self.args_frame = ttk.Frame(self.frame, style="Plain.TFrame")
        self.args_frame.pack(side="left", fill="x", expand=True, padx=(XS, 0))
        self.fields = {}
        self._op = None
        self._op_changed()

    @property
    def op(self):
        label = self.op_var.get()
        for op, lab in self.ops:
            if lab == label:
                return op
        return self.ops[0][0]

    def set_op(self, op):
        for o, lab in self.ops:
            if o == op:
                if self.op_var.get() != lab:
                    self.op_var.set(lab)
                self._op_changed()
                return True
        return False

    def _op_changed(self):
        op = self.op
        if op == self._op:
            return
        self._op = op
        for w in self.args_frame.winfo_children():
            w.destroy()
        self.fields = {}
        self.pop._build_fields(self, op)
        self.pop._changed()

    def args(self):
        return self.pop._read_fields(self, self.op)


def _expr_help(parent):
    """A small window explaining the filter expression syntax with examples."""
    win = tk.Toplevel(parent)
    win.title("Filter expressions")
    win.transient(parent)
    rows = [
        ("text", "contains \"text\" anywhere"),
        ('"exact text"', "equals exactly (quotes keep spaces)"),
        ("!text", "does NOT contain \"text\" ( ! = not )"),
        (">5   >=5   <5   <=5", "number or date comparison"),
        ("=5   !=5", "equals / does not equal"),
        ("1~10", "between 1 and 10 (either end can be empty: ~10)"),
        ("^=abc", "starts with \"abc\""),
        ("$=xyz", "ends with \"xyz\""),
        ("*=mid", "contains \"mid\" (same as typing mid)"),
        ("/^a.*z$/", "matches the regular expression"),
        ("a%b", "LIKE pattern: % is any run of characters"),
        ("IN (a, b, c)", "equals any of a, b or c"),
        ("NULL   /   NOT NULL", "the value is missing / present"),
        ("EMPTY   /   NOT EMPTY", "the text is empty / not empty"),
        ("{a} OR {b}", "matches a or b   ({a} AND {b}: both)"),
    ]
    frm = ttk.Frame(win, padding=12)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="Type one of these in the filter field:",
              style="M.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                     pady=(0, 8))
    for i, (ex, what) in enumerate(rows, start=1):
        ttk.Label(frm, text=ex, style="Mono.TLabel").grid(
            row=i, column=0, sticky="w", padx=(0, 12), pady=1)
        ttk.Label(frm, text=what).grid(row=i, column=1, sticky="w", pady=1)
    ttk.Label(frm, text="Dates understand 2026-10-02 and epoch numbers; text compares",
              style="Muted.TLabel").grid(row=len(rows) + 1, column=0, columnspan=2,
                                         sticky="w", pady=(10, 0))
    ttk.Label(frm, text="Filters match the values actually stored, not the column\u2019s",
              style="Muted.TLabel").grid(row=len(rows) + 2, column=0, columnspan=2,
                                         sticky="w")
    ttk.Label(frm, text="declared type: 5 and \u20185\u2019 stay different, and a NULL in a",
              style="Muted.TLabel").grid(row=len(rows) + 3, column=0, columnspan=2,
                                         sticky="w")
    ttk.Label(frm, text="NOT NULL column is still found by NULL.",
              style="Muted.TLabel").grid(row=len(rows) + 4, column=0, columnspan=2,
                                         sticky="w")
    ttk.Button(frm, text="Close", command=win.destroy).grid(
        row=len(rows) + 5, column=0, columnspan=2, pady=(10, 0))

class FilterPopover(tk.Toplevel):
    """The filter of one column: a borderless card under its header (see the module doc).

    Escape closes it, Enter applies. compile() -> (filter text or None, message) is what
    Apply would set; the checklist, the live count and the histogram fill in from the grid's
    filter worker while the popover is already usable."""

    WIDTH = 460

    def __init__(self, grid):
        tk.Toplevel.__init__(self, grid)
        self.grid_ = grid
        try:
            # owned by the main window: on Windows an owned popup minimizes with
            # its owner instead of staying on screen (belt and braces with the
            # _watch_min watchdog below)
            self.transient(grid.winfo_toplevel())
        except tk.TclError:
            pass
        try:
            # owned by the main window: on Windows an owned popup minimizes with
            # its owner instead of staying on screen (belt and braces with the
            # _watch_min watchdog below)
            self.transient(grid.winfo_toplevel())
        except tk.TclError:
            pass
        self.alive = False              # shown for a column (the window is kept and reused)
        self.c = None
        self._cancels = {}
        self._seq = 0
        self._count_after = None
        self._areas = {}
        self._shown_once = False
        self.withdraw()
        self.wm_overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.configure(background=K["border"])
        outer = ttk.Frame(self, style="Popover.TFrame", padding=(M, S, M, M))
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = outer
        self._build_shell(outer)
        self.bind("<Escape>", lambda e: (self.close(), "break")[1])
        self.bind("<Return>", self._on_return)
        self.bind("<KP_Enter>", self._on_return)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._click_line = None
        self._tl_tag = None
        self._drag_xy = None            # title-bar drag offset (the popover is borderless)
        self._watch_after = None        # minimize watchdog while shown
        try:
            self.wm_minsize(320, 240)
        except tk.TclError:
            pass

    def show(self, c):
        """Show the filter of column c (the window is built once per grid and reused, so it
        opens at once; the values, count and histogram then fill in from the worker)."""
        self._stop()
        g = self.grid_
        self.c = c
        self.ctx = ctx = g.filter_context(c)
        self.column = ctx["column"]
        self.source = ctx["source"]
        self.ct = detect_type(self.column, ctx["decl"], ctx["sample"], ctx["date_hint"])
        g.note_column_type(c, self.ct)
        self._threshold = {}            # (op, N) -> value found for top / bottom N
        self._nth_key = None
        self._distinct = None
        self._entries = []
        self._only_ticked = False
        self._last_text = None
        self._pending_list = None
        self.bins = None
        self.enum_chips = {}
        self.count_text = ""
        self.message = ""
        self.title_label.configure(text=self.column)
        self.type_label.configure(text=self.ct.label)
        self.values_note.configure(text="Reading the values\u2026")
        self.value_search_var.set("")
        self._base_entries = None
        self.values.set_entries([])
        self._set_count("")
        self.msg_label.configure(text="")
        self.alive = True
        self._build_column()
        self._prefill(ctx["text"])
        self._place()
        self.deiconify()
        self.lift()
        self._bind_outside()
        self._stop_watch()
        self._watch_min()
        self._start_jobs()
        self._changed()
        try:
            if g.focus_get() is not None:
                self.lines[0].op_box.focus_set()
        except (tk.TclError, KeyError):
            pass
        return self

    # -- building ------------------------------------------------------------------------------
    def _build_shell(self, f):
        """What every column's popover has: the title, Sort, the checklist, the buttons."""
        tk.Frame(f, width=self.WIDTH - 2 * M, height=1, background=K["card"]).pack()
        head = ttk.Frame(f, style="Plain.TFrame")
        head.pack(fill="x")
        self.title_label = ttk.Label(head, text="", style="CardHeading.TLabel")
        self.title_label.pack(side="left")
        self.type_label = ttk.Label(head, text="", style="CardMuted.TLabel")
        self.type_label.pack(side="left", padx=(S, 0))
        close = ttk.Button(head, text="\u00d7", width=2, style="Icon.TButton",
                           command=self.close)
        close.pack(side="right")
        for w in (head, self.title_label, self.type_label):   # drag the borderless card
            w.bind("<ButtonPress-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
            w.bind("<ButtonRelease-1>", self._drag_end, add="+")
        sort = ttk.Frame(f, style="Plain.TFrame")
        sort.pack(fill="x", pady=(XS, S))
        self.sort_asc = ttk.Button(sort, text="Sort A \u2192 Z", style="Small.TButton",
                                   command=lambda: self._sort(False))
        self.sort_asc.pack(side="left")
        self.sort_desc = ttk.Button(sort, text="Sort Z \u2192 A", style="Small.TButton",
                                    command=lambda: self._sort(True))
        self.sort_desc.pack(side="left", padx=(XS, 0))
        # the condition builder first (made per column: _build_column)
        self.cond_frame = ttk.Frame(f, style="Plain.TFrame")
        self.cond_frame.pack(fill="x")
        self.col_area = None
        # the checklist of values
        ttk.Separator(f).pack(fill="x", pady=S)
        vhead = ttk.Frame(f, style="Plain.TFrame")
        vhead.pack(fill="x")
        ttk.Label(vhead, text="Values", style="CardHeading.TLabel").pack(side="left")
        self.none_link = ttk.Button(vhead, text="Select none", style="Link.TButton",
                                    command=lambda: self.select_all(False))
        self.none_link.pack(side="right")
        self.all_link = ttk.Button(vhead, text="Select all", style="Link.TButton",
                                   command=lambda: self.select_all(True))
        self.all_link.pack(side="right", padx=(0, XS))
        self.values_note = ttk.Label(f, text="", style="CardMuted.TLabel",
                                     wraplength=self.WIDTH - 2 * M, justify="left")
        self.values_note.pack(fill="x", pady=(XS, XS))
        self.value_search_var = tk.StringVar(self)
        self.value_search = ttk.Entry(f, textvariable=self.value_search_var)
        self.value_search.pack(fill="x")
        try:
            from widgets import add_placeholder
            add_placeholder(self.value_search, self.value_search_var, "Search values\u2026")
        except Exception:               # noqa: BLE001 - a placeholder is a courtesy
            pass
        # while a search is typed: keep only the matches ticked, or add them to the values the
        # filter keeps now (as a spreadsheet's "Add current selection to filter")
        self.add_var = tk.BooleanVar(self, value=False)
        self.add_check = ttk.Checkbutton(f, text="Add the ticked matches to the current filter",
                                         variable=self.add_var, style="Card.TCheckbutton",
                                         command=self._changed)
        self.values = ValueList(f, on_toggle=self._list_toggled, rows=7)
        self.values.pack(fill="both", expand=True, pady=(XS, 0))
        self._vsearch_after = None
        self._base_entries = None       # the list before a whole-column search added matches
        self.value_search_var.trace_add("write", lambda *_a: self._value_search_changed())
        # the live count, the message, the buttons
        self.count_label = ttk.Label(f, text="", style="CardMuted.TLabel")
        self.count_label.pack(fill="x", pady=(S, 0))
        self.msg_label = ttk.Label(f, text="", style="Danger.TLabel",
                                   wraplength=self.WIDTH - 2 * M, justify="left")
        self.msg_label.pack(fill="x")
        btns = ttk.Frame(f, style="Plain.TFrame")
        btns.pack(fill="x", pady=(S, 0))
        self._grip = ttk.Sizegrip(btns)
        self._grip.pack(side="right")
        self.clear_btn = ttk.Button(btns, text="Clear filter", command=self.clear_filter)
        self.clear_btn.pack(side="left")
        self.apply_btn = ttk.Button(btns, text="Apply", style="Primary.TButton",
                                    command=self.apply)
        self.apply_btn.pack(side="right")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.close)
        self.cancel_btn.pack(side="right", padx=(0, XS))

    _AREA_ATTRS = ("col_area", "enum_bar", "lines", "joins", "extra_host",
                   "add_link", "chart", "range_label")

    def _build_column(self):
        """The condition builder of this column's type: built once per kind of column and
        kept (shown again with its fields emptied for the next column of that kind)."""
        self._building = True           # no compiling until every field exists
        try:
            if self.col_area is not None:
                self.col_area.pack_forget()
            key = (self.ct.kind, self.ct.base)
            kept = self._areas.get(key)
            if kept is None:
                self._make_column_fields()
                self._areas[key] = dict((a, getattr(self, a, None)) for a in self._AREA_ATTRS)
            else:
                for a, v in kept.items():
                    setattr(self, a, v)
                self._reset_fields()
            self.col_area.pack(fill="x")
        finally:
            self._building = False

    _building = False
    _areas = None

    def _reset_fields(self):
        for line in list(self.lines[1:]):   # the builder starts with one condition again
            self.remove_condition(line)
        line = self.lines[0]
        line._op = None                 # its fields are made anew (empty)
        if not line.set_op(line.ops[0][0]):
            line._op_changed()
        if self.enum_bar is not None:
            self.enum_bar.clear()
        if self.chart is not None:
            self.chart.empty_text = "Reading the dates\u2026"
            self.chart.set_bins(None)
            self.range_label.configure(text="")

    def _make_column_fields(self):
        area = self.col_area = ttk.Frame(self.cond_frame, style="Plain.TFrame")
        area.pack(fill="x")
        ops = self.ct.ops
        self.enum_bar = None
        if self.ct.kind in ("enum", "bool"):
            from parts import ChipBar
            self.enum_bar = ChipBar(area, bg=K["card"])
            self.enum_bar.pack(fill="x", pady=(0, XS))
        self.lines = [_Line(self, area, ops, 0)]
        self.lines[0].frame.pack(fill="x")
        self.joins = []                 # one {"var", "box", "row"} per line after the first
        self.extra_host = ttk.Frame(area, style="Plain.TFrame")
        self.extra_host.pack(fill="x")
        self.add_link = ttk.Button(area, text="+ Add condition", style="Link.TButton",
                                   command=self.add_condition)
        self.add_link.pack(anchor="w", pady=(XS, 0))
        # a date column: its dates as a histogram whose drag sets 'between'
        self.chart = None
        if self.ct.kind == "date":
            cls = _density_class()
            self.chart = cls(area, on_range=self._chart_range, height=70)
            self.chart.pack(fill="x", pady=(S, 0))
            self.chart.set_bins(None)
            self.range_label = ttk.Label(area, text="", style="CardMuted.TLabel")
            self.range_label.pack(anchor="w")

    def add_condition(self):
        """Append another condition line, joined to the previous one by AND / OR."""
        from combobox import SearchableCombobox
        row = ttk.Frame(self.extra_host, style="Plain.TFrame")
        row.pack(fill="x", pady=(XS, 0))
        var = tk.StringVar(row, value="AND")
        box = SearchableCombobox(row, textvariable=var, values=("AND", "OR"),
                                 state="readonly", width=5)
        box.pack(side="left")
        var.trace_add("write", lambda *_a: self._changed())
        line = _Line(self, row, self.ct.ops, len(self.lines))
        line.frame.pack(side="left", fill="x", expand=True, padx=(XS, 0))
        rm = ttk.Button(row, text="×", width=2, style="Icon.TButton",
                        command=lambda ln=line: self.remove_condition(ln))
        rm.pack(side="left")
        self.joins.append({"var": var, "box": box, "row": row})
        self.lines.append(line)
        self._changed()
        return line

    def remove_condition(self, line):
        """Drop one of the extra condition lines (the first one always stays)."""
        if line in self.lines[1:]:
            i = self.lines.index(line)
            self.joins[i - 1]["row"].destroy()
            del self.joins[i - 1]
            self.lines.remove(line)
            for k, ln in enumerate(self.lines):
                ln.index = k
            self._changed()

    def add_second(self, show=True):
        """Backward-compatible alias: the builder used to allow exactly two lines."""
        if show:
            if len(self.lines) < 2:
                self.add_condition()
        else:
            for line in list(self.lines[1:]):
                self.remove_condition(line)

    def _build_fields(self, line, op):
        """The value fields an operator takes, in line.args_frame."""
        from combobox import SearchableCombobox
        from widgets import ToolTip
        f = line.args_frame
        ch = lambda *_a: self._changed()     # noqa: E731
        base = self.ct.base if self.ct.kind == "enum" else self.ct.kind
        if op in NO_ARG:
            return
        if op == "expr":
            var = tk.StringVar(f)
            e = ttk.Entry(f, textvariable=var, width=30)
            e.pack(side="left", fill="x", expand=True)
            hb = ttk.Button(f, text="?", width=2,
                            command=lambda: _expr_help(self.winfo_toplevel()))
            hb.pack(side="left", padx=(4, 0))
            ToolTip(hb, "How to write a filter expression (with examples)")
            var.trace_add("write", ch)
            line.fields["text"] = var
            line.fields["widget"] = e
            return
        if base == "date":
            if op == "between":
                from datepicker import DateRangePicker
                p = DateRangePicker(f, on_change=lambda a, b: self._changed(), width=17)
                p.pack(side="left", fill="x", expand=True)
                p.start.var.trace_add("write", ch)
                p.end.var.trace_add("write", ch)
                line.fields["range"] = p
                line.fields["widget"] = p.start.entry
            elif op == "last_days":
                var = tk.StringVar(f, value="7")
                e = ttk.Entry(f, textvariable=var, width=6)
                e.pack(side="left")
                ttk.Label(f, text="days", style="CardMuted.TLabel").pack(side="left", padx=XS)
                var.trace_add("write", ch)
                line.fields["n"] = var
                line.fields["widget"] = e
            else:
                from datepicker import DateTimePicker
                p = DateTimePicker(f, on_change=lambda dt: self._changed(), width=19,
                                   end=False)
                p.pack(side="left", fill="x", expand=True)
                p.var.trace_add("write", ch)
                line.fields["date"] = p
                line.fields["widget"] = p.entry
            return
        if op in ("top", "bottom"):
            var = tk.StringVar(f, value="10")
            e = ttk.Entry(f, textvariable=var, width=6)
            e.pack(side="left")
            ttk.Label(f, text="rows", style="CardMuted.TLabel").pack(side="left", padx=XS)
            var.trace_add("write", ch)
            line.fields["n"] = var
            line.fields["widget"] = e
            return
        if base in ("number", "bool") or base == "blob":
            v1 = tk.StringVar(f)
            e1 = ttk.Entry(f, textvariable=v1, width=12 if op == "between" else 18)
            e1.pack(side="left")
            v1.trace_add("write", ch)
            line.fields["a"] = v1
            line.fields["widget"] = e1
            if op == "between":
                ttk.Label(f, text="and", style="CardMuted.TLabel").pack(side="left", padx=XS)
                v2 = tk.StringVar(f)
                e2 = ttk.Entry(f, textvariable=v2, width=12)
                e2.pack(side="left")
                v2.trace_add("write", ch)
                line.fields["b"] = v2
            return
        if op == "regex":
            var = tk.StringVar(f)
            e = ttk.Entry(f, textvariable=var, width=22)
            e.pack(side="left", fill="x", expand=True)
            icase = tk.BooleanVar(f, value=True)
            ttk.Checkbutton(f, text="any case", variable=icase, command=ch).pack(side="left",
                                                                                padx=XS)
            var.trace_add("write", ch)
            line.fields["text"] = var
            line.fields["icase"] = icase
            line.fields["widget"] = e
            return
        var = tk.StringVar(f)
        values = [v for v, _n in (self._distinct.values if self._distinct else [])
                  if isinstance(v, str) and v][:200]
        box = SearchableCombobox(f, textvariable=var, values=values, state="normal", width=22,
                                 recent_key="colfilter:" + self.column,
                                 placeholder="value…")
        box.pack(side="left", fill="x", expand=True)
        var.trace_add("write", ch)
        line.fields["text"] = var
        line.fields["box"] = box
        line.fields["widget"] = box

    def _read_fields(self, line, op):
        fl = line.fields
        base = self.ct.base if self.ct.kind == "enum" else self.ct.kind
        if op in NO_ARG:
            return ()
        if op == "expr":
            return (fl["text"].get(),)
        if base == "date":
            if op == "between":
                p = fl["range"]
                return (p.start.get_utc(), p.end.get_utc())
            if op == "last_days":
                return (fl["n"].get(),)
            return (fl["date"].get_utc(),)
        if op in ("top", "bottom"):
            return (fl["n"].get(),)
        if "a" in fl:
            return (fl["a"].get(), fl["b"].get()) if "b" in fl else (fl["a"].get(),)
        if op == "regex":
            return (fl["text"].get(), bool(fl["icase"].get()))
        return (fl["text"].get(),)

    def _field_errors(self, line):
        """A date field whose text cannot be read: its message."""
        fl = line.fields
        for key in ("date", "range"):
            p = fl.get(key)
            if p is not None:
                try:
                    msg = p.error()
                except (tk.TclError, AttributeError):
                    msg = ""
                if msg:
                    return msg
        return ""

    def set_line(self, index, op, args=()):
        """Fill condition line `index` (adding lines as needed) as a user would: an
        operator and its values (text, numbers as text, datetimes for dates)."""
        while len(self.lines) <= index:
            self.add_condition()
        line = self.lines[index]
        line.set_op(op)
        fl = line.fields
        args = tuple(args)
        if "range" in fl:
            fl["range"].set_range(args[0] if args else None, args[1] if len(args) > 1 else None)
        elif "date" in fl:
            fl["date"].set_utc(args[0] if args else None)
        elif "n" in fl:
            if args:
                fl["n"].set(str(args[0]))
        elif "a" in fl:
            fl["a"].set(str(args[0]) if args else "")
            if "b" in fl:
                fl["b"].set(str(args[1]) if len(args) > 1 else "")
        elif "text" in fl:
            fl["text"].set(args[0] if args else "")
            if "icase" in fl and len(args) > 1:
                fl["icase"].set(bool(args[1]))
        self._changed()

    def _prefill(self, text):
        st = builder_state(text, self.ct)
        self._pending_list = st["list"]
        conds = st["conds"]
        joins = st.get("joins") or []
        for i, (op, args) in enumerate(conds):
            self.set_line(i, op, args)
        for i, join in enumerate(joins):
            if i < len(self.joins) and self.joins[i]["var"].get() != join:
                self.joins[i]["var"].set(join)

    def _place(self):
        """Under the column's header, kept on the screen. The window takes its natural size
        (it follows the builder of each kind of column); the first showing lays it out now
        so its height is known, later ones reuse it."""
        g = self.grid_
        try:
            x, y = g.header_cell_root(self.c)
            if not self._shown_once:
                self.update_idletasks()
                self._shown_once = True
            vx, vy, vw, vh = self._virtual_screen()
            x = max(vx, min(x, vx + vw - self.WIDTH - 8))
            h = self.winfo_reqheight()
            if y + h > vy + vh - 40:
                y = max(vy, vy + vh - h - 40)
            self.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass

    def _virtual_screen(self):
        """(x, y, width, height) of the virtual screen covering all monitors. Clamping to
        winfo_screenwidth/height (the primary monitor on Windows) shoves the popover onto
        the wrong monitor when the grid is on another one; winfo_vroot* is not reliable
        on Windows either, so the real virtual-screen metrics are used there."""
        if sys.platform == "win32":
            try:
                from ctypes import windll
                g = windll.user32.GetSystemMetrics
                return (g(76), g(77), g(78), g(79))  # SM_X/Y/CX/CYVIRTUALSCREEN
            except (OSError, AttributeError):
                pass
        try:
            return (self.winfo_vrootx(), self.winfo_vrooty(),
                    self.winfo_vrootwidth(), self.winfo_vrootheight())
        except tk.TclError:
            return (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())

    # -- dragging the borderless card ----------------------------------------------------------
    def _drag_start(self, e):
        if not self.alive:
            return
        try:
            self._drag_xy = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())
        except tk.TclError:
            self._drag_xy = None

    def _drag_move(self, e):
        if not self.alive or not self._drag_xy:
            return
        try:
            x = e.x_root - self._drag_xy[0]
            y = e.y_root - self._drag_xy[1]
            vx, vy, vw, vh = self._virtual_screen()
            x = max(vx - self.winfo_width() + 60, min(x, vx + vw - 60))
            y = max(vy, min(y, vy + vh - 40))
            self.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass

    def _drag_end(self, _e):
        self._drag_xy = None

    # -- closing when the main window is minimized -------------------------------------------
    def _tl_hidden(self):
        """True when the main window is minimized or withdrawn. winfo_ismapped() alone is
        not enough: on Windows a minimized window still reports as mapped, so wm_state()
        == "iconic" is checked too."""
        try:
            tl = self._tl
        except AttributeError:
            return True
        try:
            if not tl.winfo_ismapped():
                return True
        except tk.TclError:
            return True
        try:
            if tl.wm_state() == "iconic":
                return True
        except tk.TclError:
            pass
        return False

    def _watch_min(self):
        """Close when the main window is minimized: a fallback for platforms where the
        <Unmap> binding does not fire for borderless windows (checked twice a second,
        only while the popover is shown)."""
        try:
            if not self.alive:
                return
            if self._tl_hidden():
                self.close()
                return
        except tk.TclError:
            return
        if self.alive:
            self._watch_after = self.after(500, self._watch_min)

    def _stop_watch(self):
        if self._watch_after is not None:
            try:
                self.after_cancel(self._watch_after)
            except tk.TclError:
                pass
            self._watch_after = None

    # -- worker jobs ---------------------------------------------------------------------------
    def _job(self, name, fn, done):
        """fn(cancel) on the grid's filter worker, done(result, error) here; a newer job of
        the same name cancels this one (a running SQL read is interrupted)."""
        self._cancel_job(name)
        ev = threading.Event()
        self._cancels[name] = ev
        self._seq += 1
        key = ("colfilter", id(self), name, self._seq)

        def run():
            if ev.is_set():
                raise Cancelled()
            return fn(ev.is_set)

        def finish(result, error):
            if ev.is_set() or not self.alive:
                return
            if self._cancels.get(name) is ev:
                del self._cancels[name]
            done(result, error)
        self.grid_.filter_runner().submit(key, run, finish)

    def _cancel_job(self, name):
        ev = self._cancels.pop(name, None)
        if ev is None:
            return
        ev.set()
        runner = self.grid_.filter_runner()
        me = id(self)
        runner.discard(lambda k: not (k[0] == "colfilter" and k[1] == me and k[2] == name))
        key, th = runner.running()
        if key is not None and key[0] == "colfilter" and key[1] == me and key[2] == name:
            hook = getattr(self.source, "interrupt", None)
            if hook is not None and th is not None:
                try:
                    hook(th)
                except Exception:       # noqa: BLE001 - it then just finishes
                    pass

    def jobs_pending(self):
        return sorted(self._cancels)

    def _start_jobs(self):
        src, flt = self.source, self.ctx["others"]
        if src is None:
            return
        column = self.column
        limit, scan = limits.get("filter_distinct_values"), limits.get("filter_distinct_scan_rows")
        cache_key = (column, flt.key() if flt else None, limit, scan)
        hit = self.grid_.distinct_cache(cache_key)
        if hit is not None:
            self._got_distinct(hit, None)
        else:
            def done(result, error):
                if error is None:
                    self.grid_.distinct_cache(cache_key, result)
                self._got_distinct(result, error)
            self._job("distinct", lambda cancel: source_distinct(src, column, flt, limit, scan,
                                                                 cancel), done)
        if self.chart is not None:
            kind = self.ct.date_kind
            most = 60
            self._job("bins", lambda cancel: source_date_bins(src, column, flt, kind, most, scan,
                                                              cancel), self._got_bins)

    def _value_search_changed(self):
        """Typing in 'Search values': the list shows the matches at once; when the list holds
        only the most frequent values (a limit cut it), the whole column is searched too
        (after a pause in typing) and the matches found there join the list."""
        q = self.value_search_var.get()
        self.values.set_query(q)
        if q.strip():
            if not self.add_check.winfo_manager():
                self.add_check.pack(fill="x", pady=(XS, 0), before=self.values)
        elif self.add_check.winfo_manager():
            self.add_check.pack_forget()
            self.add_var.set(False)
        if self._vsearch_after is not None:
            try:
                self.after_cancel(self._vsearch_after)
            except tk.TclError:
                pass
            self._vsearch_after = None
        d = self._distinct
        if q.strip() and d is not None and (d.capped or d.scan_capped):
            self._vsearch_after = self.after(350, lambda: self._search_column(q.strip()))
        self._changed()

    def _search_column(self, q):
        """The distinct values of the whole column that contain q (within the other filters),
        read on the filter worker; they join the list, ticked."""
        self._vsearch_after = None
        if not self.alive or self.source is None or self.c is None:
            return
        try:
            flt = self.grid_.filter_with(self.c, q)
        except Exception:               # noqa: BLE001 - a term that cannot be a filter
            return
        src, column = self.source, self.column
        limit, scan = limits.get("filter_distinct_values"), limits.get("filter_distinct_scan_rows")
        self.values_note.configure(text="Searching the whole column for \u201c%s\u201d\u2026" % q)

        def done(result, error):
            if error is not None or result is None or self.value_search_var.get().strip() != q:
                if error is not None and not isinstance(error, Cancelled):
                    self.values_note.configure(text="Could not search the column: %s" % error)
                return
            have = set(e.key for e in self._entries)
            if self._base_entries is None:
                self._base_entries = list(self._entries)
            new = [e for e in build_entries(result.values, self.ctx["display"])
                   if e.key not in have]
            ticked = set(self.values.ticked) | set(e.key for e in new)
            self._entries = self._entries + new
            self.values.set_entries(self._entries, ticked)
            self.values.set_query(q)
            n = len(self.values.shown)
            self.values_note.configure(text="%s value%s contain \u201c%s\u201d in the whole "
                                            "column%s" % (
                fmt_int(n), "" if n == 1 else "s", q,
                " (the %s most frequent: limit filter_distinct_values)" % fmt_int(n)
                if result.capped else ""))
            self._changed()
        self._job("vsearch", lambda cancel: source_distinct(src, column, flt, limit, scan,
                                                            cancel), done)

    def _got_distinct(self, result, error):
        if error is not None:
            if not isinstance(error, Cancelled):
                self.values_note.configure(text="Could not read the values: %s" % error)
            return
        self._distinct = result
        display = self.ctx["display"]
        self._entries = build_entries(result.values, display)
        ticked = set(e.key for e in self._entries)
        lst = self._pending_list
        if lst is not None:
            listed = set(_vkey(o[1]) for o in lst.items)
            if lst.has_null:
                listed.add(_vkey(None))
            has_blank_items = lst.has_null or any(o == ("text", "") for o in lst.items)
            if lst.kind == "in":
                ticked = set(e.key for e in self._entries
                             if (e.blank and has_blank_items) or
                             (not e.blank and e.key in listed))
                self._only_ticked = not (result.total <= len(result.values)
                                         and not result.scan_capped)
            else:
                ticked = set(e.key for e in self._entries
                             if not ((e.blank and has_blank_items) or
                                     (not e.blank and e.key in listed)))
        self.values.set_entries(self._entries, ticked)
        self.values_note.configure(text=self._values_note_text(result))
        # text lines suggest the column's frequent values
        for line in self.lines:
            box = line.fields.get("box")
            if box is not None:
                try:
                    box.configure(values=[v for v, _n in result.values
                                          if isinstance(v, str) and v][:200])
                except tk.TclError:
                    pass
        self._fill_chips()
        self._last_text = None          # counted from the values now, when they are all read
        self._changed()

    def _values_note_text(self, r):
        parts = []
        n = len(r.values)
        if r.capped:
            parts.append("showing the %s most frequent of %s values (limit "
                         "filter_distinct_values)" % (fmt_int(n), fmt_int(r.total)))
        else:
            parts.append("%s distinct value%s" % (fmt_int(r.total), "" if r.total == 1 else "s"))
        if r.scan_capped:
            parts.append("counted in the first %s rows only (limit filter_distinct_scan_rows)"
                         % fmt_int(r.scanned))
        others = self.ctx["others_count"]
        if others:
            parts.append("in the rows the other %s filter%s keep" % (
                "" if others == 1 else str(others), "" if others == 1 else "s"))
        text = "; ".join(parts)
        return text[:1].upper() + text[1:]

    def _got_bins(self, bins, error):
        if self.chart is None:
            return
        if error is not None:
            if not isinstance(error, Cancelled):
                self.chart.empty_text = "Could not read the dates: %s" % error
                self.chart.set_bins(None)
            return
        if not bins.edges:
            self.chart.empty_text = "No value reads as a date of this kind"
        self.chart.set_bins(bins)
        if bins.first is not None:
            text = "%s – %s (UTC), %s rows with a date" % (
                fmt_when(bins.first), fmt_when(bins.last), fmt_int(bins.rows))
            if bins.scan_capped:
                text += "; the first %s rows only (limit filter_distinct_scan_rows)" \
                    % fmt_int(bins.scanned)
            self.range_label.configure(text=text)
        self.bins = bins

    def _fill_chips(self):
        if self.enum_bar is None or not self._entries:
            return
        if len(self._entries) > ENUM_MAX + 1:
            return
        self.enum_bar.clear()
        self.enum_chips = {}
        for e in self._entries:
            chip = _share_chip_font(self.enum_bar.add_chip(
                e.label, toggle=True, on=e.key in self.values.ticked, count=fmt_int(e.count),
                on_change=lambda on, k=e.key: self._chip_toggled(k)))
            self.enum_chips[e.key] = chip

    def _chip_toggled(self, key):
        self.values.toggle(key)

    def _list_toggled(self):
        chips = getattr(self, "enum_chips", {})
        for k, chip in chips.items():
            on = k in self.values.ticked
            if chip.on != on:
                chip.set(on=on)
        self._changed()

    def select_all(self, on):
        """Select all / Select none (of the values shown by the value search)."""
        if not self.values.query:
            self._only_ticked = not on
        self.values.tick_shown(on)

    def _chart_range(self, start, end):
        if start is None:
            return
        self.set_line(0, "between", (start, end))

    # -- compiling -----------------------------------------------------------------------------
    def _threshold_for(self, line):
        op = line.op
        if op not in ("top", "bottom"):
            return None
        try:
            n = parse_count(line.args()[0])
        except (ValueError, IndexError):
            return None
        key = (op, n)
        if key in self._threshold:
            return self._threshold[key]
        if self._nth_key == key and "nth" in self._cancels:
            return None                 # being found
        self._nth_key = key
        src, flt, column = self.source, self.ctx["others"], self.column
        if src is None:
            return None

        def done(value, error):
            if error is None:
                self._threshold[key] = value
            elif not isinstance(error, Cancelled):
                self._threshold[key] = None
            self._changed()
        self._job("nth", lambda cancel: source_nth(src, column, flt, n, op == "top", cancel),
                  done)
        return None

    _nth_key = None

    def compile(self):
        """(filter text or None for no filter, message): what Apply would set; message says
        why Apply cannot be used now ('' when it can)."""
        try:
            parts = []
            for line in self.lines:
                bad = self._field_errors(line)
                if bad:
                    return None, bad
                th = self._threshold_for(line)
                if line.op in ("top", "bottom") and th is None:
                    args = line.args()
                    if args and str(args[0]).strip():
                        parse_count(args[0])
                        if (line.op, parse_count(args[0])) in self._threshold:
                            return None, "No number in this column"
                        return None, "Finding the threshold…"
                parts.append(compile_condition(self.ct, line.op, line.args(), threshold=th))
            cond = parts[0]
            for j, p in zip(self.joins, parts[1:]):
                if p:
                    cond = combine(cond, j["var"].get(), p) if cond else p
            lst = None
            if self._entries and self.values.query:
                # a search is typed: the filter keeps the ticked matches only, or, with 'Add the
                # ticked matches to the current filter', those and every value ticked before
                shown = self.values.shown_entries()
                keys = set(e.key for e in shown)
                pick = shown + ([e for e in self._entries if e.key not in keys]
                                if self.add_var.get() else [])
                lst = checklist_text(pick, self.values.ticked, False, True)
            elif self._entries:
                complete = not self._distinct.capped and not self._distinct.scan_capped
                lst = checklist_text(self._entries, self.values.ticked, complete,
                                     self._only_ticked)
            elif self._pending_list is not None:
                lst = self._pending_list.text   # the values are still being read: kept as is
            return join_conditions([cond, lst]), ""
        except NothingTicked as e:
            return None, str(e)
        except (ValueError, TypeError, FilterError) as e:
            return None, str(e)

    def description(self, text):
        base = describe_filter(self.column, text, self.ct)
        for line in self.lines:
            if line.op in ("top", "bottom"):
                th = self._threshold_for(line)
                if th is not None:
                    n = parse_count(line.args()[0])
                    return "%s: %s %d (%s %s)" % (self.column, line.op, n,
                                                   "≥" if line.op == "top" else "≤",
                                                   short_value(th))
        return base

    # -- live count ----------------------------------------------------------------------------
    def _changed(self):
        if not self.alive or self._building:
            return
        text, msg = self.compile()
        self.message = msg
        try:
            self.msg_label.configure(text=msg if msg and not msg.endswith("…") else "")
            self.apply_btn.state(["disabled"] if msg else ["!disabled"])
        except tk.TclError:
            return
        if msg:
            self._stop_count()
            self._last_text = None
            self._set_count(msg if msg.endswith("…") else "")
            return
        if text == self._last_text and self.count_text:
            return
        self._last_text = text
        self._stop_count()
        exact = count_from_distinct(self._distinct, text, self.ctx["encoding"])
        if exact is not None:
            self._show_count(exact, True)
            return
        self._set_count("counting…")
        self._count_after = self.after(COUNT_DELAY_MS, lambda: self._count(text))

    def _stop_count(self):
        """The count of what the popover showed before is not wanted any more."""
        if self._count_after is not None:
            try:
                self.after_cancel(self._count_after)
            except tk.TclError:
                pass
            self._count_after = None
        self._cancel_job("count")
        self._cancel_job("estimate")

    def _count(self, text):
        self._count_after = None
        if not self.alive:
            return
        src = self.source
        flt = self.grid_.filter_with(self.c, text)
        if src is None:
            return

        def exact_done(n, error):
            self._cancel_job("estimate")
            if error is None and n is not None:
                self._show_count(n, True)
            elif error is not None and not isinstance(error, Cancelled):
                self._set_count("could not count: %s" % error)

        def est_done(res, error):
            if error is None and res is not None and "count" in self._cancels:
                self._show_count(res[0], False)
        self._job("count", lambda cancel: source_count(src, flt, cancel), exact_done)
        self._job("estimate", lambda cancel: source_estimate(src, flt, cancel), est_done)

    def _show_count(self, n, exact):
        total = self.ctx["total"]
        text = ("%s" if exact else "≈ %s") % fmt_int(n)
        if total is not None:
            text += " of %s rows" % fmt_compact(total)
        else:
            text += " rows"
        self._set_count(text + ("" if exact else " (estimate; counting all…)"))

    def _set_count(self, text):
        self.count_text = text
        try:
            self.count_label.configure(text=text)
        except tk.TclError:
            pass

    # -- actions -------------------------------------------------------------------------------
    def _on_return(self, _e=None):
        self.apply()
        return "break"

    def apply(self):
        """Set the column's filter to what the popover shows and close it; False (and the
        popover stays) when it cannot be used now."""
        text, msg = self.compile()
        if msg:
            self.message = msg
            try:
                self.msg_label.configure(text=msg)
            except tk.TclError:
                pass
            return False
        from combobox import record_recent
        for line in self.lines:
            if "box" in line.fields:
                t = line.fields["text"].get()
                if t:
                    record_recent("colfilter:" + self.column, t)
        desc = self.description(text) if text else ""
        self.grid_.apply_column_filter(self.c, text or "", desc)
        self.close()
        return True

    def clear_filter(self):
        self.grid_.apply_column_filter(self.c, "")
        self.close()

    def _sort(self, desc):
        self.grid_.sort_by(self.c, desc)
        self.close()

    # -- closing when the user clicks elsewhere (as the searchable dropdowns do) -------------
    def _bind_outside(self):
        """A click anywhere outside the popover (and its own dropdowns and calendars) closes
        it, as does moving or minimising the main window. The line added to the 'all'
        bindings is removed again on close, leaving any others as they were."""
        if self._click_line is not None:
            return
        self._click_cmd = self.register(self._on_global_click)
        self._click_line = "%s %%W" % self._click_cmd
        try:
            script = str(self.tk.call("bind", "all", "<ButtonPress>") or "")
            self.tk.call("bind", "all", "<ButtonPress>",
                         (script + "\n" + self._click_line) if script else self._click_line)
            tl = self.grid_.winfo_toplevel()
            self._tl, self._tl_geom = tl, tl.winfo_geometry()
            self._tl_tag = "ColumnFilter%d" % id(self)
            tl.bindtags((self._tl_tag,) + tuple(tl.bindtags()))
            tl.bind_class(self._tl_tag, "<Configure>", self._on_toplevel_change)
            tl.bind_class(self._tl_tag, "<Unmap>", lambda e: self._on_toplevel_change(e, True))
        except tk.TclError:
            pass

    def _unbind_outside(self):
        line = self._click_line
        if line:
            self._click_line = None
            try:
                script = str(self.tk.call("bind", "all", "<ButtonPress>") or "")
                keep = [ln for ln in script.split("\n") if ln.strip() != line]
                self.tk.call("bind", "all", "<ButtonPress>", "\n".join(keep))
            except tk.TclError:
                pass
            try:
                self.deletecommand(self._click_cmd)
            except (tk.TclError, ValueError):
                pass
        tag = self._tl_tag
        if tag:
            self._tl_tag = None
            try:
                self._tl.bindtags(tuple(t for t in self._tl.bindtags() if t != tag))
                for seq in ("<Configure>", "<Unmap>"):
                    self._tl.unbind_class(tag, seq)
            except tk.TclError:
                pass

    def _on_global_click(self, path):
        if not self.alive:
            return
        if not str(path).startswith(str(self)):
            self.close()

    def _on_toplevel_change(self, event, unmapped=False):
        if not self.alive:
            return
        try:
            if str(event.widget) != str(self._tl):
                return
            if unmapped or self._tl.winfo_geometry() != self._tl_geom:
                self.close()
        except tk.TclError:
            pass

    def _on_destroy(self, e):
        if e.widget is self:
            self._stop()
            self._unbind_outside()
            self.alive = False

    def _stop(self):
        """Cancel the reads of the column shown until now."""
        self._stop_watch()
        for name in list(self._cancels):
            self._cancel_job(name)
        if self._count_after is not None:
            try:
                self.after_cancel(self._count_after)
            except tk.TclError:
                pass
            self._count_after = None

    def close(self):
        """Hide the popover (kept, with the builders made so far, for the next column) and
        stop its reads."""
        if not self.alive:
            return
        self.alive = False
        self._stop()
        self._unbind_outside()
        try:
            self.withdraw()
        except tk.TclError:
            pass
        self.grid_.popover_closed(self)


class FilterBar(tk.Frame):
    """The bar above a grid: the search of all columns, Back / Forward, Saved ▾, the filter
    row toggle and the count on one line; below it, while a filter is in force, a chip per
    filter (click: edit it; × / Delete / Backspace: remove it), Clear all, Save filter… and
    Copy ▾."""

    def __init__(self, master, grid, search=True):
        tk.Frame.__init__(self, master, background=K["background"])
        from widgets import SearchBox, ToolTip
        from parts import ChipBar
        self.grid_ = grid
        self.tools = tk.Frame(self, background=K["background"])
        self.tools.pack(fill="x", padx=XS, pady=(XS, 0))
        self.search = None
        if search:
            self.search = SearchBox(self.tools, placeholder="Search in rows…", delay=400,
                                    find_button=False, width=28,
                                    on_change=lambda t: grid.set_global_filter(t, True),
                                    on_next=lambda forward: grid.focus_set(),
                                    tooltip="Keeps the rows where every word occurs in some "
                                            "column (any case); matches are highlighted")
            self.search.pack(side="left")
        self.back = ttk.Button(self.tools, text="◂", width=2, style="Icon.TButton",
                               command=grid.undo_filter)
        self.fwd = ttk.Button(self.tools, text="▸", width=2, style="Icon.TButton",
                              command=grid.redo_filter)
        self.back.pack(side="left", padx=(S, 0))
        self.fwd.pack(side="left")
        ToolTip(self.back, "Back to the previous filters and sort (Ctrl+Z in the grid)")
        ToolTip(self.fwd, "Forward again (Ctrl+Y in the grid)")
        self.saved_btn = ttk.Button(self.tools, text="Saved ▾", style="Small.TButton",
                                    command=self._saved_menu)
        self.row_btn = ttk.Button(self.tools, text="Filter row", style="Small.TButton",
                                  command=lambda: grid.set_inline_filters(
                                      not grid.inline_filters()))
        self.row_btn.pack(side="left", padx=(S, 0))
        ToolTip(self.row_btn, "Show or hide a filter field under each column header")
        self.count_label = tk.Label(self.tools, text="", background=K["background"],
                                    foreground=K["muted_text"], font=F["small"])
        self.count_label.pack(side="right", padx=(S, XS))
        self.chips = ChipBar(self, bg=K["background"])
        self._chips_key = None
        self.menu = None

    def _post(self, menu, widget):
        try:
            menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            menu.grab_release()

    def saved_menu(self):
        """The Saved ▾ menu (not posted): one entry per saved filter of this table."""
        g = self.grid_
        if self.menu is not None:
            try:
                self.menu.destroy()
            except tk.TclError:
                pass
        m = self.menu = tk.Menu(self, tearoff=0)
        saved = g.saved_filter_sets()
        for name in sorted(saved, key=lambda s: s.lower()):
            m.add_command(label=name, command=lambda n=name: g.apply_saved_filter(saved[n]))
        if not saved:
            m.add_command(label="No saved filters for this table", state="disabled")
        return m

    def _saved_menu(self):
        self._post(self.saved_menu(), self.saved_btn)

    def copy_menu(self):
        g = self.grid_
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Copy as SQL WHERE", command=lambda: g.copy_text(g.sql_where()))
        m.add_command(label="Copy as filter text", command=lambda: g.copy_text(g.filter_text()))
        return m

    def update_bar(self):
        """Show what the grid's filters are now (called by the grid after each change)."""
        g = self.grid_
        chips = g.filter_chips()
        key = tuple((k, text, bad) for k, text, _tip, bad in chips) + \
            (g.saved_filters is not None,)
        if key != self._chips_key:
            self._chips_key = key
            self.chips.clear()
            for k, text, tip, bad in chips:
                _share_chip_font(self.chips.add_chip(
                    cut(("invalid: " if bad else "") + text), closable=True, active=not bad,
                    tooltip=tip, on_click=lambda k=k: g.edit_filter(k),
                    on_close=lambda k=k: g.remove_filter(k)))
            if chips:
                for text, cmd in (("Clear all", g.clear_filters),
                                  ("Save filter…", g.save_filter_dialog)
                                  if g.saved_filters is not None else (None, None)):
                    if text:
                        b = ttk.Button(self.chips, text=text, style="Link.TButton", command=cmd)
                        self.chips.add(b, gap=S)
                cb = ttk.Button(self.chips, text="Copy ▾", style="Link.TButton")
                cb.configure(command=lambda b=cb: self._post(self.copy_menu(), b))
                self.chips.add(cb, gap=S)
        if chips:
            if not self.chips.winfo_manager():
                self.chips.pack(fill="x", padx=XS, pady=(XS, XS))
        elif self.chips.winfo_manager():
            self.chips.pack_forget()
        if g.saved_filters is not None:
            if not self.saved_btn.winfo_manager():
                self.saved_btn.pack(side="left", padx=(S, 0), before=self.row_btn)
        elif self.saved_btn.winfo_manager():
            self.saved_btn.pack_forget()
        self.back.state(["!disabled"] if g.can_undo_filter() else ["disabled"])
        self.fwd.state(["!disabled"] if g.can_redo_filter() else ["disabled"])
        self.row_btn.configure(text="Filter row ✓" if g.inline_filters() else "Filter row")
        self.set_count(g.filter_count_text())

    def set_count(self, text):
        if self.count_label.cget("text") != text:
            self.count_label.configure(text=text)

    def chip_widgets(self):
        return self.chips.chips()
