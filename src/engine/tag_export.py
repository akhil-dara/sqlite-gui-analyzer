"""Exports of tagged rows: a self-contained HTML report, a folder of CSV files, or JSON.

Every export names the tool and its version, the export time (UTC) and the evidence files the
rows come from (path, size, modification time and SHA-256 of the database and of its WAL /
journal sidecars), and for each row its tags, note, source, table, row id, where it was found
(provenance), when it was tagged, and its values as they were when tagged (the snapshot kept
with the tag).

- HTML: the tool's one report design (engine.html_report): one file with no external
  resources (a Content-Security-Policy forbids loading any), every value escaped, printable.
  Rows are grouped by tag, then table (a row with several tags appears under each, with all
  its tags shown), or by table; each group is a searchable, sortable table with row details.
  BLOBs show their decoded summary and size; PNG / JPEG / GIF / WEBP images up to limit
  html_thumb_bytes are shown as thumbnails.
- CSV: a folder holding index.csv (one line per row), one <table>.csv per table with that
  table's columns, export_info.csv (tool, time, evidence hashes) and blobs/ with each BLOB as a
  file, referenced from its cell by relative path, size and summary. UTF-8 with a BOM so
  spreadsheets detect the encoding; text beginning with = + - @, a tab or a carriage return
  gets a leading apostrophe so a spreadsheet does not run it as a formula, and NUL is written
  as \\x00 (engine.csvcells, the rule every CSV of the tool follows). Existing files are never
  replaced (a _2 suffix is added instead); a file that fails to write is removed.
- JSON: the tag-file format plus the export details; Load tags reads it back.

Nothing is written inside the evidence folder: each function takes `protected` (path -> True
when writing there would touch the evidence folder) and raises TagExportError for such a target.
"""

import base64
import contextlib
import datetime
import hashlib
import html
import json
import os
from collections import OrderedDict

from .csvcells import FORMULA_PREFIXES, csv_text, csv_writer  # noqa: F401 (FORMULA_PREFIXES: API)
from .decode import summary as blob_summary
from .evidence import _sha256
from .fileformat.record import InvalidText
from .tags import (FILE_FORMAT, FILE_VERSION, PartialBlob, safe_name, utc_now, valid_color,
                   write_json_atomic)

TOOL_NAME = "SQLite GUI Analyzer"
THUMB_CAP = 512 << 10          # images up to this size are embedded in the HTML report
HTML_TEXT_CAP = 10000          # characters of one text value shown in the HTML report
PROVENANCE_ORDER = ("frame", "frame_state", "page", "cell_offset", "offset", "file", "source",
                    "confidence", "frames", "wal_frames", "flags", "reasons")

_IMAGE_MIME = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
               (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))
_EXT_SIGS = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"),
             (b"bplist", ".plist"), (b"<?xml", ".xml"), (b"%PDF", ".pdf"),
             (b"PK\x03\x04", ".zip"), (b"\x1f\x8b", ".gz"), (b"SQLite format 3\x00", ".sqlite"))


class TagExportError(Exception):
    """The export cannot be written where asked (e.g. inside the evidence folder)."""


# -- export details --------------------------------------------------------------------------
def evidence_files(evidence, wait=None):
    """Fingerprints of an engine EvidenceSet's files ({role, path, size, mtime_ns, sha256}):
    waits up to `wait` seconds (None: until done) for the background hashing, then hashes any
    file it did not finish. Call it on a worker thread: hashing a large file takes a while."""
    if not evidence.hashing_done and getattr(evidence, "_thread", None) is not None:
        evidence.wait_hashing(wait)
    out = []
    for fp in evidence.fingerprints.values():
        d = fp.as_dict()
        if not d["sha256"]:
            try:
                d["sha256"] = _sha256(fp.path)
            except OSError as e:
                d["sha256"] = None
                d["error"] = str(e)
        out.append(d)
    return out


def export_info(version, evidence, database=None, scope=""):
    """What every export states about itself: tool, UTC time, evidence files, scope."""
    evidence = list(evidence or ())
    main = next((e for e in evidence if e.get("role") == "main"), {})
    return {"tool": {"name": TOOL_NAME, "version": str(version)}, "exported_utc": utc_now(),
            "database": database or main.get("path"), "evidence": evidence, "scope": scope}


