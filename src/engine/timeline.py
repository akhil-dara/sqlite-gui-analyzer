"""Timeline: find the timestamp columns of a database and list the events they date.

detect() samples every table (its first and last rows) and decides for each column whether it
holds dates, in which encoding, how sure that is and why:

  * by name: time, date, timestamp, _ts, created, modified, last_visit, expires, sent,
    received ... (a name ending in id, count, size, number, phone ... never counts), and by a
    declared type naming DATE or TIME;
  * by values: the sampled values that are not NULL (0, -1 and empty text count as "not set")
    must read as dates between LO_YEAR and HI_YEAR in one kind - Unix s/ms/us/ns, Cocoa s/ns,
    WebKit us, FILETIME, HFS+, .NET ticks, OLE days or date text (ISO 8601, RFC 2822) - for
    most of them: 60 % with a date name, 70 % with a weak one (start, end, last ...), 80 %
    without; without a date name at least 3 values and half of them distinct (flags and
    codes repeat). OLE days count only for REAL values; OLE and HFS+ only for columns whose
    name or type says date; GPS seconds (Unix seconds shifted by ten years) are never guessed.
    A Cocoa value within a year of 2001-01-01 is no evidence (a small counter reads as such a
    date). When several kinds fit equally, the one with fewer dates more than a year in the
    future, then the one whose dates lie nearer to now, wins; .NET ticks and FILETIME win over
    Cocoa nanoseconds.

build_events() then reads (row locator, the date column, a few describing columns) of every
chosen column: in SQL for tables SQLite serves - only those columns, only the date range asked
for (the dates are turned into the column's raw numbers, so SQLite filters), newest first up to
a cap per column - and natively for the others. wal_events() and carved_events() date the WAL
row versions and the recovered records of the Forensics carver the same way.

No Tk here: timeline_tab.py drives it.
"""

import email.utils
import heapq
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import limits, uiyield
from .backends import CONN_OP_LOCK
from .csvcells import csv_writer
from .decode import timestamps as tsm
from .fileformat.record import InvalidText
from .schema import Locator, quote_ident

LO_YEAR, HI_YEAR = tsm.LO_YEAR, tsm.HI_YEAR     # the one plausible window of the tool
# defaults of the limits timeline_sample_rows, timeline_column_events, timeline_total_events
SAMPLE_ROWS = 300               # rows sampled from each end of a table
COLUMN_CAP = 50000              # events read per column at most (the newest)
TOTAL_CAP = 1000000             # events kept at most
DESC_COLUMNS = 3                # describing columns per table
DESC_CHARS = 80                 # characters kept of a describing value
ISO = "iso_text"
OFF = "off"
AUTO = "auto"
FILE_FORMAT = "sqlite-gui-analyzer-timeline"

NUMERIC_KINDS = tuple(k[0] for k in tsm.KINDS)
KINDS = NUMERIC_KINDS + (ISO,)
LABELS = dict(tsm.LABELS)
LABELS[ISO] = "Date text (ISO 8601, RFC 2822)"
SHORT = dict(tsm.SHORT)
SHORT[ISO] = "Date text"
# order in which equally good kinds are preferred (after the checks described above)
PRIORITY = ("unix_s", "unix_ms", "unix_us", "unix_ns", "webkit_us", "filetime", "cocoa_s",
            "cocoa_ns", "dotnet_ticks", "hfs_s", "ole_days", "gps_s")
_NAMED_ONLY = frozenset(("ole_days", "hfs_s"))
# GPS seconds differ from Unix seconds by ten years only: never guessed, only chosen by the user
AUTO_KINDS = frozenset(k for k in PRIORITY if k != "gps_s")
# .NET ticks and FILETIME of any year read as Cocoa nanoseconds of a few months (2020-21, 2005):
# when both fit, the wider format wins
_PREFER = (("dotnet_ticks", "cocoa_ns"), ("filetime", "cocoa_ns"))
_EPOCH = dict((k[0], k[2]) for k in tsm.KINDS)
_UNIT = dict((k[0], k[3]) for k in tsm.KINDS)
_EPOCH_GAP = timedelta(days=366)

_POS = frozenset(("time", "date", "timestamp", "ts", "datetime", "created", "creation",
                  "modified", "modification", "updated", "expires", "expiry",
                  "expire", "expiration", "sent", "received", "receipt", "seen", "birth",
                  "birthday", "dob", "epoch", "accessed", "deleted", "utc", "mtime", "ctime",
                  "atime", "dt", "when"))
_WEAK = frozenset(("start", "end", "begin", "finish", "last", "first", "since", "until", "at",
                   "on"))
_NEG = frozenset(("id", "ids", "count", "cnt", "size", "len", "length", "number", "num", "no",
                  "phone", "msisdn", "jid", "duration", "dur", "port", "version", "ver", "flags",
                  "flag", "type", "status", "state", "hash", "key", "pid", "uid", "gid", "crc",
                  "offset", "total", "amount", "price", "width", "height", "bytes", "seq",
                  "sequence", "index", "idx", "rank", "position", "pos", "row", "rowid",
                  "counter", "revision", "rev", "code", "lat", "latitude", "lon", "lng",
                  "longitude", "level", "score", "percent", "ratio", "mask", "mode", "kind",
                  "color", "colour", "category", "priority", "retry", "retries", "attempts",
                  "limit", "max", "min", "sum", "avg", "rate", "fps", "zip", "pin", "name"))
_NOT_TIME = ("timeout", "interval", "duration", "elapsed", "ttl", "delay", "period", "timezone",
             "time_zone", "offset")
_DESC_NAMES = re.compile(r"text|body|data|title|name|url|subject|message|msg|caption|content|"
                         r"note|desc|address|addr|display|jid|remote|sender|from|to|number|path|"
                         r"file|host|domain|label|summary|query|term|value", re.I)
_ISO_RE = re.compile(r"^\s*(\d{4})[-/](\d{2})[-/](\d{2})"
                     r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?"
                     r"\s*(Z|UTC|GMT|[+-]\d{2}(?::?\d{2})?)?\s*$", re.I)


class Cancelled(Exception):
    """The caller's cancel() turned true (or its statement was interrupted)."""


