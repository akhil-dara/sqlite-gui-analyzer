"""Numbers to UTC date-times for the epochs and units forensic data commonly uses.

Everything is computed as epoch + timedelta with integer microseconds, never with
datetime.fromtimestamp (which raises OSError for negative or large values on Windows).
Nothing here raises: impossible values give None / an empty list.

Kinds:
  unix_s, unix_ms, unix_us, unix_ns   since 1970-01-01
  cocoa_s, cocoa_ns                   since 2001-01-01 (Apple Cocoa / Core Data "Mac absolute")
  webkit_us                           microseconds since 1601-01-01 (Chrome/WebKit)
  filetime                            100 ns ticks since 1601-01-01 (Windows FILETIME)
  hfs_s                               seconds since 1904-01-01 (HFS+; classic HFS used local time)
  dotnet_ticks                        100 ns ticks since 0001-01-01 (.NET DateTime.Ticks)
  ole_days                            days since 1899-12-30 (OLE Automation / Excel serial date)
  gps_s                               seconds since 1980-01-06 (GPS epoch)

GPS time counts no leap seconds, so it runs ahead of UTC (18 s since 2017). The offset
depends on the date and some sources already remove it, so gps_s is converted as plain elapsed
seconds with no leap-second correction; the result can be up to 18 s later than true UTC.

This module is the one timestamp decoder of the tool: the row windows, Browse's 'Show as date',
the BLOB Inspector, the Timeline and the exports all use its kinds, names (LABELS long, SHORT
short), plausible years (LO_YEAR..HI_YEAR) and display format (fmt_utc: 'YYYY-MM-DD
HH:MM:SS[.fff] UTC'; ISO 8601 with 'Z' in machine-readable exports).
"""

import math
from datetime import datetime, timedelta

# kind -> (label, epoch, microseconds per unit as (numerator, denominator))
KINDS = (
    ("unix_s", "Unix seconds", datetime(1970, 1, 1), (1000000, 1)),
    ("unix_ms", "Unix milliseconds", datetime(1970, 1, 1), (1000, 1)),
    ("unix_us", "Unix microseconds", datetime(1970, 1, 1), (1, 1)),
    ("unix_ns", "Unix nanoseconds", datetime(1970, 1, 1), (1, 1000)),
    ("cocoa_s", "Cocoa / Mac absolute seconds", datetime(2001, 1, 1), (1000000, 1)),
    ("cocoa_ns", "Cocoa nanoseconds", datetime(2001, 1, 1), (1, 1000)),
    ("webkit_us", "WebKit / Chrome microseconds", datetime(1601, 1, 1), (1, 1)),
    ("filetime", "Windows FILETIME", datetime(1601, 1, 1), (1, 10)),
    ("hfs_s", "HFS+ seconds", datetime(1904, 1, 1), (1000000, 1)),
    ("dotnet_ticks", ".NET ticks", datetime(1, 1, 1), (1, 10)),
    ("ole_days", "OLE Automation days", datetime(1899, 12, 30), (86400000000, 1)),
    ("gps_s", "GPS seconds", datetime(1980, 1, 6), (1000000, 1)),
)
_BY_KIND = dict((k[0], k) for k in KINDS)
LABELS = dict((k[0], k[1]) for k in KINDS)
# the short names grids and column headers use ('UTC Unix ms')
SHORT = {"unix_s": "Unix s", "unix_ms": "Unix ms", "unix_us": "Unix µs",
         "unix_ns": "Unix ns", "cocoa_s": "Cocoa s", "cocoa_ns": "Cocoa ns",
         "webkit_us": "WebKit µs", "filetime": "FILETIME", "hfs_s": "HFS+ s",
         "dotnet_ticks": ".NET ticks", "ole_days": "OLE days", "gps_s": "GPS s"}
LO_YEAR, HI_YEAR = 1990, 2040       # the years a reading must fall in to count as plausible
EPOCH_SLACK = timedelta(days=1)     # guess() ignores results this close to a kind's epoch
# readings() of a lone number (no column name to go by): these kinds, a value at least this
# large (smaller numbers are ids, counts and sizes far more often than dates), and a date more
# than a year after the kind's epoch (a small counter reads as such a date)
READING_KINDS = ("unix_s", "unix_ms", "unix_us", "unix_ns", "webkit_us", "filetime",
                 "cocoa_s", "cocoa_ns", "dotnet_ticks")