def _check(protected, *paths):
    for p in paths:
        if not p:
            raise TagExportError("no export location given")
        if protected is not None and protected(p):
            raise TagExportError("refusing to write %s: it is inside the evidence folder" % p)


def _used_defs(entries, defs):
    used = set(t for e in entries for t in e.tags)
    out = [d for d in defs if d.name in used]
    known = set(d.name for d in out)
    for e in entries:                       # a tag without a definition still gets a colour
        for t in e.tags:
            if t not in known:
                known.add(t)
                out.append(_Def(t, "#8993a4"))
    return out


class _Def(object):
    __slots__ = ("name", "color")

    def __init__(self, name, color):
        self.name, self.color = name, color

    def as_dict(self):
        return {"name": self.name, "color": self.color}


def provenance_text(entry):
    """Where a tagged row was found, in a few words (e.g. 'WAL frame 12 (superseded), page 7')."""
    p = entry.provenance or {}
    parts = []
    if p.get("index"):
        parts.append("index entry of %s" % p["index"])
    if p.get("frame") is not None:
        parts.append("WAL frame %s%s" % (p["frame"], " (%s)" % p["frame_state"]
                                         if p.get("frame_state") else ""))
    elif p.get("frame_state"):
        parts.append(str(p["frame_state"]))
    if len(p.get("frames") or ()) > 1:
        parts.append("same version in %d frames" % len(p["frames"]))
    if p.get("wal_frames"):
        parts.append("also in %d WAL frame(s)" % len(p["wal_frames"]))
    if p.get("source") and entry.source not in ("DB", "WAL"):
        parts.append(str(p["source"]))
    if p.get("file") and p.get("file") != "main":
        parts.append("%s file" % p["file"])
    if p.get("page") is not None:
        off = p.get("cell_offset", p.get("offset"))
        parts.append("page %s%s" % (p["page"], " @ %s" % off if off is not None else ""))
    if p.get("confidence"):
        parts.append("confidence %s" % p["confidence"])
    if p.get("flags"):
        parts.append("flags: %s" % ", ".join(str(f) for f in p["flags"]))
    return ", ".join(parts)


def _group(entries, key):
    out = OrderedDict()
    for e in entries:
        out.setdefault(key(e), []).append(e)
    return out


def _columns_of(entries):
    cols = []
    seen = set()
    for e in entries:
        for c in e.columns:
            if c not in seen:
                seen.add(c)
                cols.append(c)
    return cols


def _mtime_text(mtime_ns):
    try:
        t = datetime.datetime.fromtimestamp(mtime_ns / 1e9, datetime.timezone.utc)
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def image_mime(data):
    for sig, mime in _IMAGE_MIME:
        if data[:len(sig)] == sig:
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def blob_extension(data):
    for sig, ext in _EXT_SIGS:
        if data[:len(sig)] == sig:
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def blob_text(v):
    """One line for a BLOB: its decoded summary and size (and, for one kept in part, that and
    its SHA-256)."""
    if isinstance(v, PartialBlob):
        return "%s (%s bytes; first %s kept; SHA-256 %s)" % (
            blob_summary(bytes(v)), format(v.size, ","), format(len(v), ","), v.sha256)
    return "%s (%s bytes)" % (blob_summary(v), format(len(v), ","))


# -- HTML ------------------------------------------------------------------------------------
def _e(v):
    return html.escape("" if v is None else str(v), quote=True)


def html_value(v):
    """HTML for one snapshot value (escaped; an image BLOB also as a small embedded picture)."""
    if v is None:
        return '<span class="null">NULL</span>'
    if isinstance(v, InvalidText):
        raw = bytes(v)
        return '<span class="bad">invalid text (%d bytes): %s</span>' % (
            len(raw), _e(raw.decode("utf-8", "backslashreplace")[:HTML_TEXT_CAP]))
    if isinstance(v, bytes):
        out = '<span class="blob">%s</span>' % _e(blob_text(v))
        mime = image_mime(v) if not isinstance(v, PartialBlob) else None
        if mime and len(v) <= THUMB_CAP:
            out += '<img class="thumb" alt="image" src="data:%s;base64,%s">' % (
                mime, base64.b64encode(v).decode("ascii"))
        return out
    if isinstance(v, float):
        return _e(repr(v))
    s = str(v)
    if len(s) > HTML_TEXT_CAP:
        s = s[:HTML_TEXT_CAP] + " … (%s characters)" % format(len(s), ",")
    return _e(s)