# -- names ---------------------------------------------------------------------------------------
def name_tokens(name):
    """Lower-case words of a column name: snake_case, camelCase and Core Data's Z prefix."""
    text = str(name)
    if len(text) > 1 and text[0] == "Z" and text[1:].isupper():
        text = text[1:]                 # Core Data: ZDATE, ZCREATIONDATE
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def name_hint(name, decl_type=""):
    """'strong' when a name or declared type says date/time, 'weak' for start/end/last...,
    'never' for names that say what else the column holds (ending in id, count, size, number,
    phone ..., or naming a duration, an interval, a time zone, a flag is_/has_), None
    otherwise."""
    low = str(name).lower()
    toks = name_tokens(name)
    if not toks:
        return None
    if any(w in low for w in _NOT_TIME) or toks[-1] in _NEG or toks[0] in ("is", "has"):
        return "never"
    decl = (decl_type or "").upper()
    if "DATE" in decl or "TIME" in decl:
        return "strong"
    if any(t in _POS for t in toks) or re.search(r"date|time|stamp", low):
        return "strong"
    if any(t in _WEAK for t in toks):
        return "weak"
    return None


# -- values ----------------------------------------------------------------------------------------
def parse_iso(text):
    """Naive UTC datetime of an ISO 8601 date / date-time text (an offset is applied; none means
    UTC), or None."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(text, str) or len(text) > 40:
        return None
    m = _ISO_RE.match(text)
    if m is None:
        return None
    y, mo, d, hh, mm, ss, frac, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0),
                      int((frac or "0")[:6].ljust(6, "0")))
    except ValueError:
        return None
    if tz and tz.upper() not in ("Z", "UTC", "GMT"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        minutes = int(digits[:2]) * 60 + int(digits[2:4] or 0)
        try:
            dt -= sign * timedelta(minutes=minutes)
        except OverflowError:
            return None
    return dt


_RFC_RE = re.compile(r"^\s*(?:[A-Za-z]{3},?\s+)?\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{1,2}:\d{2}")


def parse_rfc(text):
    """Naive UTC datetime of an RFC 2822 date text ('Tue, 02 Mar 2021 10:00:00 GMT', as HTTP
    headers write it), or None."""
    if not isinstance(text, str) or len(text) > 64 or not _RFC_RE.match(text):
        return None
    try:
        parts = email.utils.parsedate_tz(text)
        if parts is None:
            return None
        dt = datetime(*parts[:6])
        return dt - timedelta(seconds=parts[9] or 0)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None


def parse_date_text(text):
    """Naive UTC datetime of ISO 8601 or RFC 2822 date text, or None."""
    return parse_iso(text) or parse_rfc(text)


def to_utc(value, kind):
    """Naive UTC datetime of a raw value read as `kind`, or None. BLOBs are never dates."""
    if value is None or isinstance(value, bool):
        return None
    if kind == ISO:
        return parse_date_text(value) if isinstance(value, str) else None
    if isinstance(value, (bytes, bytearray, InvalidText)):
        return None
    return tsm.to_datetime(value, kind)


def from_utc(dt, kind):
    """The raw number `kind` stores for a naive UTC datetime (the inverse of to_utc); for ISO
    the text 'YYYY-MM-DD HH:MM:SS'."""
    if kind == ISO:
        return "%04d-%02d-%02d %02d:%02d:%02d" % (dt.year, dt.month, dt.day, dt.hour, dt.minute,
                                                  dt.second)
    num, den = _UNIT[kind]
    micros = (dt - _EPOCH[kind]) // timedelta(microseconds=1)
    if kind == "ole_days":
        return micros / float(num)
    if den > 1:
        return micros * den // num
    return micros // num if micros % num == 0 else micros / float(num)


def utc_now():
    """Now as a naive UTC datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


fmt_time = tsm.fmt_time          # 'YYYY-MM-DD HH:MM:SS[.mmm|.uuuuuu]' (the column says UTC)


