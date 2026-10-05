"""Row tags: bookmarks with a note on rows of an evidence database, kept per database together
with the saved Browse view state, plus the app-level settings (recent databases).

Everything lives in the user's app-data folder (data_dir()), never next to the evidence: a
store refuses to write anywhere inside the folder of the database it belongs to, however the
app-data folder is configured. Files are written atomically (a temporary file in the same
folder, then os.replace), so an interrupted save never leaves half a file.

A tag entry keeps the row as it was when tagged (the snapshot): exports and the Tagged tab show
those values even if the row is gone later. Keys identify a row across sessions:

  database rows     DB|<table>|<locator json>
  view rows         DB|<view>|values:<sha1 of the values json>  (an ordinal locator is a position
                    in one particular read, so the values identify the row)
  WAL row versions  WAL|<table>|<locator json>|<sha1 of the values json>
  freelist records  Freelist|<page>|<cell offset>
  other records     <source>|<id>  (e.g. carved records, which have a stable 16-hex id)

Each entry also names its database ({"identity": normalised path + size, "path", "name",
"size"}), so rows of several databases examined together (a case) are told apart; a file
written before cases has none, and its entries get the store's own database when loaded.
Case files (write_case / read_case) list the databases of a case in case-lists/.

File format (JSON): {"format": "sqlite-gui-analyzer-tags", "version": 1, "saved_utc",
"evidence": {path, size, mtime_ns, sha256}, "tag_defs": [{name, color}], "entries": [...],
"state": {"last_table": ..., "tables": {table: {"widths", "hidden", "sort"}},
"date_formats": {table: {column: kind}}, "timeline": {"overrides", "offset", ...}}}.

No Tk here: the UI (tagging.py, tags_tab.py) drives it.
"""

import base64
import copy
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections import OrderedDict

from .evidence import inside_any, path_inside
from .filenames import safe_file_name
from .fileformat.record import InvalidText
from .schema import Locator

FILE_FORMAT = "sqlite-gui-analyzer-tags"
FILE_VERSION = 1
DATA_DIR_ENV = "SGA_DATA_DIR"        # overrides the app-data folder (tests use a temp folder)
RECENT_MAX = 10
BLOB_INLINE_CAP = 1 << 20            # default of the limit tag_blob_bytes (kept whole)
BLOB_HEAD = 64 << 10                 # of a larger one: its SHA-256, size and first 64 KiB

DEFAULT_TAGS = (("Relevant", "#1f9d55"), ("Review", "#e8a200"), ("Suspicious", "#d93a1e"),
                ("Not relevant", "#8993a4"))
# colours offered to new tags, in order (the defaults' colours come first)
PALETTE = ("#1f9d55", "#e8a200", "#d93a1e", "#8993a4", "#0065ff", "#6554c0", "#00a3bf",
           "#c9377e", "#7a5d00", "#403294")

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class TagError(Exception):
    """A tag operation that cannot be done (a refused location, a name in use, a bad file)."""


# -- places ------------------------------------------------------------------------------------
OVERRIDE_FILE = "datadir.txt"   # in the default folder: points at a user-picked folder


def _default_data_dir(platform=None, environ=None):
    env = os.environ if environ is None else environ
    if (platform or sys.platform) == "win32":
        base = env.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "SQLite GUI Analyzer")
    base = env.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "sqlite-gui-analyzer")


def data_dir_override(platform=None, environ=None):
    """The folder picked in View > Storage (the datadir.txt pointer), or None."""
    ptr = os.path.join(_default_data_dir(platform, environ), OVERRIDE_FILE)
    try:
        with open(ptr, "r", encoding="utf-8") as f:
            path = f.read().strip()
    except (OSError, ValueError):
        return None
    return os.path.abspath(path) if path else None


def set_data_dir_override(path, platform=None, environ=None):
    """Point the app at another data folder (None: back to the default). Takes effect
    on restart; the pointer is a small text file in the default folder."""
    default = _default_data_dir(platform, environ)
    ptr = os.path.join(default, OVERRIDE_FILE)
    if path is None:
        try:
            os.remove(ptr)
        except OSError:
            pass
        return
    os.makedirs(default, exist_ok=True)
    with open(ptr, "w", encoding="utf-8") as f:
        f.write(os.path.abspath(path))


def data_dir(platform=None, environ=None):
    """Folder for tag files and settings: $SGA_DATA_DIR when set; else the folder picked
    in View > Storage; else %APPDATA%\\SQLite GUI Analyzer on Windows and
    $XDG_DATA_HOME/sqlite-gui-analyzer (~/.local/share/...) elsewhere.
    platform and environ default to this process's (tests pass others)."""
    env = os.environ if environ is None else environ
    override = env.get(DATA_DIR_ENV)
    if override:
        return os.path.abspath(override)
    picked = data_dir_override(platform, environ)
    if picked:
        return picked
    return _default_data_dir(platform, environ)


def safe_name(text, limit=60):
    """Text usable in a file name (letters, digits, '.', '-', '_'; engine.filenames)."""
    return safe_file_name(text, limit, default="x", strip="._")