ENTRY_FIELDS = ("Row", "Source", "Where", "Tags", "Note", "Tagged at")


def _entry_rows(entries, columns):
    """The rows of one table of the report: the entry's fields, then its snapshot values
    (a column the snapshot does not have: MISSING, shown empty)."""
    from .html_report import MISSING
    for e in entries:
        values = dict(zip(e.columns, e.row_values()))
        yield [e.rowid, e.source, provenance_text(e), list(e.tags), e.note, e.tagged_at] + [
            values[c] if c in values else MISSING for c in columns]


def build_report(entries, defs, info, layout="tag", title=None, limits=None):
    """The HTML report as an engine.html_report.Report (write() it, or render() it).
    layout 'tag': a section per tag, then a table per database table; 'table': a section per
    table."""
    from .html_report import Markup, Report, chip
    entries = list(entries)
    defs = _used_defs(entries, defs)
    colors = OrderedDict((d.name, valid_color(d.color)) for d in defs)
    db = info.get("database") or ""
    title = title or "Tagged rows: %s" % os.path.basename(db)
    tool = info.get("tool") or {}
    details = [("Database", db)] if db else []
    if info.get("scope"):
        details.append(("Scope", info["scope"]))
    rep = Report(title, kind="Tagged rows", tool=(tool.get("name", TOOL_NAME),
                                                   tool.get("version", "")),
                 exported_utc=info.get("exported_utc", ""), case_name=info.get("case", ""),
                 evidence=[("", ev) for ev in info.get("evidence") or ()], details=details,
                 limits=limits, auto_summary=layout == "table")
    counts = OrderedDict((d.name, 0) for d in defs)
    for e in entries:
        for t in e.tags:
            counts[t] = counts.get(t, 0) + 1
    rep.add_summary_cards([
        ("Tagged rows", format(len(entries), ","), "in %s" % _plural(
            len(set(e.table for e in entries)), "table"), None),
        ("Tags", format(len(defs), ","), None, None)])
    rep.add_section("Tags", id="tags")
    ids = [rep.uid("tag-%d" % i, "tag") for i in range(len(defs))] if layout == "tag" else []
    items = []
    for i, d in enumerate(defs):
        pill = chip(d.name, d.color)
        link = '<a href="#%s">%s</a>' % (ids[i], pill) if layout == "tag" else pill
        items.append("<li>%s %s</li>" % (link, _e(_plural(counts.get(d.name, 0), "row"))))
    rep.add_html('<ul class="legend">%s</ul>' % "".join(items) if items else
                 '<p class="muted">No tagged rows.</p>')
    base = os.path.basename(db)
    detail = {"prov": ["Source", "Where", "Tagged at"],
              "path": [base or "database", None, {"col": "Row"}]}
    if not entries:
        return rep

    def add(table, group):
        columns = _columns_of(group)
        d = dict(detail, path=[base or "database", table, {"col": "Row"}])
        rep.add_table(table, list(ENTRY_FIELDS) + columns, _entry_rows(group, columns),
                      badges={"Source": {}}, detail=d, tags=("Tags", colors), notes="Note",
                      dates={"Tagged at": "iso_text"}, title="%s" % table)
    if layout == "table":
        for table, group in sorted(_group(entries, lambda e: e.table).items()):
            rep.add_section(table)
            add(table, group)
        return rep
    for i, d in enumerate(defs):
        tagged = [e for e in entries if d.name in e.tags]
        if not tagged:
            continue
        # chip() and _e() already escape: mark the concatenation trusted explicitly.
        rep.add_section(d.name, id=ids[i], title_html=Markup("%s %s" % (
            chip(d.name, d.color), _e(_plural(len(tagged), "row")))))
        for table, group in sorted(_group(tagged, lambda e: e.table).items()):
            add(table, group)
    return rep


def _plural(n, word):
    return "%s %s%s" % (format(n, ","), word, "" if n == 1 else "s")


def html_report(entries, defs, info, layout="tag", title=None):
    """The HTML report text. layout 'tag': a section per tag, then per table; 'table': a
    section per table."""
    return build_report(entries, defs, info, layout, title).render()


