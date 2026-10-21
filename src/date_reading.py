"""A recovered or older value read as a date the way its column is read.

Deleted records, WAL versions and row history show the stored numbers; an examiner needs the
date they stand for, in UTC. The column's kind is the one the date detector finds for a sample
of the table's live values (as the row window and the Timeline do), so one value alone is never
read as a different kind (FILETIME as Cocoa nanoseconds). Only columns named like dates are
read; 0, -1 and empty values are 'not set', never a date at an epoch.
"""

from engine import timeline as tl
from engine.decode import timestamps

COLUMN_KINDS = {}       # (id(database), table, column) -> kind found for the column, or None


def column_kind(db, table, column):
    """The date kind the detector picks for a sample of table.column (cached), or None."""
    key = (id(db), table, column)
    if key not in COLUMN_KINDS:
        kind = None
        try:
            session = getattr(db, "session", None)
            if session is not None and table:
                kind = tl.suggest_kind(column, tl.sample_column(session, table, column))
        except Exception:               # noqa: BLE001 - the table may be gone: no column kind
            kind = None
        COLUMN_KINDS[key] = kind if kind in tl.KINDS else None
    return COLUMN_KINDS[key]


def date_reading(db, table, column, value):
    """'2021-01-30 13:31:43 UTC · Cocoa nanoseconds' for a value of a date column, '' for
    anything else (other columns, text, BLOBs, unset values)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or tl.is_unset(value):
        return ""
    if tl.name_hint(column) not in ("strong", "weak"):
        return ""
    # the date first, how it was read after it: a narrow column still shows the date
    kind = column_kind(db, table, column)
    if kind is not None:
        when = tl.utc_text(tl.formatter(kind)(value))
        if when and "not set" not in when:
            return "%s · %s" % (when, tl.LABELS[kind])
    found = timestamps.readings(value)
    return "%s · %s" % (found[0][2], found[0][1]) if found else ""