def tag_file_path(db_path, directory=None):
    """cases/<database name>-<12 hex of sha1(normalised absolute path)>.json in the app-data
    folder: one file per database path, readable name, no collision between equal names."""
    norm = os.path.normcase(os.path.abspath(db_path))
    digest = hashlib.sha1(norm.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return os.path.join(directory or data_dir(), "cases",
                        "%s-%s.json" % (safe_name(os.path.basename(db_path)), digest))


def inside(path, folder):
    """True when path is folder itself or lies inside it, compared as absolute paths, as
    resolved ones and by file identity (a link, junction or another name for the folder must
    not lead a write into it): engine.evidence.path_inside."""
    return path_inside(path, folder)


def utc_now():
    """UTC time in ISO 8601 with milliseconds, e.g. 2026-09-29T10:15:02.123Z."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def write_json_atomic(path, data, refuse_in=None):
    """Write data as JSON to path through a temporary file in the same folder and os.replace,
    so readers see the old file or the new one, never a partial one. refuse_in: a folder
    (the evidence folder) that must not be written to: TagError before anything is created."""
    path = os.path.abspath(path)
    if refuse_in and inside(path, refuse_in):
        raise TagError("refusing to write %s: it is inside the evidence folder %s"
                       % (path, refuse_in))
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path)[:40], suffix=".tmp",
                               dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:     # Windows: a scanner or indexer holds the old file
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


# -- values and locators -----------------------------------------------------------------------
class PartialBlob(bytes):
    """A BLOB larger than BLOB_INLINE_CAP as a snapshot keeps it: its first BLOB_HEAD bytes.
    .size and .sha256 describe the whole value."""
    size = 0
    sha256 = ""


def _blob_cap():
    """The limit tag_blob_bytes: larger BLOBs keep their SHA-256, size and first part."""
    from . import limits
    return limits.get("tag_blob_bytes")


def encode_value(v):
    """JSON-safe form of a row value. Numbers and text stay as they are; BLOBs up to 1 MiB
    become {"$b64", "size"}, larger ones {"$sha256", "size", "$b64_head"}; invalid text keeps its
    bytes ({"$invalid_text_b64"}); a float JSON cannot carry becomes {"$real": repr}."""
    if v is None or isinstance(v, bool):
        return v
    cap = _blob_cap()
    if isinstance(v, InvalidText):
        raw = bytes(v)
        out = {"$invalid_text_b64": base64.b64encode(raw[:cap]).decode("ascii"),
               "size": len(raw)}
        if len(raw) > cap:
            out["truncated"] = True
        return out
    if isinstance(v, PartialBlob):
        return {"$sha256": v.sha256, "size": v.size,
                "$b64_head": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, (bytes, bytearray, memoryview)):
        raw = bytes(v)
        if len(raw) <= cap:
            return {"$b64": base64.b64encode(raw).decode("ascii"), "size": len(raw)}
        return {"$sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                "$b64_head": base64.b64encode(raw[:BLOB_HEAD]).decode("ascii")}
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else {"$real": repr(v)}
    if isinstance(v, str):
        return v
    if isinstance(v, Locator):
        return {"$locator": locator_to_json(v)}
    return {"$text": str(v)}


def decode_value(v):
    """The Python value encode_value() stored (a PartialBlob for a BLOB kept in part). A value
    a hand-edited file spoiled comes back as text saying so, never as an exception."""
    if not isinstance(v, dict):
        return v
    try:
        return _decode_dict(v)
    except (ValueError, TypeError):     # bad base64 or hex (binascii.Error is a ValueError)
        return "[unreadable value: %s]" % json.dumps(v, sort_keys=True)[:200]


def _decode_dict(v):
    if "$b64" in v:
        return base64.b64decode(v["$b64"])
    if "$sha256" in v:
        p = PartialBlob(base64.b64decode(v.get("$b64_head") or ""))
        p.size, p.sha256 = v.get("size") or len(p), v["$sha256"]
        return p
    if "$invalid_text_b64" in v:
        return InvalidText(base64.b64decode(v["$invalid_text_b64"]))
    if "$real" in v:
        return float(v["$real"])
    if "$locator" in v:
        return locator_from_json(v["$locator"])
    if "$text" in v:
        return v["$text"]
    return json.dumps(v, sort_keys=True)


def _key_part(v):
    """One primary-key value in a locator's JSON form (bytes as hex: keys stay readable)."""
    if isinstance(v, InvalidText):
        return {"$invalid_hex": bytes(v).hex()}
    if isinstance(v, (bytes, bytearray)):
        return {"$hex": bytes(v).hex()}
    if isinstance(v, float) and not (v == v and v not in (float("inf"), float("-inf"))):
        return {"$real": repr(v)}
    if isinstance(v, (tuple, list)):
        return [_key_part(x) for x in v]
    return v


def _key_value(v):
    if isinstance(v, dict):
        if "$hex" in v:
            return bytes.fromhex(v["$hex"])
        if "$invalid_hex" in v:
            return InvalidText(bytes.fromhex(v["$invalid_hex"]))
        if "$real" in v:
            return float(v["$real"])
    if isinstance(v, list):
        return tuple(_key_value(x) for x in v)
    return v


def locator_to_json(loc):
    """{"kind", "value"} of an engine Locator (None stays None); a primary key is a list."""
    if loc is None:
        return None
    if isinstance(loc, dict):
        return loc
    value = loc.value
    if isinstance(value, (tuple, list)):
        value = [_key_part(x) for x in value]
    else:
        value = _key_part(value)
    return {"kind": loc.kind, "value": value}


def locator_from_json(d, snapshot=None):
    """The Locator locator_to_json() described (a primary key comes back as a tuple)."""
    if not d:
        return None
    value = _key_value(d.get("value"))
    if d.get("kind") == "pk" and not isinstance(value, tuple):
        value = (value,)
    return Locator(d.get("kind"), value, snapshot=snapshot)


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def values_digest(values):
    """sha1 of the canonical JSON of a row's values: the same values give the same digest in
    every session (1 and 1.0, text and BLOB stay different)."""
    return hashlib.sha1(_canon([encode_value(v) for v in values]).encode(
        "utf-8", "surrogatepass")).hexdigest()


# -- keys --------------------------------------------------------------------------------------
def db_key(table, locator, values=None):
    """Key of a database row. Rows with an ordinal locator (views, virtual tables) are keyed by
    their values (given, or the snapshot the locator carries)."""
    if locator is not None and getattr(locator, "kind", None) == "ordinal":
        if values is None and getattr(locator, "snapshot", None) is not None:
            values = locator.snapshot[1]
        return "DB|%s|values:%s" % (table, values_digest(values or ()))
    return "DB|%s|%s" % (table, _canon(locator_to_json(locator)))


def wal_key(table, locator, values):
    """Key of one version of a row kept in WAL frames (the same version in several frames is
    one key)."""
    return "WAL|%s|%s|%s" % (table, _canon(locator_to_json(locator)), values_digest(values))


def freelist_key(page, cell_offset):
    return "Freelist|%s|%s" % (page, cell_offset)


def record_key(source, record_id):
    return "%s|%s" % (source, record_id)


# -- tag definitions and entries ---------------------------------------------------------------
def valid_color(color, default="#8993a4"):
    """color when it is '#rrggbb', else default (colours end up in HTML style attributes)."""
    return color if isinstance(color, str) and _COLOR_RE.match(color) else default


def tint(color, amount=0.78):
    """A light version of '#rrggbb' (mixed with white) for row backgrounds."""
    c = valid_color(color)
    rgb = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (255 - x) * amount) for x in rgb)