def export_html(path, entries, defs, info, layout="tag", protected=None, title=None):
    """Write the HTML report to path (streamed; a very large group continues in part files
    next to it, listed in the report); returns the path."""
    from .html_report import ReportError
    _check(protected, path)
    try:
        build_report(entries, defs, info, layout, title).write(path, protected)
    except ReportError as e:
        raise TagExportError(str(e))
    return path


# -- JSON ------------------------------------------------------------------------------------
def export_json(path, entries, defs, info, protected=None):
    """Write the entries in the tag-file format (Load tags merges it back) plus the export
    details; returns the path."""
    _check(protected, path)
    entries = list(entries)
    main = next((e for e in info.get("evidence") or () if e.get("role") == "main"), {})
    data = {"format": FILE_FORMAT, "version": FILE_VERSION,
            "saved_utc": info.get("exported_utc"), "exported_utc": info.get("exported_utc"),
            "tool": info.get("tool"), "scope": info.get("scope", ""),
            "evidence": dict((k, main.get(k)) for k in ("path", "size", "mtime_ns", "sha256")),
            "evidence_files": list(info.get("evidence") or ()),
            "tag_defs": [d.as_dict() for d in _used_defs(entries, defs)],
            "entries": [e.as_dict() for e in entries], "state": {}}
    return write_json_atomic(path, data)


# -- CSV -------------------------------------------------------------------------------------
def _csv_text(s):
    """Text as written in a cell: spreadsheet-safe, NUL as \\x00 (engine.csvcells)."""
    return csv_text(s, True)


def _csv_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, InvalidText):
        return _csv_text(bytes(v).decode("utf-8", "backslashreplace"))
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (list, dict)):
        return _csv_text(json.dumps(v, ensure_ascii=False, default=str))
    if isinstance(v, str):
        return _csv_text(v)
    return str(v)


def _new_file(folder, name, binary=False):
    """Open folder/name for writing without ever replacing a file: when the name is taken,
    _2, _3, ... goes before the extension. Returns (file, path)."""
    base, ext = os.path.splitext(name)
    for n in range(1, 10000):
        path = os.path.join(folder, name if n == 1 else "%s_%d%s" % (base, n, ext))
        try:
            if binary:
                return open(path, "xb"), path
            return open(path, "x", encoding="utf-8-sig", newline=""), path
        except FileExistsError:
            continue
    raise OSError("no free file name for %s in %s" % (name, folder))


@contextlib.contextmanager
def _csv_file(folder, name):
    """(writer, path) of a new CSV file in folder (never replacing one; cells NUL-safe, text
    made spreadsheet-safe by the caller). A failed write removes the partly written file and
    raises TagExportError saying so."""
    f, path = _new_file(folder, name)
    try:
        with f:
            yield csv_writer(f, formulas=False), path
    except OSError as e:
        try:
            os.remove(path)
            what = "the partly written file was removed"
        except OSError as e2:
            what = "the partly written file is INCOMPLETE and could not be removed: %s" % e2
        raise TagExportError("cannot write %s: %s (%s)" % (path, e, what))


def _blob_name(entry, column, ext):
    """Readable, unique name: table, row and column, plus a hash of the row's key (two rows
    whose names fold to the same text still get different files)."""
    digest = hashlib.sha256(entry.key.encode("utf-8", "surrogatepass")).hexdigest()[:10]
    return "%s_r%s_%s_%s%s" % (safe_name(entry.table, 40), safe_name(entry.rowid or "x", 40),
                               digest, safe_name(column, 40), ext)