READING_MIN = 10 ** 8
READING_EPOCH_GAP = timedelta(days=366)


def _number(value):
    """int or finite float from an int, float or numeric string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("ascii")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 64:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def to_datetime(value, kind):
    """Naive UTC datetime for value interpreted as `kind`, or None if impossible."""
    spec = _BY_KIND.get(kind)
    number = _number(value)
    if spec is None or number is None:
        return None
    epoch, (num, den) = spec[2], spec[3]
    try:
        if kind == "ole_days" and number < 0:
            # OLE dates below zero count whole days back but the time of day forwards.
            whole = math.trunc(number)
            micros = whole * num + int(round(abs(number - whole) * num))
        elif isinstance(number, int):
            micros = number * num // den
        else:
            micros = int(round(number * num / den))
        return epoch + timedelta(microseconds=micros)
    except (OverflowError, ValueError, TypeError):
        return None


def iso(dt):
    """'YYYY-MM-DDTHH:MM:SS[.ffffff]Z' for a naive UTC datetime (any year, any platform)."""
    text = "%04d-%02d-%02dT%02d:%02d:%02d" % (dt.year, dt.month, dt.day, dt.hour, dt.minute,
                                              dt.second)
    if dt.microsecond:
        text += ".%06d" % dt.microsecond
    return text + "Z"


def to_iso(value, kind):
    """ISO 8601 UTC string for value interpreted as `kind`, or None."""
    dt = to_datetime(value, kind)
    return iso(dt) if dt is not None else None


def fmt_time(dt):
    """'YYYY-MM-DD HH:MM:SS[.mmm|.uuuuuu]' of a naive datetime (any year)."""
    text = "%04d-%02d-%02d %02d:%02d:%02d" % (dt.year, dt.month, dt.day, dt.hour, dt.minute,
                                              dt.second)
    if dt.microsecond:
        text += (".%03d" % (dt.microsecond // 1000)) if dt.microsecond % 1000 == 0 \
            else (".%06d" % dt.microsecond)
    return text


def fmt_utc(dt):
    """How the tool shows a date-time: 'YYYY-MM-DD HH:MM:SS[.fff] UTC'."""
    return fmt_time(dt) + " UTC"


def readings(value, kinds=READING_KINDS, lo_year=LO_YEAR, hi_year=HI_YEAR):
    """Plausible dates of a lone number (a row window's value, the Browse row inspector):
    [(kind, long label, 'YYYY-MM-DD HH:MM:SS UTC')], most plausible first. Numbers below
    READING_MIN, BLOBs, text and booleans give []; see READING_KINDS for the kinds tried."""
    number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if number is None or (isinstance(number, float) and not math.isfinite(number)) \
            or abs(number) < READING_MIN:
        return []
    out = []
    for kind, _text in guess(number, lo_year, hi_year):
        if kind not in kinds:
            continue
        dt = to_datetime(number, kind)
        if dt is None or abs(dt - _BY_KIND[kind][2]) <= READING_EPOCH_GAP:
            continue
        out.append((kind, LABELS[kind], fmt_utc(dt)))
    return out


def guess(value, lo_year=LO_YEAR, hi_year=HI_YEAR):
    """Plausible readings of value as a timestamp: [(kind, iso_utc_string), ...].

    Keeps kinds whose date falls in lo_year..hi_year (inclusive) and is more than a day away
    from that kind's epoch (a value near zero is not evidence of a timestamp). Ranked by
    closeness to the middle of the window, then by the order of KINDS, so the result is
    deterministic.
    """
    try:
        lo_year, hi_year = int(lo_year), int(hi_year)
        center = datetime(max(1, min(9999, (lo_year + hi_year) // 2)), 7, 1)
    except (TypeError, ValueError, OverflowError):
        return []
    found = []
    for rank, (kind, _label, epoch, _unit) in enumerate(KINDS):
        dt = to_datetime(value, kind)
        if dt is None or not lo_year <= dt.year <= hi_year:
            continue
        if abs(dt - epoch) <= EPOCH_SLACK:
            continue
        found.append((abs((dt - center).total_seconds()), rank, kind, iso(dt)))
    found.sort()
    return [(kind, text) for _dist, _rank, kind, text in found]