class TagDef(object):
    __slots__ = ("name", "color")

    def __init__(self, name, color):
        self.name, self.color = name, valid_color(color)

    def as_dict(self):
        return {"name": self.name, "color": self.color}

    def __repr__(self):
        return "TagDef(%r, %r)" % (self.name, self.color)


class TagEntry(object):
    """One tagged row. values are kept JSON-safe (encode_value); row_values() decodes them.
    locator is the JSON form (locator_to_json) or None for records without one; provenance
    says where the row was found (frame, frame_state, page, cell_offset, confidence, ...)."""
    __slots__ = ("key", "source", "table", "locator", "rowid", "tags", "note", "tagged_at",
                 "updated_at", "columns", "values", "provenance", "database")

    def __init__(self, key, source, table, locator=None, rowid="", columns=(), values=(),
                 provenance=None, tags=(), note="", tagged_at=None, updated_at=None,
                 database=None):
        self.key, self.source, self.table = key, source, table
        # the database the row belongs to: {"identity", "path", "name", "size"} (database_info);
        # None until a store or the caller sets it (a store gives it its own)
        self.database = dict(database) if isinstance(database, dict) else None
        self.locator = locator
        self.rowid = "" if rowid is None else str(rowid)
        self.columns, self.values = list(columns), list(values)
        self.provenance = dict(provenance or {})
        self.tags, self.note = list(tags), note or ""
        self.tagged_at = tagged_at or utc_now()
        self.updated_at = updated_at or self.tagged_at

    def as_dict(self):
        return {"key": self.key, "source": self.source, "table": self.table,
                "locator": self.locator, "rowid": self.rowid, "tags": list(self.tags),
                "note": self.note, "tagged_at": self.tagged_at, "updated_at": self.updated_at,
                "columns": list(self.columns), "values": list(self.values),
                "provenance": dict(self.provenance), "database": dict(self.database or {})}

    @classmethod
    def from_dict(cls, d):
        """An entry from its as_dict() form; fields of the wrong type (a hand-edited file) are
        dropped rather than trusted."""
        if not isinstance(d, dict) or not isinstance(d.get("key"), str) or not d["key"]:
            raise TagError("tag entry without a key")

        def of(name, kind, default):
            v = d.get(name)
            return v if isinstance(v, kind) else default
        loc = of("locator", dict, None)
        return cls(d["key"], of("source", str, "") or "DB", of("table", str, ""),
                   loc if loc and "kind" in loc else None, d.get("rowid", ""),
                   [str(c) for c in of("columns", list, [])], of("values", list, []),
                   of("provenance", dict, {}),
                   [t for t in of("tags", list, []) if isinstance(t, str) and t],
                   of("note", str, ""), of("tagged_at", str, None), of("updated_at", str, None),
                   _database_of(d.get("database")))

    def copy(self):
        return TagEntry.from_dict(copy.deepcopy(self.as_dict()))

    @property
    def identity(self):
        """The identity of the entry's database ('' when not known)."""
        return (self.database or {}).get("identity") or ""

    @property
    def case_key(self):
        """The key of the row within a case of several databases: database identity + key."""
        return "%s\x1f%s" % (self.identity, self.key)

    def row_values(self):
        return [decode_value(v) for v in self.values]

    def row_locator(self):
        """The engine Locator (an ordinal one carries the snapshot, as a view row does)."""
        if not self.locator:
            return None
        snap = (list(self.columns), self.row_values()) \
            if self.locator.get("kind") == "ordinal" else None
        return locator_from_json(self.locator, snapshot=snap)

    def __repr__(self):
        return "TagEntry(%r, %r)" % (self.key, self.tags)