def export_csv(folder, entries, defs, info, protected=None, blob_loader=None):
    """Write the CSV folder (see the module docstring). blob_loader(entry, column index) may
    return the whole value of a BLOB the snapshot kept in part (it is used only when its
    SHA-256 matches). Returns {index, info, tables: {table: path}, blobs, blob_errors, rows}."""
    folder = os.path.abspath(folder or "")
    _check(protected, folder, os.path.join(folder, "index.csv"))
    entries = list(entries)
    os.makedirs(folder, exist_ok=True)
    blob_dir = os.path.join(folder, "blobs")
    written, errors = [0], []
    number = dict((id(e), i + 1) for i, e in enumerate(entries))
    files = OrderedDict()

    def blob_cell(e, ci, column, v):
        whole = v
        if isinstance(v, PartialBlob) and blob_loader is not None:
            try:
                data = blob_loader(e, ci)
            except Exception:           # noqa: BLE001 - the part kept is written instead
                data = None
            if data is not None and hashlib.sha256(bytes(data)).hexdigest() == v.sha256:
                whole = bytes(data)
        partial = isinstance(whole, PartialBlob)
        ext = blob_extension(bytes(whole))
        name = _blob_name(e, column, ("_first%dKiB" % (len(whole) // 1024) if partial else "")
                          + ext)
        try:
            os.makedirs(blob_dir, exist_ok=True)
            f, path = _new_file(blob_dir, name, binary=True)
            with f:
                f.write(bytes(whole))
            written[0] += 1
            rel = "blobs/" + os.path.basename(path)
        except OSError as ex:
            errors.append("%s row %s column %s: %s" % (e.table, e.rowid, column, ex))
            rel = "(not written: %s)" % ex
        size = whole.size if partial else len(whole)
        return _csv_text("%s (%s bytes; %s)" % (rel, format(size, ","), blob_text(whole)
                                                if partial else blob_summary(bytes(whole))))

    for table, group in _group(entries, lambda e: e.table).items():
        columns = _columns_of(group)
        with _csv_file(folder, safe_name(table or "table") + ".csv") as (w, path):
            files[table] = path
            w.writerow(["#", "Row", "Source", "Tags", "Note", "Tagged at"] +
                       [_csv_text(c) for c in columns])
            for e in group:
                pos = dict((c, i) for i, c in enumerate(e.columns))
                values = e.row_values()
                row = [number[id(e)], _csv_text(e.rowid), e.source, _csv_text("; ".join(e.tags)),
                       _csv_text(e.note), e.tagged_at]
                for c in columns:
                    i = pos.get(c)
                    if i is None or i >= len(values):
                        row.append("")
                    elif isinstance(values[i], bytes) and not isinstance(values[i], InvalidText):
                        row.append(blob_cell(e, i, c, values[i]) if values[i]
                                   else "(empty BLOB, 0 bytes)")
                    else:
                        row.append(_csv_value(values[i]))
                w.writerow(row)

    prov_keys = []
    for e in entries:
        for k in e.provenance:
            if k not in prov_keys:
                prov_keys.append(k)
    rank = dict((k, i) for i, k in enumerate(PROVENANCE_ORDER))
    prov_keys.sort(key=lambda k: (rank.get(k, len(rank)), k))
    with _csv_file(folder, "index.csv") as (w, index_path):
        w.writerow(["#", "Tags", "Note", "Source", "Table", "Row", "Tagged at", "Updated at",
                    "File"] + prov_keys + ["Key"])
        for e in entries:
            prov = [_csv_value(e.provenance.get(k)) if e.provenance.get(k) is not None else ""
                    for k in prov_keys]
            w.writerow([number[id(e)], _csv_text("; ".join(e.tags)), _csv_text(e.note), e.source,
                        _csv_text(e.table), _csv_text(e.rowid), e.tagged_at, e.updated_at,
                        _csv_text(os.path.basename(files[e.table]))] + prov + [_csv_text(e.key)])
    with _csv_file(folder, "export_info.csv") as (w, info_path):
        tool = info.get("tool") or {}
        w.writerow(["field", "value"])
        w.writerow(["tool", _csv_text("%s %s" % (tool.get("name", TOOL_NAME), tool.get("version", "")))])
        w.writerow(["exported_utc", _csv_text(info.get("exported_utc", ""))])
        w.writerow(["database", _csv_text(info.get("database") or "")])
        w.writerow(["scope", _csv_text(info.get("scope") or "")])
        w.writerow(["rows", len(entries)])
        w.writerow(["tags", _csv_text("; ".join(d.name for d in _used_defs(entries, defs)))])
        w.writerow([])
        w.writerow(["file", "path", "size", "mtime_ns", "modified_utc", "sha256"])
        for ev in info.get("evidence") or ():
            w.writerow([_csv_text(ev.get("role") or ""), _csv_text(ev.get("path") or ""), ev.get("size"), ev.get("mtime_ns"),
                        _mtime_text(ev.get("mtime_ns")), ev.get("sha256") or ""])
    return {"index": index_path, "info": info_path, "tables": dict(files), "blobs": written[0],
            "blob_errors": errors, "rows": len(entries)}
