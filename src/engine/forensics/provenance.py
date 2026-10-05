"""Where a recovered record came from, and how sure we are about it.

Every record the forensics engine returns (carved, historical or schema) is a Record with a
Provenance. Records are plain Python values so the UI and the report writers need no engine
knowledge; as_dict() gives a JSON-safe form.

Source kinds (Provenance.source):
  live         the row as it is now (current state, WAL applied)
  wal          an intact cell on a WAL frame's copy of a page that is not the current version
               (superseded, uncommitted or stale frame)
  replaced     an intact cell on a main-file page whose current version is in the WAL
  journal      an intact cell on a rollback-journal page image (the pre-transaction content)
  freeblock    carved from a freeblock inside a b-tree page (leading bytes reconstructed)
  unallocated  carved from the gap between the cell pointer array and the cell content area
  freelist     from a freelist trunk or leaf page (intact old cells or carved bytes)
  orphan       from a b-tree page that no tree and no freelist references
  schema       a sqlite_master row that is no longer in the current schema
File (Provenance.file): "main", "wal" or "journal": which evidence file holds the bytes.
"""

import hashlib

from ..fileformat.record import InvalidText

LIVE, WAL, REPLACED, JOURNAL = "live", "wal", "replaced", "journal"
FREEBLOCK, UNALLOCATED, FREELIST, ORPHAN, SCHEMA = (
    "freeblock", "unallocated", "freelist", "orphan", "schema")
SOURCES = (LIVE, WAL, REPLACED, JOURNAL, FREEBLOCK, UNALLOCATED, FREELIST, ORPHAN, SCHEMA)
CARVE_SOURCES = (FREEBLOCK, UNALLOCATED, FREELIST, ORPHAN, WAL, REPLACED, JOURNAL)

MAIN_FILE, WAL_FILE, JOURNAL_FILE = "main", "wal", "journal"

HIGH, MEDIUM, LOW = "high", "medium", "low"
CONFIDENCES = (HIGH, MEDIUM, LOW)
_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}


def worst(a, b):
    """The lower of two confidence levels."""
    return a if _RANK[a] >= _RANK[b] else b


class Provenance(object):
    """Location of the bytes a record was read from.

    page/offset: page number (1-based) and byte offset of the cell inside that page image.
    length: bytes the cell occupies on that page (local part only).
    frame/frame_state/commit_group: WAL frame index, its state (current / superseded /
    uncommitted / stale) and commit group, when file == 'wal'.
    overflow: overflow page numbers followed for the payload.
    """
    __slots__ = ("source", "file", "page", "offset", "length", "frame", "frame_state",
                 "commit_group", "overflow")

    def __init__(self, source, file, page, offset=None, length=None, frame=None,
                 frame_state=None, commit_group=None, overflow=()):
        self.source, self.file, self.page, self.offset = source, file, page, offset
        self.length, self.frame, self.frame_state = length, frame, frame_state
        self.commit_group, self.overflow = commit_group, tuple(overflow)

    def key(self):
        return "|".join(str(x) for x in (self.file, self.frame, self.page, self.offset, self.source))

    def where(self):
        """Short human-readable location, e.g. 'WAL frame 12 (superseded) page 7 @ 3001'."""
        if self.file == WAL_FILE:
            head = "WAL frame %s (%s) page %s" % (self.frame, self.frame_state, self.page)
        elif self.file == JOURNAL_FILE:
            head = "journal page %s" % self.page
        else:
            head = "page %s" % self.page
        return head if self.offset is None else "%s @ %d" % (head, self.offset)

    def as_dict(self):
        return {"source": self.source, "file": self.file, "page": self.page,
                "offset": self.offset, "length": self.length, "frame": self.frame,
                "frame_state": self.frame_state, "commit_group": self.commit_group,
                "overflow": list(self.overflow)}

    def __repr__(self):
        return "Provenance(%s, %s)" % (self.source, self.where())