def database_info(path, size=None):
    """{"identity", "path", "name", "size"} of a database (what a tag entry keeps of it)."""
    from .case import db_identity
    path = os.path.abspath(path)
    return {"identity": db_identity(path, size), "path": path, "name": os.path.basename(path),
            "size": size}


def _database_of(d):
    """A tag file's "database" of an entry, when it is one (a hand-edited file may hold else)."""
    if not isinstance(d, dict) or not isinstance(d.get("identity"), str) or not d["identity"]:
        return None
    size = d.get("size")
    return {"identity": d["identity"], "path": str(d.get("path") or ""),
            "name": str(d.get("name") or ""),
            "size": size if isinstance(size, int) and not isinstance(size, bool) else None}


def _entry(key, source, table, locator, rowid, columns, values, provenance):
    values = list(values)
    cols = [str(c) for c in columns][:len(values)] if len(columns) > len(values) \
        else [str(c) for c in columns]
    cols += ["col%d" % i for i in range(len(cols), len(values))]
    prov = dict((k, v) for k, v in (provenance or {}).items() if v not in (None, "", [], ()))
    return TagEntry(key, source, table, locator_to_json(locator), rowid, cols,
                    [encode_value(v) for v in values], prov)


def entry_from_db_row(table, locator, columns, values, flags=None):
    """A row of a table or view (columns and values without the locator column)."""
    prov = {"flags": sorted(flags)} if flags else {}
    return _entry(db_key(table, locator, values), "DB", table, locator,
                  locator.display() if locator is not None else "", columns, values, prov)


def entry_from_wal_record(rec, frames=None):
    """A row version from WAL frames: a WALParser.recover_all_records() dict (table, locator,
    values_dict, raw_values, flags, frame_idx, page_num, category, rowid). frames: every
    (frame, page, state) holding the same version, when known."""
    values = list(rec.get("raw_values") or ())
    cols = list((rec.get("values_dict") or {}).keys())
    prov = {"frame": rec.get("frame_idx"), "page": rec.get("page_num"),
            "frame_state": rec.get("category")}
    if frames:
        prov["frames"] = [list(f) for f in sorted(frames)]
    if rec.get("flags"):
        prov["flags"] = sorted(rec["flags"])
    loc = rec.get("locator")
    return _entry(wal_key(rec["table"], loc, values), "WAL", rec["table"], loc,
                  rec.get("rowid", loc.display() if loc is not None else ""), cols, values, prov)


def entry_from_freelist(table, page, cell_offset, columns, values, rowid=None, confidence=None,
                        flags=None):
    """A record still held by a freed page."""
    prov = {"page": page, "cell_offset": cell_offset, "confidence": confidence}
    if flags:
        prov["flags"] = sorted(flags)
    return _entry(freelist_key(page, cell_offset), "Freelist", table, None,
                  "" if rowid in (None, "-") else rowid, columns, values, prov)