def _subsecond(dt):
    if not dt.microsecond:
        return ""
    return (".%03d" % (dt.microsecond // 1000)) if dt.microsecond % 1000 == 0 \
        else (".%06d" % dt.microsecond)


# timestamp display presets: (key, menu label, strftime, strftime after sub-seconds)
TIME_FORMATS = (
    ("iso", "ISO 8601 (24-hour)", "%Y-%m-%d %H:%M:%S", ""),
    ("12h", "12-hour", "%Y-%m-%d %I:%M:%S", " %p"),
    ("excel", "Excel style", "%d-%b-%Y %H:%M:%S", ""),
    ("us", "US style", "%m/%d/%Y %I:%M:%S", " %p"),
    ("dmy12", "Day-month-year, 12-hour", "%d-%b-%Y %I:%M:%S", " %p"),
    ("custom", "Custom\u2026", None, None),
)
TIME_FORMAT_KEYS = tuple(k for k, _l, _f, _a in TIME_FORMATS)


def _fraction_digits(pattern, dt):
    """%1f ... %9f in a custom format: the fraction of the second with that many digits
    (%7f: .1234560, as .NET ticks show it); a plain %f keeps its 6."""
    digits = "%06d" % dt.microsecond + "000"
    return re.sub(r"%([1-9])f", lambda m: digits[:int(m.group(1))], pattern)


def format_dt(dt, fmt="iso", custom=""):
    """A naive UTC datetime as text: one of the TIME_FORMATS presets ('iso',
    '12h', 'excel' like 02-Oct-2026 14:30:45.123, 'us'), or 'custom' for a strftime
    string (a broken one falls back to ISO). Sub-seconds are always shown when present."""
    for k, _l, before, after in TIME_FORMATS:
        if k == fmt and before is not None:
            return dt.strftime(before) + _subsecond(dt) + dt.strftime(after)
    if fmt == "custom" and custom:
        try:
            text = dt.strftime(_fraction_digits(custom, dt))
            # Windows' strftime passes a lone surrogate through instead of failing: text
            # that is not valid Unicode cannot be shown or saved, so it counts as broken
            text.encode("utf-8")
        except Exception:                       # noqa: BLE001 - a bad format shows ISO
            text = ""
        if text:
            # sub-seconds join the seconds only when the pattern ends with them (never after
            # an AM/PM or a 'Z' the pattern puts last)
            has_fraction = "%f" in custom or re.search(r"%[1-9]f", custom)
            ends_with_seconds = custom.rstrip().endswith("%S")
            return text + (_subsecond(dt) if ends_with_seconds and not has_fraction else "")
    return fmt_time(dt)


def formatter(kind, fmt="iso", custom=""):
    """fn(value) -> the value as a date text in the display format, or None when it is no
    date of that kind. Sentinel values (-1, 0, ...) are not dates: they read '0 (not set)',
    never 1970-01-01 or 1601-01-01."""
    def f(value):
        if kind != ISO and isinstance(value, (int, float)) and not isinstance(value, bool) \
                and value <= 0:
            return "%s (not set)" % (int(value) if float(value).is_integer() else value)
        dt = to_utc(value, kind)
        if dt is None:
            return None
        return format_dt(dt, fmt, custom)
    return f


# Where an offset is used (standard time; a place on summer time shows the next offset):
# names for the zone choices, so 'UTC+05:30' reads 'UTC+05:30 · India, Sri Lanka'. Plain data,
# no time-zone database (Windows has none in the standard library).
ZONE_NAMES = {
    -720: "Baker Island", -660: "American Samoa", -600: "Hawaii", -570: "Marquesas",
    -540: "Alaska", -480: "US Pacific", -420: "US Mountain", -360: "US Central, Mexico City",
    -300: "US Eastern, Colombia", -240: "Atlantic, Venezuela", -210: "Newfoundland",
    -180: "Brazil, Argentina", -120: "South Georgia", -60: "Azores, Cape Verde",
    0: "UTC, London (winter), Lisbon", 60: "Central Europe, West Africa",
    120: "Eastern Europe, Egypt, South Africa", 180: "Moscow, Turkey, Saudi Arabia",
    210: "Iran", 240: "UAE, Oman", 270: "Afghanistan", 300: "Pakistan, Uzbekistan",
    330: "India, Sri Lanka", 345: "Nepal", 360: "Bangladesh", 390: "Myanmar",
    420: "Thailand, Vietnam, Indonesia (west)", 480: "China, Singapore, Philippines",
    540: "Japan, Korea", 570: "Australia (central)", 600: "Australia (east)",
    630: "Lord Howe", 660: "Solomon Islands", 720: "New Zealand, Fiji", 780: "Tonga",
    840: "Line Islands",
}


def local_offset():
    """This computer's offset from UTC now, in minutes east."""
    import time as _t
    return -(_t.altzone if _t.daylight and _t.localtime().tm_isdst else _t.timezone) // 60


def zone_label(minutes, local=None):
    """'UTC+05:30 · India, Sri Lanka', with '(local time)' when it is this computer's
    offset (local: that offset, default local_offset()). parse_offset() reads it back."""
    text = offset_label(minutes)
    name = ZONE_NAMES.get(minutes)
    if name and minutes:
        text += " · " + name
    if (local_offset() if local is None else local) == minutes:
        text += " (local time)"
    return text


def parse_offset(text):
    """Minutes east of UTC from 'UTC', 'UTC+05:30', '+2', '-0800' ...; None when unreadable.
    A zone label's description ('UTC+05:30 · India (this computer)') is ignored."""
    text = (text or "").split("·")[0].split("(")[0]
    s = (text or "").strip().upper().replace("GMT", "UTC")
    if s in ("", "UTC", "Z"):
        return 0
    m = re.match(r"^(?:UTC)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", s)
    if m is None:
        return None
    minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
    if minutes > 14 * 60:
        return None
    return -minutes if m.group(1) == "-" else minutes


def offset_label(minutes):
    if not minutes:
        return "UTC"
    sign = "-" if minutes < 0 else "+"
    return "UTC%s%02d:%02d" % (sign, abs(minutes) // 60, abs(minutes) % 60)


def parse_when(text, end=False):
    """Naive datetime from 'YYYY-MM-DD[ HH:MM[:SS]]'; with end=True a date alone means the end
    of that day. None for empty text; ValueError for unreadable text."""
    s = (text or "").strip()
    if not s:
        return None
    for fmt, step in (("%Y-%m-%d %H:%M:%S", timedelta(seconds=1)),
                      ("%Y-%m-%d %H:%M", timedelta(minutes=1)),
                      ("%Y-%m-%dT%H:%M:%S", timedelta(seconds=1)),
                      ("%Y-%m-%d", timedelta(days=1))):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt + step - timedelta(microseconds=1) if end else dt
    raise ValueError("not a date: %r (use YYYY-MM-DD or YYYY-MM-DD HH:MM)" % s)


# -- the detector ------------------------------------------------------------------------------------
def _bounds(kind, lo, hi):
    """(low, high, epoch gap) raw limits of kind for [lo, hi) as numbers."""
    gap = 0
    if lo <= _EPOCH[kind] <= hi:
        gap = abs(from_utc(_EPOCH[kind] + _EPOCH_GAP, kind))
    return from_utc(lo, kind), from_utc(hi, kind), gap


_BOUNDS_CACHE = {}


def _kind_bounds(lo_year, hi_year):
    key = (lo_year, hi_year)
    hit = _BOUNDS_CACHE.get(key)
    if hit is None:
        lo, hi = datetime(lo_year, 1, 1), datetime(hi_year + 1, 1, 1)
        hit = _BOUNDS_CACHE[key] = dict((k, _bounds(k, lo, hi)) for k in NUMERIC_KINDS)
    return hit


def _as_number(v):
    """int/float of a numeric value or numeric text, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if not isinstance(v, float) or math.isfinite(v) else None
    if isinstance(v, str) and 0 < len(v) <= 32:
        try:
            return int(v)
        except ValueError:
            try:
                f = float(v)
            except ValueError:
                return None
            return f if math.isfinite(f) else None
    return None


class Guess(object):
    """What the detector concluded about one column's values."""
    __slots__ = ("kind", "score", "matched", "considered", "unset", "first", "last", "hint",
                 "confidence", "reason", "alternatives", "text_numbers", "loose_text")

    def __init__(self):
        self.kind, self.score, self.matched, self.considered, self.unset = None, 0.0, 0, 0, 0
        self.first = self.last = None
        self.hint, self.confidence, self.reason = None, "", ""
        self.alternatives, self.text_numbers, self.loose_text = [], False, False


def judge(name, values, decl_type="", now=None, lo_year=LO_YEAR, hi_year=HI_YEAR):
    """Guess for a column named `name` holding the sampled `values` (see the module docstring
    for the rules). guess.kind is None when the column is not taken for a date column; the
    reason then says why."""
    g = Guess()
    g.hint = hint = name_hint(name, decl_type)
    if hint == "never":
        g.reason = "name '%s' says it holds something else (an id, count, size, number ...)" \
            % name
        return g
    now = now or utc_now()
    bounds = _kind_bounds(lo_year, hi_year)
    numbers, texts, isos = [], 0, []
    text_numbers = loose = 0
    for v in values:
        if v is None:
            continue
        if isinstance(v, (bytes, bytearray)):
            texts += 1                  # a BLOB is not a date
            continue
        if isinstance(v, str):
            if not v.strip():
                g.unset += 1            # empty text: not set, like 0
                continue
            dt = parse_iso(v)
            if dt is None:
                dt = parse_rfc(v)
                loose += dt is not None
            if dt is not None:
                isos.append(dt)
                continue
            n = _as_number(v) if hint else None
            if n is None:
                texts += 1
                continue
            text_numbers += 1
            v = n
        n = _as_number(v)
        if n is None:
            texts += 1
        elif n in (0, -1):
            g.unset += 1
        else:
            numbers.append(n)
    g.considered = considered = len(numbers) + len(isos) + texts
    if not considered:
        g.reason = "no values to judge (%s)" % ("all NULL or unset" if g.unset or not values
                                                else "empty")
        return g
    candidates = []
    for rank, kind in enumerate(PRIORITY):
        if kind not in AUTO_KINDS:
            continue
        if kind == "ole_days" and not any(isinstance(n, float) for n in numbers):
            continue
        if kind in _NAMED_ONLY and hint != "strong":
            continue
        lo, hi, gap = bounds[kind]
        hits = [n for n in numbers if lo <= n < hi and abs(n) >= gap
                and (kind != "ole_days" or isinstance(n, float))]
        if hits:
            candidates.append((kind, rank, hits))
    if isos:
        lo, hi = datetime(lo_year, 1, 1), datetime(hi_year + 1, 1, 1)
        hits = [dt for dt in isos if lo <= dt < hi]
        if hits:
            candidates.append((ISO, len(PRIORITY), hits))
    scored = []
    for kind, rank, hits in candidates:
        if kind == ISO:
            dts = sorted(hits)
        else:
            ordered = sorted(hits)
            dts = [to_utc(ordered[0], kind), to_utc(ordered[len(ordered) // 2], kind),
                   to_utc(ordered[-1], kind)]
            dts = [d for d in dts if d is not None]
            if len(dts) != 3:
                continue
        median = dts[len(dts) // 2]
        if kind == ISO:
            past = sum(1 for d in hits if d <= now + timedelta(days=366))
        else:
            fut = from_utc(now + timedelta(days=366), kind)
            past = sum(1 for n in hits if n <= fut)
        scored.append(((len(hits), past, -abs((median - now).total_seconds()), -rank),
                       kind, hits, dts))
    if not scored:
        g.reason = "%s; none of %d sampled values reads as a date %d-%d" % (
            _hint_text(name, hint), considered, lo_year, hi_year)
        return g
    scored.sort(reverse=True)
    top = scored[0]
    for better, worse in _PREFER:
        if top[1] == worse:
            alt = [s for s in scored if s[1] == better and s[0][0] >= top[0][0]]
            if alt:
                top = alt[0]
                scored.remove(top)
                scored.insert(0, top)
    (count, _past, _dist, _rank), kind, hits, dts = top
    g.matched = count
    g.score = count / float(considered)
    g.first, g.last = dts[0], dts[-1]
    g.alternatives = [k for (c, _p, _d, _r), k, _h, _x in scored[1:] if c >= count * 0.9]
    g.text_numbers = kind != ISO and text_numbers > 0
    g.loose_text = kind == ISO and loose > 0
    need, least = {"strong": (0.6, 1), "weak": (0.7, 2)}.get(hint, (0.8, 3))
    distinct = len(set(hits)) if kind != ISO else len(set(dts))
    what = "%d of %d sampled values read as %s (%s .. %s)" % (
        count, considered, LABELS[kind], fmt_time(g.first)[:10], fmt_time(g.last)[:10])
    if g.score < need or count < least:
        g.reason = "%s; only %s" % (_hint_text(name, hint), what)
        return g
    if hint is None and distinct < max(3, count * 0.5):
        g.reason = "%s; %s, but only %d distinct: values that repeat like this are flags or " \
                   "codes, not dates" % (_hint_text(name, hint), what, distinct)
        return g
    if hint is None and kind != ISO and distinct >= 5:
        ordered = sorted(set(hits))
        steps = set(b - a for a, b in zip(ordered, ordered[1:]))
        if len(steps) <= 2:
            g.reason = "%s; %s, but they step evenly like a counter or an id" % (
                _hint_text(name, hint), what)
            return g
    g.kind = kind
    if hint == "strong" and g.score >= 0.9 and count >= 5:
        g.confidence = "high"
    elif hint is not None or (g.score >= 0.95 and count >= 20 and distinct >= 10):
        g.confidence = "medium"
    else:
        g.confidence = "low"
    g.reason = "%s; %s" % (_hint_text(name, hint), what)
    if g.alternatives:
        g.reason += "; also fits %s" % ", ".join(SHORT[k] for k in g.alternatives)
    if g.unset:
        g.reason += "; %d not set (0 or empty)" % g.unset
    return g


def _hint_text(name, hint):
    if hint == "strong":
        return "name/type '%s' says date" % name
    if hint == "weak":
        return "name '%s' may be a date" % name
    return "values only"


def suggest_kind(name, values, decl_type=""):
    """The kind the detector picks for these values (None when no kind fits)."""
    return judge(name, values, decl_type).kind


# -- detection over a database -------------------------------------------------------------------------
class TimeColumn(object):
    """A column detected as holding dates (or named like one): kind and confidence found by the
    detector, the reason, and the user's override (a kind, or OFF to leave it out)."""
    __slots__ = ("table", "column", "kind", "confidence", "score", "reason", "hint", "first",
                 "last", "override", "text_numbers", "loose_text", "sampled", "database")

    def __init__(self, table, column, guess, sampled=0):
        self.table, self.column = table, column
        self.database = None            # the database it belongs to, in a case (the UI's)
        self.kind, self.confidence, self.score = guess.kind, guess.confidence, guess.score
        self.reason, self.hint = guess.reason, guess.hint
        self.first, self.last = guess.first, guess.last
        self.text_numbers, self.loose_text = guess.text_numbers, guess.loose_text
        self.override = None
        self.sampled = sampled

    @property
    def key(self):
        return (self.table, self.column)

    @property
    def effective_kind(self):
        if self.override == OFF:
            return None
        if self.override in KINDS:
            return self.override
        return self.kind

    @property
    def enabled(self):
        return self.effective_kind is not None

    def __repr__(self):
        return "TimeColumn(%s.%s, %s, %s)" % (self.table, self.column, self.effective_kind,
                                              self.confidence)


class Detection(object):
    """detect()'s result: columns (TimeColumn, detected ones first), descriptions
    {table: [describing columns]}, notes, the time it took and whether it was cancelled."""

    def __init__(self):
        self.columns, self.descriptions, self.notes = [], {}, []
        self.seconds, self.tables, self.cancelled = 0.0, 0, False

    def detected(self):
        return [c for c in self.columns if c.kind is not None]

    def enabled(self):
        return [c for c in self.columns if c.enabled]

    def get(self, table, column):
        for c in self.columns:
            if c.table == table and c.column == column:
                return c
        return None

    def apply_overrides(self, overrides):
        """overrides: {table: {column: kind or OFF}} (the saved user choices)."""
        for c in self.columns:
            o = (overrides or {}).get(c.table)
            v = o.get(c.column) if isinstance(o, dict) else None
            c.override = v if v in KINDS or v == OFF else None


def _is_interrupt(err):
    return isinstance(err, sqlite3.OperationalError) and str(err).lower() == "interrupted"


def _close_cursor(cur):
    with CONN_OP_LOCK:
        try:
            cur.close()
        except sqlite3.Error:
            pass


def _sample_expr(col):
    q = quote_ident(col)
    return "CASE typeof(%s) WHEN 'blob' THEN x'' WHEN 'text' THEN substr(%s, 1, 64) ELSE %s END" \
        % (q, q, q)


def _clip(v, n=64):
    if isinstance(v, (bytes, bytearray)):
        return b""
    if isinstance(v, str) and len(v) > n:
        return v[:n]
    return v


def sample_table(session, name, rows=None):
    """(columns, rows) of the first and last `rows` rows of a table: BLOBs come as b'' and text
    is cut to 64 characters (only what the detector needs is read)."""
    if rows is None:
        rows = limits.get("timeline_sample_rows")
    t = session.info(name)
    cols = session.visible_columns(name)
    if session.source(name) == "sql" and cols:
        conn = session.conn()
        base = "SELECT %s FROM %s" % (", ".join(_sample_expr(c) for c in cols), quote_ident(name))
        out = []
        try:
            for sql in (base + " LIMIT ?", None):
                if sql is None:
                    if len(out) < rows:
                        break
                    rev = session._reverse_natural_order(t)
                    if not rev:
                        break
                    sql = base + " ORDER BY " + rev + " LIMIT ?"
                cur = conn.execute(sql, (rows,))
                try:
                    out.extend(list(r) for r in cur.fetchall())
                finally:
                    _close_cursor(cur)
            return cols, out
        except sqlite3.Error as e:
            if _is_interrupt(e) or not t.natively_readable:
                raise
    out = []
    page = session.browse(name, 0, rows)
    cols = list(page.columns) or cols
    out.extend([_clip(v) for v in r.values] for r in page.rows)
    if len(page.rows) >= rows:
        page = session.browse(name, 0, rows, desc=True)
        out.extend([_clip(v) for v in r.values] for r in page.rows)
    return cols, out


def sample_column(session, table, column, rows=None):
    """Sampled values of one column (for the Browse date format's Auto)."""
    if rows is None:
        rows = limits.get("timeline_sample_rows")
    cols, data = sample_table(session, table, rows)
    if column not in cols:
        return []
    i = cols.index(column)
    return [r[i] for r in data if i < len(r)]


def _pick_descriptions(cols, data, skip):
    """Up to DESC_COLUMNS describing columns: text columns first (message, name, url ...),
    then the other non-date columns."""
    scored = []
    for i, c in enumerate(cols):
        if c in skip:
            continue
        vals = [r[i] for r in data if i < len(r) and r[i] is not None]
        if not vals:
            continue
        texts = [v for v in vals if isinstance(v, str)]
        blobs = sum(1 for v in vals if isinstance(v, (bytes, bytearray)))
        if blobs * 2 > len(vals):
            continue
        is_text = len(texts) * 2 >= len(vals)
        named = bool(_DESC_NAMES.search(c))
        avg = sum(len(t) for t in texts) / float(len(texts)) if texts else 0
        scored.append((0 if is_text else 1, 0 if named else 1, -min(avg, 40), i, c))
    scored.sort()
    return [s[-1] for s in scored[:DESC_COLUMNS]]


def timeline_tables(session):
    """Tables a timeline covers: ordinary tables (no views, no virtual tables, no sqlite_*)."""
    return [n for n in session.schema.names("table") if not n.lower().startswith("sqlite_")]


def detect(session, tables=None, cancel=None, progress=None, overrides=None, now=None):
    """Detection over the database's tables (all, or `tables`). cancel() true stops (the result
    says cancelled); progress(done, total) is called per table."""
    t0 = time.time()
    res = Detection()
    names = [n for n in (tables if tables is not None else timeline_tables(session))]
    listed = []
    for i, name in enumerate(names):
        if cancel is not None and cancel():
            res.cancelled = True
            break
        if progress is not None:
            progress(i, len(names))
        try:
            t = session.info(name)
            if session.source(name) == "unavailable":
                res.notes.append("%s: cannot be read without SQLite" % name)
                continue
            cols, data = sample_table(session, name)
        except sqlite3.Error as e:
            if _is_interrupt(e):
                res.cancelled = True
                break
            res.notes.append("%s: %s" % (name, e))
            continue
        except Exception as e:          # noqa: BLE001 - one table must not stop the rest
            res.notes.append("%s: %s" % (name, e))
            continue
        res.tables += 1
        decl = dict((c.name, c.decl_type) for c in t.columns)
        alias = t.columns[t.rowid_alias].name if t.rowid_alias is not None else None
        found = set()
        for ci, c in enumerate(cols):
            if c == alias or "BLOB" in (decl.get(c) or "").upper():
                continue
            vals = [r[ci] for r in data if ci < len(r)]
            g = judge(c, vals, decl.get(c, ""), now=now)
            if g.kind is not None or g.hint == "strong":
                listed.append(TimeColumn(name, c, g, len(vals)))
                if g.kind is not None:
                    found.add(c)
        if found:
            res.descriptions[name] = _pick_descriptions(cols, data, found | set([alias]))
    order = {"high": 0, "medium": 1, "low": 2}
    listed.sort(key=lambda c: (c.kind is None, c.table.lower(), order.get(c.confidence, 3),
                               c.column.lower()))
    res.columns = listed
    res.apply_overrides(overrides)
    res.seconds = time.time() - t0
    if progress is not None:
        progress(len(names), len(names))
    return res


# -- events ---------------------------------------------------------------------------------------------
class Event(object):
    """One dated row: when (naive UTC), table, column, kind, locator (engine Locator or None),
    row (display text), description, source ('DB', 'WAL (superseded)', 'Recovered (...)'),
    raw (the stored value) and ref (the WAL record dict or recovered Record behind it)."""
    __slots__ = ("when", "table", "column", "kind", "locator", "row", "description", "source",
                 "raw", "ref", "database")

    def __init__(self, when, table, column, kind, locator, row, description, source, raw,
                 ref=None, database=""):
        self.when, self.table, self.column, self.kind = when, table, column, kind
        self.locator, self.row, self.description = locator, row, description
        self.source, self.raw, self.ref = source, raw, ref
        self.database = database        # the database's name when several are merged

    def as_dict(self, offset_minutes=0):
        d = {"time_utc": fmt_time(self.when), "table": self.table, "column": self.column,
             "kind": self.kind, "row": self.row, "source": self.source,
             "description": self.description, "raw": _json_raw(self.raw)}
        if self.database:
            d["database"] = self.database
        if offset_minutes:
            d["time_local"] = fmt_time(self.when + timedelta(minutes=offset_minutes))
        return d

    def __repr__(self):
        return "Event(%s, %s.%s, %s, %s)" % (fmt_time(self.when), self.table, self.column,
                                             self.row, self.source)


class EventSet(object):
    """build_events()'s result: events sorted by time, notes, events per (table, column), the
    columns whose cap was reached, the time it took and whether it was cancelled."""

    def __init__(self):
        self.events, self.notes, self.counts, self.capped = [], [], {}, []
        self.seconds, self.cancelled = 0.0, False


def _short(v):
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "[BLOB]"
    s = v if isinstance(v, str) else str(v)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return s if len(s) <= DESC_CHARS else s[:DESC_CHARS] + "…"


def describe(columns, values):
    return ", ".join("%s=%s" % (c, _short(v)) for c, v in zip(columns, values) if v is not None)


def _range_sql(expr, kind, start, end, lo_year=LO_YEAR, hi_year=HI_YEAR):
    """(where, params) keeping raw values of `kind` between start and end (default: the whole
    plausible window)."""
    lo = start if start is not None else datetime(lo_year, 1, 1)
    hi = end if end is not None else datetime(hi_year + 1, 1, 1)
    if kind == ISO:
        # text compares by date: widen by a day, the exact bounds are applied after parsing
        return "%s >= ? AND %s < ?" % (expr, expr), [from_utc(lo - timedelta(days=1), ISO)[:10],
                                                     from_utc(hi + timedelta(days=2), ISO)[:10]]
    rlo, rhi = from_utc(lo, kind), from_utc(hi, kind)
    return "%s BETWEEN ? AND ?" % expr, [rlo, rhi]


def _in_range(dt, start, end, lo_year=LO_YEAR, hi_year=HI_YEAR):
    if dt is None:
        return False
    if start is not None and dt < start:
        return False
    if end is not None and dt > end:
        return False
    return lo_year <= dt.year <= hi_year


def _sql_column_events(session, tc, kind, desc, start, end, cap, cancel):
    t = session.info(tc.table)
    if t.without_rowid:
        lead = [quote_ident(t.columns[i].name) for i in t.pk_columns]
    else:
        lead = [t.rowid_name]           # a literal alias from schema.ROWID_ALIASES
    col = quote_ident(tc.column)
    expr = "CAST(%s AS REAL)" % col if tc.text_numbers and kind != ISO else col
    sel = lead + [col] + ["CASE typeof(%s) WHEN 'blob' THEN NULL WHEN 'text' THEN substr(%s, 1, %d)"
                          " ELSE %s END" % (quote_ident(d), quote_ident(d), DESC_CHARS + 1,
                                            quote_ident(d)) for d in desc]
    table = quote_ident(tc.table)
    if tc.loose_text:
        # dates written like 'Tue, 02 Mar 2021 ...' do not sort as text: every non-NULL value
        # is read and the newest are kept here
        sql, params = "SELECT %s FROM %s WHERE %s IS NOT NULL" % (", ".join(sel), table, col), []
    else:
        where, params = _range_sql(expr, kind, start, end)
        if t.without_rowid:
            sql = "SELECT %s FROM %s WHERE %s ORDER BY %s DESC LIMIT ?" % (
                ", ".join(sel), table, where, expr)
        else:
            # the newest keys are found reading only the key and the date; the describing
            # columns are then read for those rows alone
            sql = "SELECT %s FROM %s WHERE %s IN (SELECT %s FROM %s WHERE %s ORDER BY %s DESC " \
                  "LIMIT ?)" % (", ".join(sel), table, lead[0], lead[0], table, where, expr)
        params = params + [cap + 1]
    n = len(lead)
    heap, k = [], 0
    cur = session.conn().execute(sql, params)
    try:
        while True:
            if cancel is not None and cancel():
                raise Cancelled()
            uiyield.pause()
            chunk = cur.fetchmany(2000)
            if not chunk:
                break
            for r in chunk:
                raw = r[n]
                dt = to_utc(raw, kind) if not tc.text_numbers else to_utc(_as_number(raw), kind)
                if not _in_range(dt, start, end):
                    continue
                k += 1
                item = (dt, k, r)
                if len(heap) <= cap:
                    heapq.heappush(heap, item)
                else:
                    heapq.heappushpop(heap, item)
    finally:
        _close_cursor(cur)
    capped = k > cap
    if len(heap) > cap:
        heapq.heappop(heap)             # the oldest of cap + 1
    out = []
    for dt, _k, r in heap:
        loc = Locator("rowid", r[0]) if not t.without_rowid else Locator("pk", tuple(r[:n]))
        out.append(Event(dt, tc.table, tc.column, kind, loc, loc.display(),
                         describe(desc, r[n + 1:]), "DB", r[n]))
    return out, capped


def _native_column_events(session, tc, kind, desc, start, end, cap, cancel):
    t = session.info(tc.table)
    cols = t.column_names
    ci = cols.index(tc.column)
    di = [cols.index(d) for d in desc if d in cols]
    heap, n, capped = [], 0, False
    for k, row in enumerate(session.iter_rows(tc.table)):
        if cancel is not None and k % 2000 == 0 and cancel():
            raise Cancelled()
        vals = row.values
        raw = vals[ci] if ci < len(vals) else None
        dt = to_utc(raw if not tc.text_numbers else _as_number(raw), kind)
        if not _in_range(dt, start, end):
            continue
        n += 1
        item = (dt, n, row.locator, raw, [vals[i] if i < len(vals) else None for i in di])
        if len(heap) < cap:
            heapq.heappush(heap, item)
        else:
            capped = True
            heapq.heappushpop(heap, item)
    out = []
    for dt, _n, loc, raw, dvals in heap:
        out.append(Event(dt, tc.table, tc.column, kind, loc, loc.display() if loc else "",
                         describe([d for d in desc if d in cols], dvals), "DB", raw))
    return out, capped


def column_groups(columns, descriptions):
    """{table: [(column, kind, text_numbers)]} of the enabled columns, and their describing
    columns {table: [...]}."""
    wanted = {}
    for c in columns:
        k = c.effective_kind
        if k is not None:
            wanted.setdefault(c.table, []).append((c.column, k, c.text_numbers))
    return wanted, dict((t, list(descriptions.get(t) or ())) for t in wanted)


def build_events(session, columns, descriptions=None, start=None, end=None, column_cap=COLUMN_CAP,
                 total_cap=TOTAL_CAP, cancel=None, progress=None):
    """Events of the enabled TimeColumns between start and end (naive UTC datetimes, inclusive;
    None: no limit), newest `column_cap` per column, at most `total_cap` in all."""
    t0 = time.time()
    res = EventSet()
    descriptions = descriptions or {}
    todo = [c for c in columns if c.enabled]
    events = []
    for i, tc in enumerate(todo):
        if progress is not None:
            progress(i, len(todo))
        if cancel is not None and cancel():
            res.cancelled = True
            break
        kind = tc.effective_kind
        desc = [d for d in descriptions.get(tc.table) or () if d != tc.column]
        try:
            src = session.source(tc.table)
            if src == "unavailable":
                res.notes.append("%s.%s: the table cannot be read without SQLite"
                                 % (tc.table, tc.column))
                continue
            got = None
            if src == "sql":
                try:
                    got = _sql_column_events(session, tc, kind, desc, start, end, column_cap,
                                             cancel)
                except sqlite3.Error as e:
                    if _is_interrupt(e):
                        raise Cancelled()
                    if not session.info(tc.table).natively_readable:
                        raise
                    res.notes.append("%s.%s: SQLite failed (%s); read natively"
                                     % (tc.table, tc.column, e))
            if got is None:
                got = _native_column_events(session, tc, kind, desc, start, end, column_cap,
                                            cancel)
        except Cancelled:
            res.cancelled = True
            break
        except sqlite3.Error as e:
            if _is_interrupt(e):
                res.cancelled = True
                break
            res.notes.append("%s.%s: %s" % (tc.table, tc.column, e))
            continue
        except Exception as e:          # noqa: BLE001 - one column must not stop the rest
            res.notes.append("%s.%s: %s" % (tc.table, tc.column, e))
            continue
        evs, capped = got
        res.counts[tc.key] = len(evs)
        if capped:
            res.capped.append(tc.key)
            res.notes.append("%s.%s: more than %s events; the newest %s are shown (choose a "
                             "date range, or a larger Max events per column, to see others)"
                             % (tc.table, tc.column, format(column_cap, ","),
                                format(column_cap, ",")))
        events.extend(evs)
    if progress is not None:
        progress(len(todo), len(todo))
    res.events = finish(events, total_cap, res.notes)
    res.seconds = time.time() - t0
    return res


def finish(events, total_cap=TOTAL_CAP, notes=None):
    """Sort events by time (then table, column, source); keep the newest total_cap."""
    if len(events) > total_cap:
        events = heapq.nlargest(total_cap, events, key=lambda e: e.when)
        if notes is not None:
            notes.append("more than %s events in all: the newest %s are kept (limit "
                         "timeline_total_events)" % (format(total_cap, ","),
                                                     format(total_cap, ",")))
    events.sort(key=lambda e: (e.when, e.table, e.column, e.source))
    return events


def _value_key(v):
    if v is None:
        return (0, None)
    if isinstance(v, InvalidText):
        return (4, bytes(v))
    if isinstance(v, (bytes, bytearray)):
        return (5, bytes(v))
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, int):
        return (1, v)
    if isinstance(v, float):
        return (2, repr(v))
    return (3, v)


def wal_events(records, columns, descriptions=None, start=None, end=None, live_values=None,
               cancel=None, cap=COLUMN_CAP, live_checks=50000):
    """Events of the row versions held in WAL frames that the database does not show: records
    are WALParser.recover_all_records() dicts (table, locator, values_dict, raw_values,
    frame_idx, page_num, category). Versions in 'current' frames are what the database shows
    and are skipped, as are copies identical to them; live_values(table, locator) -> the row's
    current values (or None) also skips versions identical to the live row (at most
    live_checks look-ups). Returns (events, notes)."""
    wanted, desc = column_groups(columns, descriptions or {})
    current, versions = set(), {}
    order = []
    for rec in records:
        if cancel is not None and cancel():
            raise Cancelled()
        table = rec.get("table")
        if table not in wanted:
            continue
        try:
            key = (table, rec["locator"], tuple(_value_key(v) for v in rec["raw_values"]))
            hash(key)
        except TypeError:
            continue
        if rec.get("category") == "current":
            current.add(key)
            continue
        ent = versions.get(key)
        if ent is None:
            versions[key] = [rec, [(rec["frame_idx"], rec["page_num"], rec["category"])]]
            order.append(key)
        else:
            ent[1].append((rec["frame_idx"], rec["page_num"], rec["category"]))
    events, notes, checks, per = [], [], 0, {}
    for key in order:
        if key in current:
            continue
        rec, frames = versions[key]
        table, loc = key[0], key[1]
        if live_values is not None and checks < live_checks:
            checks += 1
            try:
                live = live_values(table, loc)
            except Exception:           # noqa: BLE001 - no live row to compare with
                live = None
            if live is not None and tuple(_value_key(v) for v in live) == key[2]:
                continue
        cols = list(rec["values_dict"].keys())
        vals = rec["raw_values"]
        dcols = [d for d in desc.get(table) or () if d in cols]
        dvals = [vals[cols.index(d)] if cols.index(d) < len(vals) else None for d in dcols]
        newest = max(frames)
        for col, kind, text_numbers in wanted[table]:
            if col not in cols:
                continue
            i = cols.index(col)
            raw = vals[i] if i < len(vals) else None
            dt = to_utc(_as_number(raw) if text_numbers else raw, kind)
            if not _in_range(dt, start, end):
                continue
            n = per.get((table, col), 0)
            if n >= cap:
                continue
            per[(table, col)] = n + 1
            ev = Event(dt, table, col, kind, loc, "%s (WAL frame %s)" % (
                loc.display() if loc is not None else "?", newest[0]),
                describe(dcols, dvals), "WAL (%s)" % newest[2], raw, rec)
            events.append(ev)
    for (table, col), n in sorted(per.items()):
        if n >= cap:
            notes.append("%s.%s: WAL versions capped at %s" % (table, col, format(cap, ",")))
    if checks >= live_checks:
        notes.append("WAL versions: only the first %s were compared with the live rows"
                     % format(live_checks, ","))
    return events, notes


def carved_events(records, columns, descriptions=None, start=None, end=None, cap=COLUMN_CAP):
    """Events of recovered records (engine.forensics Records of the Forensics carver) whose
    table has date columns. Returns (events, notes)."""
    wanted, desc = column_groups(columns, descriptions or {})
    events, per, notes = [], {}, []
    for rec in records or ():
        table = getattr(rec, "table", None)
        if table not in wanted:
            continue
        cols, vals = list(rec.columns), list(rec.values)
        dcols = [d for d in desc.get(table) or () if d in cols]
        dvals = [vals[cols.index(d)] if cols.index(d) < len(vals) else None for d in dcols]
        loc = Locator("rowid", rec.rowid) if isinstance(rec.rowid, int) else None
        where = rec.prov.where()
        for col, kind, text_numbers in wanted[table]:
            if col not in cols:
                continue
            i = cols.index(col)
            raw = vals[i] if i < len(vals) else None
            dt = to_utc(_as_number(raw) if text_numbers else raw, kind)
            if not _in_range(dt, start, end):
                continue
            n = per.get((table, col), 0)
            if n >= cap:
                continue
            per[(table, col)] = n + 1
            events.append(Event(dt, table, col, kind, loc, "%s @ %s" % (
                "?" if rec.rowid is None else rec.rowid, where), describe(dcols, dvals),
                "Recovered (%s, %s)" % (rec.prov.source, rec.confidence), raw, rec))
    for (table, col), n in sorted(per.items()):
        if n >= cap:
            notes.append("%s.%s: recovered records capped at %s" % (table, col, format(cap, ",")))
    return events, notes


# -- export ---------------------------------------------------------------------------------------------
def _json_raw(v):
    if isinstance(v, InvalidText):
        return {"invalid_text_hex": bytes(v).hex()}
    if isinstance(v, (bytes, bytearray)):
        return {"blob_hex": bytes(v).hex(), "size": len(v)}     # every byte
    if isinstance(v, float) and not math.isfinite(v):
        return {"real": repr(v)}
    return v


def _text_raw(v):
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "x'%s'" % bytes(v).hex()                          # every byte
    if isinstance(v, float):
        return repr(v)
    return str(v)


FIELDS = ("time_utc", "table", "column", "kind", "row", "source", "description", "raw")


def export_fields(events, offset_minutes=0):
    """The columns of a timeline export: FIELDS, plus 'database' when the events come from
    several databases and 'time_local' with a local offset."""
    fields = list(FIELDS)
    if any(e.database for e in events):
        fields.insert(1, "database")     # events of several databases say which
    if offset_minutes:
        fields.insert(1, "time_local")
    return fields


def event_row(e, fields, offset_minutes=0):
    """One event as the values of `fields` for engine.export: 'raw' is the value as stored
    (None, a number, text, bytes), so the one export writer encodes it like every other
    export; the other fields are text ('' only for a database name an event has not)."""
    d = e.as_dict(offset_minutes)
    d["raw"] = e.raw
    return [d.get(k) if k == "raw" else ("" if d.get(k) is None else d.get(k))
            for k in fields]


def export_events(path, fmt, events, info=None, is_protected=None, offset_minutes=0):
    """Write events as 'csv', 'json' or 'html' (UTF-8) without a manifest (the app writes
    timeline exports with engine.export: event_row). is_protected(path) true (a path in the
    evidence folder) refuses before anything is written. Returns the number written."""
    if is_protected is not None and is_protected(path):
        raise ValueError("refusing to write %s: it is inside the evidence folder" % path)
    info = dict(info or {})
    fields = list(FIELDS)
    if any(e.database for e in events):
        fields.insert(1, "database")     # events of several databases say which
    if offset_minutes:
        fields.insert(1, "time_local")
        info.setdefault("local_offset", offset_label(offset_minutes))
    n = 0
    if fmt == "csv":
        f = open(path, "w", encoding="utf-8-sig", errors="backslashreplace", newline="")
        try:
            with f:
                w = csv_writer(f)        # spreadsheet-safe text, NUL as \x00 (engine.csvcells)
                w.writerow(fields)
                for e in events:
                    d = e.as_dict(offset_minutes)
                    d["raw"] = e.raw if isinstance(e.raw, (int, float)) and \
                        not isinstance(e.raw, bool) else _text_raw(e.raw)  # numbers stay numbers
                    w.writerow([d.get(k, "") if d.get(k) is not None else "" for k in fields])
                    n += 1
        except Exception as e:  # noqa: BLE001 - any failure removes the partial file
            try:
                os.remove(path)
                what = "the partly written file was removed"
            except OSError:
                what = "the partly written file is INCOMPLETE"
            raise OSError("cannot write %s: %s (%s)" % (path, e, what))
        return n
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"format": "%s", "info": %s, "events": [' % (
                FILE_FORMAT, json.dumps(info, ensure_ascii=False, default=str)))
            for e in events:
                f.write(",\n " if n else "\n ")
                f.write(json.dumps(e.as_dict(offset_minutes), ensure_ascii=False, default=str))
                n += 1
            f.write("\n]}\n")
        return n
    from .html_report import Report
    count = [0]

    def rows():
        for e in events:
            d = e.as_dict(offset_minutes)
            d["raw"] = _text_raw(e.raw)
            count[0] += 1
            yield [d.get(k) for k in fields]
    details = [(str(k), v if isinstance(v, str) else json.dumps(v, ensure_ascii=False,
                                                                  default=str))
               for k, v in info.items()]
    rep = Report("Timeline", kind="Timeline", details=details)
    rep.add_section("Events")
    rep.add_text("%d events" % len(events))
    rep.add_table("Timeline", fields, rows(), dates={"time_utc": ISO},
                  badges={"kind": {}, "source": {}})
    rep.write(path, is_protected)
    return count[0]