class Record(object):
    """One recovered row.

    table: best matching table name, or None when no table's schema fits.
    candidates: every table whose schema fits (table first when known).
    columns/values: the row in declared column order (values are Python values: None, int,
      float, str, bytes, InvalidText). rowid: int, or None when unknown (e.g. overwritten by a
      freeblock header, or a WITHOUT ROWID table).
    confidence: 'high' | 'medium' | 'low'; reasons: why (list of short strings).
    flags: set of tags, e.g. 'rowid_unknown', 'truncated', 'overflow_partial',
      'prior_version' (the key is live with other values), 'uncertain_value'.
    copies: Provenance of further locations holding the identical record.
    index: for an index entry (flag 'index_entry'), the index's name; table is then the
      indexed table and columns are the indexed columns followed by 'rowid'. None otherwise.
    """
    __slots__ = ("table", "candidates", "columns", "values", "rowid", "prov", "confidence",
                 "reasons", "flags", "copies", "index", "_id")

    def __init__(self, table, columns, values, prov, confidence=LOW, reasons=(), rowid=None,
                 candidates=(), flags=(), index=None):
        self.table, self.columns, self.values = table, list(columns), list(values)
        self.prov, self.confidence, self.rowid = prov, confidence, rowid
        self.reasons = list(reasons)
        self.candidates = list(candidates) or ([table] if table else [])
        self.flags = set(flags)
        self.copies = []
        self.index = index
        self._id = None

    @property
    def label(self):
        """'table' or 'table [index name]' for an index entry."""
        base = self.table or "(unknown)"
        return base if self.index is None else "%s [index %s]" % (base, self.index)

    @property
    def source(self):
        return self.prov.source

    @property
    def id(self):
        """Stable identifier: the same evidence always gives the same id for the same record."""
        if self._id is None:
            raw = "%s|%s" % (self.prov.key(), self.table or "")
            if self.index is not None:
                raw += "|index|" + self.index
            self._id = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
        return self._id

    def values_dict(self):
        return dict(zip(self.columns, self.values))

    def identity(self):
        """Hashable content identity used to merge identical copies of one record."""
        return (self.table, self.rowid, tuple(value_key(v) for v in self.values))

    def as_dict(self):
        return {"id": self.id, "table": self.table, "candidates": list(self.candidates),
                "index": self.index, "rowid": self.rowid, "columns": list(self.columns),
                "values": [json_value(v) for v in self.values],
                "confidence": self.confidence, "reasons": list(self.reasons),
                "flags": sorted(self.flags), "provenance": self.prov.as_dict(),
                "copies": [p.as_dict() for p in self.copies]}

    def __repr__(self):
        return "Record(%s, %s, %s, %r)" % (self.table, self.prov.where(), self.confidence,
                                           self.values)


def value_key(v):
    """Type-tagged, hashable form of a value (1 != 1.0 != '1', text != blob)."""
    if v is None:
        return (0, None)
    if isinstance(v, InvalidText):
        return (4, bytes(v))
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, int):
        return (1, v)
    if isinstance(v, float):
        return (2, repr(v))
    if isinstance(v, bytes):
        return (5, v)
    return (3, v)


BLOB_JSON_CAP = 1 << 20


def json_value(v, cap=BLOB_JSON_CAP):
    """JSON-safe form of a value: bytes become {'blob_hex': ...}; floats that JSON cannot
    carry (nan, inf) become {'real': repr}."""
    if isinstance(v, InvalidText):
        raw = bytes(v)
        out = {"invalid_text_hex": raw[:cap].hex(), "size": len(raw)}
        if len(raw) > cap:
            out["truncated"] = True
        return out
    if isinstance(v, bytes):
        out = {"blob_hex": v[:cap].hex(), "size": len(v)}
        if len(v) > cap:
            out["truncated"] = True
        return out
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return {"real": repr(v)}
    return v