def entry_from_record(record):
    """A generic record: a dict {source, table, columns, values, rowid, provenance, id} (e.g. a
    carved record from the Forensics tab), an engine.forensics Record, or a DB.record_rows()
    display dict (its 'record' is used). The key is '<source>|<id>'; without an id, one is
    made from the table and the values. 'raw_values', when present, wins over 'values'."""
    if isinstance(record, dict) and hasattr(record.get("record"), "prov"):
        record = record["record"]
    if not isinstance(record, dict):
        prov = record.prov.as_dict() if getattr(record, "prov", None) is not None else {}
        prov["confidence"] = getattr(record, "confidence", None)
        if getattr(record, "reasons", None):
            prov["reasons"] = list(record.reasons)
        if getattr(record, "index", None) is not None:
            prov["index"] = record.index            # an index entry: indexed values + rowid
        record = {"source": "Carved", "table": record.table or "(unknown table)",
                  "columns": record.columns, "values": record.values, "rowid": record.rowid,
                  "provenance": prov, "id": record.id}
    values = list(record["raw_values"] if record.get("raw_values") is not None
                  else record.get("values") or ())
    table = record.get("table") or "(unknown table)"
    source = record.get("source") or "Carved"
    rid = record.get("id") or hashlib.sha1(
        (table + "|" + values_digest(values)).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    prov = dict(record.get("provenance") or {})
    loc = record.get("locator")
    return _entry(record_key(source, rid), source, table,
                  loc if isinstance(loc, Locator) and loc.kind != "ordinal" else None,
                  record.get("rowid"), record.get("columns") or (), values, prov)


def group_key(group):
    """Key of a search result line (a search_results.RowGroup, or anything with its table,
    locator, source, row and hits), without reading the row: None for a column-name match."""
    first = group.hits[0] if group.hits else {}
    if group.source == "Freelist":
        return freelist_key(first.get("page"), first.get("cell_offset"))
    if group.locator is None:
        return None
    if group.source == "WAL":
        return wal_key(group.table, group.locator, group.row or ())
    return db_key(group.table, group.locator)


def entry_from_group(group, columns=None, values=None, flags=None):
    """Entry for a search result line. A database row needs its columns and values (read by
    the caller), unless its locator carries them (a view row)."""
    first = group.hits[0] if group.hits else {}
    loc = group.locator
    if group.source == "Freelist":
        snap = getattr(loc, "snapshot", None)
        cols, vals = (snap[0], snap[1]) if snap else (columns or (), group.row or ())
        return entry_from_freelist(group.table, first.get("page"), first.get("cell_offset"),
                                   cols, vals, first.get("rowid"), first.get("confidence"))
    if loc is None:
        raise TagError("a column-name match is not a row")
    if group.source == "WAL":
        rec = {"table": group.table, "locator": loc, "raw_values": list(group.row or ()),
               "values_dict": OrderedDict((c, None) for c in (first.get("row_data") or {})),
               "rowid": first.get("rowid"), "frame_idx": first.get("frame_idx"),
               "page_num": first.get("page_num"), "category": first.get("category")}
        return entry_from_wal_record(rec, frames=group.frames)
    if values is None:
        snap = getattr(loc, "snapshot", None)
        if snap is None:
            raise TagError("the row's values are needed to tag it")
        columns, values = snap[0], snap[1]
    e = entry_from_db_row(group.table, loc, columns or (), values, flags)
    if group.frames:
        e.provenance["wal_frames"] = [list(f) for f in sorted(group.frames)]
    return e


# -- the store ---------------------------------------------------------------------------------
class TagStore(object):
    """Tags of one database: definitions, entries, saved view state, persisted as JSON in the
    app-data folder. Every change sets `dirty` (save() writes it); tag changes also bump
    `revision` so views know to redraw."""

    def __init__(self, db_path, path=None, directory=None):
        self.db_path = os.path.abspath(db_path)
        self.evidence_dir = os.path.dirname(self.db_path)
        self.path = os.path.abspath(path) if path else tag_file_path(self.db_path, directory)
        self.defs = [TagDef(n, c) for n, c in DEFAULT_TAGS]
        self._entries = OrderedDict()
        self._tables = None                 # table -> entries (computed when asked)
        self.state = {}
        self.evidence = {"path": self.db_path}
        self.database = database_info(self.db_path)     # given to entries without one
        self.saved_evidence = None          # what the loaded file was saved for
        self.warnings = []
        self.dirty = False
        self.revision = 0
        self.loaded = False

    # -- persistence -----------------------------------------------------------
    def load(self):
        """Read this database's tag file if there is one; True when it was read. An unreadable
        file is moved aside (never deleted) and reported in self.warnings."""
        if not os.path.isfile(self.path):
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._apply(data, merge=False)
        except (OSError, ValueError, TagError, TypeError, KeyError, AttributeError) as e:
            aside = "%s.unreadable-%s" % (self.path, time.strftime("%Y%m%d-%H%M%S"))
            try:
                os.replace(self.path, aside)
                kept = "kept as %s" % aside
            except OSError as e2:
                kept = "left in place (%s)" % e2
            self.warnings.append("The tag file %s could not be read (%s); it was %s. Tags start "
                                 "empty." % (self.path, e, kept))
            return False
        self.saved_evidence = data.get("evidence") or None
        self.loaded = True
        self.dirty = False
        return True

    def to_data(self, entries=None):
        """The tag-file content (entries: a subset to write, default all)."""
        return {"format": FILE_FORMAT, "version": FILE_VERSION, "saved_utc": utc_now(),
                "evidence": dict(self.evidence),
                "tag_defs": [d.as_dict() for d in self.defs],
                "entries": [e.as_dict() for e in (self._entries.values() if entries is None
                                                  else entries)],
                "state": copy.deepcopy(self.state)}

    def save(self, force=False):
        """Write the tag file when something changed (or force). Refused (TagError) when the
        file would be inside the evidence folder."""
        if not (self.dirty or force):
            return False
        write_json_atomic(self.path, self.to_data(), refuse_in=self.evidence_dir)
        self.dirty = False
        return True

    def save_as(self, path, entries=None):
        """Write the tags (or some entries) to a file the user chose; refused in the evidence
        folder."""
        return write_json_atomic(path, self.to_data(entries), refuse_in=self.evidence_dir)

    def merge_file(self, path):
        """Merge a tag file or a JSON export into this store: returns (added, merged)."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return self._apply(data, merge=True)

    def merge_data(self, data):
        return self._apply(data, merge=True)

    def _apply(self, data, merge):
        if not isinstance(data, dict) or data.get("format") != FILE_FORMAT:
            raise TagError("not a tag file of this tool")
        if int(data.get("version") or 1) > FILE_VERSION:
            raise TagError("the tag file was written by a newer version (format %s)"
                           % data.get("version"))
        defs = [TagDef(d["name"], d.get("color")) for d in data.get("tag_defs") or ()
                if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"].strip()]
        entries = [TagEntry.from_dict(d) for d in data.get("entries") or ()]
        if not merge:
            if defs:
                seen = set()
                self.defs = [d for d in defs if not (d.name in seen or seen.add(d.name))]
            self._entries = OrderedDict()
            for e in entries:
                if e.tags:
                    if e.database is None:      # a file of a single database (before cases)
                        e.database = dict(self.database)
                    self._entries[e.key] = e
            state = data.get("state")
            self.state = state if isinstance(state, dict) else {}
            for e in self._entries.values():
                self._ensure_defs(e.tags)
                self._sort_tags(e)
            self._tables = None
            return len(self._entries), 0
        for d in defs:
            if self.def_of(d.name) is None:
                self.defs.append(d)
        added = merged = 0
        for e in entries:
            if not e.tags:
                continue
            self._ensure_defs(e.tags)
            cur = self._entries.get(e.key)
            if cur is None:
                if e.database is None:
                    e.database = dict(self.database)
                self._entries[e.key] = e
                self._sort_tags(e)
                added += 1
                continue
            before = (list(cur.tags), cur.note)
            for t in e.tags:
                if t not in cur.tags:
                    cur.tags.append(t)
            if e.note != cur.note and e.note:
                newer = (e.updated_at or "") > (cur.updated_at or "")
                same_time = (e.updated_at or "") == (cur.updated_at or "")
                if not cur.note or newer or (same_time and len(e.note) > len(cur.note)):
                    cur.note = e.note
            self._sort_tags(cur)
            if (cur.tags, cur.note) != before:
                cur.updated_at = max(cur.updated_at or "", e.updated_at or "")
                merged += 1
        if added or merged or defs:
            self._touch()
        return added, merged

    # -- evidence --------------------------------------------------------------
    def set_evidence(self, path, size=None, mtime_ns=None, sha256=None):
        """Record the evidence the tags now belong to (written with the next save); returns
        how it differs from what the loaded file was saved for ([] when it matches)."""
        cur = {"path": os.path.abspath(path), "size": size, "mtime_ns": mtime_ns,
               "sha256": sha256 or self.evidence.get("sha256")}
        self.evidence = cur
        old = self.database["identity"]
        self.database = database_info(path, size)
        for e in self._entries.values():   # entries migrated from a file without identities
            if e.database is None or e.database.get("identity") == old:
                e.database = dict(self.database)
        return self.evidence_differences(cur)

    def evidence_differences(self, current=None):
        """What changed in the database file since the loaded tags were saved."""
        saved, cur = self.saved_evidence, current or self.evidence
        if not saved:
            return []
        out = []
        for field, label in (("size", "size"), ("mtime_ns", "modification time"),
                             ("sha256", "SHA-256")):
            a, b = saved.get(field), cur.get(field)
            if a is not None and b is not None and a != b:
                out.append("%s %s -> %s" % (label, a, b) if field == "size" else
                           "%s changed" % label)
        return out

    # -- tag definitions -------------------------------------------------------
    def def_of(self, name):
        for d in self.defs:
            if d.name == name:
                return d
        return None

    def names(self):
        return [d.name for d in self.defs]

    def color_of(self, name):
        d = self.def_of(name)
        return d.color if d is not None else "#8993a4"

    def next_color(self):
        used = set(d.color.lower() for d in self.defs)
        free = [c for c in PALETTE if c.lower() not in used]
        return free[0] if free else PALETTE[len(self.defs) % len(PALETTE)]

    def add_def(self, name, color=None):
        name = (name or "").strip()
        if not name:
            raise TagError("a tag needs a name")
        if self.def_of(name) is not None:
            raise TagError("there is already a tag %r" % name)
        d = TagDef(name, color or self.next_color())
        self.defs.append(d)
        self._touch()
        return d

    def rename_def(self, old, new):
        new = (new or "").strip()
        d = self.def_of(old)
        if d is None:
            raise TagError("no tag %r" % old)
        if not new:
            raise TagError("a tag needs a name")
        if new != old and self.def_of(new) is not None:
            raise TagError("there is already a tag %r" % new)
        d.name = new
        for e in self._entries.values():
            e.tags = [new if t == old else t for t in e.tags]
        self._touch()

    def recolor_def(self, name, color):
        d = self.def_of(name)
        if d is None:
            raise TagError("no tag %r" % name)
        d.color = valid_color(color, d.color)
        self._touch()

    def delete_def(self, name):
        """Delete a tag: it leaves every entry, and entries left without a tag are removed.
        Returns the number of entries that had it."""
        d = self.def_of(name)
        if d is None:
            raise TagError("no tag %r" % name)
        self.defs.remove(d)
        n = self._strip_tag(list(self._entries), name)
        self._touch()
        return n

    def move_def(self, name, delta):
        """Move a tag up (-1) or down (+1): the order is the colour priority and Ctrl+1..9."""
        d = self.def_of(name)
        i = self.defs.index(d)
        j = max(0, min(len(self.defs) - 1, i + delta))
        if i != j:
            self.defs.insert(j, self.defs.pop(i))
            for e in self._entries.values():
                self._sort_tags(e)
            self._touch()

    def _ensure_defs(self, names):
        for n in names:
            if self.def_of(n) is None:
                self.defs.append(TagDef(n, self.next_color()))

    def _sort_tags(self, e):
        order = dict((d.name, i) for i, d in enumerate(self.defs))
        e.tags = sorted(dict.fromkeys(e.tags), key=lambda t: order.get(t, len(order)))

    # -- entries -----------------------------------------------------------------
    def __len__(self):
        return len(self._entries)

    def __contains__(self, key):
        return key in self._entries

    def get(self, key):
        return self._entries.get(key)

    def tags_of(self, key):
        e = self._entries.get(key)
        return list(e.tags) if e is not None else []

    def entries(self, tag=None):
        return [e for e in self._entries.values() if tag is None or tag in e.tags]

    def counts(self):
        """{tag: entries having it} for every definition (in definition order)."""
        out = OrderedDict((d.name, 0) for d in self.defs)
        for e in self._entries.values():
            for t in e.tags:
                out[t] = out.get(t, 0) + 1
        return out

    def has_table(self, table):
        """Whether any entry belongs to this table (fast: lets views skip untagged tables)."""
        if self._tables is None:
            tables = {}
            for e in self._entries.values():
                tables[e.table] = tables.get(e.table, 0) + 1
            self._tables = tables
        return table in self._tables

    def toggle(self, entry, tag):
        """Add tag to the row, or take it off when the row has it; True when it now has it."""
        cur = self._entries.get(entry.key)
        if cur is not None and tag in cur.tags:
            self.remove([entry.key], tag)
            return False
        self.add([entry], tag)
        return True

    def add(self, entries, tag):
        """Tag every row with `tag` (created when unknown); returns rows newly tagged."""
        self._ensure_defs([tag])
        now = utc_now()
        n = 0
        for e in entries:
            cur = self._entries.get(e.key)
            if cur is None:
                if tag not in e.tags:
                    e.tags.append(tag)
                e.tagged_at = e.updated_at = now
                if e.database is None:
                    e.database = dict(self.database)
                self._sort_tags(e)
                self._entries[e.key] = e
                n += 1
            elif tag not in cur.tags:
                cur.tags.append(tag)
                cur.updated_at = now
                self._sort_tags(cur)
                n += 1
        if n:
            self._touch()
        return n

    def set_tags(self, entries, tag, on):
        """Give every row the tag (on) or take it off; returns rows changed."""
        return self.add(entries, tag) if on else self.remove([e.key for e in entries], tag)

    def remove(self, keys, tag=None):
        """Take `tag` off these rows (every tag when None); rows left without a tag are
        dropped. Returns rows changed."""
        n = self._strip_tag(keys, tag)
        if n:
            self._touch()
        return n

    def _strip_tag(self, keys, tag):
        n = 0
        now = utc_now()
        for k in keys:
            cur = self._entries.get(k)
            if cur is None:
                continue
            if tag is None:
                del self._entries[k]
                n += 1
            elif tag in cur.tags:
                cur.tags.remove(tag)
                cur.updated_at = now
                n += 1
                if not cur.tags:
                    del self._entries[k]
        return n

    def set_note(self, key, note):
        cur = self._entries.get(key)
        if cur is None:
            raise TagError("the row is not tagged")
        note = note or ""
        if cur.note != note:
            cur.note = note
            cur.updated_at = utc_now()
            self._touch()

    def _touch(self):
        self.dirty = True
        self.revision += 1
        self._tables = None

    # -- saved view state --------------------------------------------------------
    def table_state(self, table):
        """Saved {"widths": {column: px}, "hidden": [columns], "sort": [column, desc]} of a
        table ({} when none, or when a hand-edited file holds something else)."""
        tables = self.state.get("tables")
        st = tables.get(table) if isinstance(tables, dict) else None
        if not isinstance(st, dict):
            return {}
        out = {}
        if isinstance(st.get("widths"), dict):
            out["widths"] = dict((str(k), int(w)) for k, w in st["widths"].items()
                                 if isinstance(w, (int, float)) and not isinstance(w, bool))
        if isinstance(st.get("hidden"), list):
            out["hidden"] = [str(c) for c in st["hidden"] if isinstance(c, str)]
        sort = st.get("sort")
        if isinstance(sort, list) and len(sort) == 2 and isinstance(sort[0], str):
            out["sort"] = [sort[0], bool(sort[1])]
        return out

    def set_table_state(self, table, st):
        tables = self.state.get("tables")
        if not isinstance(tables, dict):
            tables = self.state["tables"] = {}
        st = dict((k, v) for k, v in (st or {}).items() if v)
        if (tables.get(table) or {}) != st:
            if st:
                tables[table] = st
            else:
                tables.pop(table, None)
            self.dirty = True

    def section(self, name):
        """A copy of a named part of the saved state (a dict; {} when none, or when a
        hand-edited file holds something else there): e.g. 'timeline', 'date_formats'."""
        v = self.state.get(name)
        return copy.deepcopy(v) if isinstance(v, dict) else {}

    def set_section(self, name, value):
        """Replace a named part of the saved state (written with the next save)."""
        value = dict(value or {})
        if self.state.get(name) != value:
            if value:
                self.state[name] = copy.deepcopy(value)
            else:
                self.state.pop(name, None)
            self.dirty = True

    @property
    def last_table(self):
        t = self.state.get("last_table")
        return t if isinstance(t, str) else None

    @last_table.setter
    def last_table(self, table):
        if self.state.get("last_table") != table:
            self.state["last_table"] = table
            self.dirty = True


# -- app settings ------------------------------------------------------------------------------
def settings_path(directory=None):
    return os.path.join(directory or data_dir(), "settings.json")


def load_settings(directory=None):
    """App settings ({"recent": [paths]}); {} when there are none or they cannot be read."""
    try:
        with open(settings_path(directory), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings, directory=None, refuse_in=None):
    return write_json_atomic(settings_path(directory), settings, refuse_in=refuse_in)


def recent_max(settings):
    """How many recent databases/cases settings keeps (settings["recent_max"], 10)."""
    try:
        n = int((settings or {}).get("recent_max", RECENT_MAX))
    except (TypeError, ValueError):
        n = RECENT_MAX
    return min(max(n, 0), 50)


def add_recent(settings, path):
    """Put path first in settings['recent'] (at most recent_max(), no duplicates)."""
    path = os.path.abspath(path)
    norm = os.path.normcase(path)
    recent = [p for p in settings.get("recent") or () if isinstance(p, str)
              and os.path.normcase(p) != norm]
    settings["recent"] = [path] + recent[:recent_max(settings) - 1]
    return settings["recent"]


# -- cases: several databases examined together ---------------------------------------------
CASE_FORMAT = "sqlite-gui-analyzer-case"
CASE_VERSION = 1


def case_file_path(db_paths, directory=None):
    """case-lists/case-<n>db-<12 hex of sha1(the sorted normalised paths)>.json in the app-data
    folder: the same databases always give the same file."""
    norm = sorted(os.path.normcase(os.path.abspath(p)) for p in db_paths)
    digest = hashlib.sha1("\n".join(norm).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return os.path.join(directory or data_dir(), "case-lists",
                        "case-%ddb-%s.json" % (len(norm), digest))


def write_case(path, databases, active=None, state=None, refuse_in=()):
    """Save a case: databases [{"path", "size", "mtime_ns", "sha256", "color", "safe_parse"}],
    the active
    database's path and the case state (e.g. which databases a search covers). Refused
    (TagError) inside any evidence folder in refuse_in."""
    folder = inside_any(path, list(refuse_in or ()))
    if folder is not None:
        raise TagError("refusing to write %s: it is inside the evidence folder %s"
                       % (path, folder))
    data = {"format": CASE_FORMAT, "version": CASE_VERSION, "saved_utc": utc_now(),
            "databases": [dict((k, d.get(k)) for k in ("path", "name", "size", "mtime_ns",
                                                       "sha256", "color", "safe_parse"))
                          for d in databases],
            "active": active, "state": copy.deepcopy(state or {})}
    return write_json_atomic(path, data)


def read_case(path):
    """The case file's content (databases, active, state, problems); TagError when it is not
    one. Nothing listed in it is touched (no path is checked here). A database colour that is
    not '#rrggbb' is dropped (a default colour is used) and said in problems."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise TagError("cannot read the case file %s: %s" % (path, e))
    if not isinstance(data, dict) or data.get("format") != CASE_FORMAT:
        raise TagError("%s is not a case file of this tool" % path)
    dbs = [d for d in data.get("databases") or () if isinstance(d, dict)
           and isinstance(d.get("path"), str) and d["path"]]
    if not dbs:
        raise TagError("the case file %s lists no database" % path)
    problems = []
    for d in dbs:
        color = d.get("color")
        if color is not None and valid_color(color, None) is None:
            text = "%s: the colour %s in the case file is not #rrggbb; a default colour is " \
                   "used" % (d["path"], json.dumps(color, ensure_ascii=True)[:60])
            problems.append(text)
            d["color"] = None
            d["color_problem"] = text
    state = data.get("state")
    return {"databases": dbs, "active": data.get("active") if isinstance(data.get("active"), str)
            else None, "state": state if isinstance(state, dict) else {},
            "saved_utc": data.get("saved_utc") or "", "problems": problems}


def case_changes(saved):
    """How a database of a saved case differs from the file now: [] when its size and
    modification time are the same; else what differs ('missing', 'size 10 -> 12', ...).
    The SHA-256 is compared later, once the opened database is hashed (sha256_change)."""
    path = saved.get("path") or ""
    try:
        st = os.stat(path)
    except OSError:
        return ["missing"]
    out = []
    if saved.get("size") is not None and saved["size"] != st.st_size:
        out.append("size %s -> %s" % (format(saved["size"], ","), format(st.st_size, ",")))
    if saved.get("mtime_ns") is not None and saved["mtime_ns"] != st.st_mtime_ns:
        out.append("modification time changed")
    return out


def sha256_change(saved, sha256):
    """'SHA-256 changed' when the saved hash is known and differs, else ''."""
    old = saved.get("sha256") if isinstance(saved, dict) else None
    return "SHA-256 changed" if old and sha256 and old != sha256 else ""


def add_recent_case(settings, case_path, names):
    """Put a case first in settings['recent_cases'] ([{"path", "names"}], at most RECENT_MAX)."""
    case_path = os.path.abspath(case_path)
    norm = os.path.normcase(case_path)
    recent = [c for c in settings.get("recent_cases") or () if isinstance(c, dict)
              and isinstance(c.get("path"), str) and os.path.normcase(c["path"]) != norm]
    settings["recent_cases"] = [{"path": case_path, "names": list(names)}] + \
        recent[:recent_max(settings) - 1]
    return settings["recent_cases"]
