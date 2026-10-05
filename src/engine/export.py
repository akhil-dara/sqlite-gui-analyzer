"""One writer for every data export of the tool (CSV, JSON or HTML), with its provenance.

Every export states where its rows come from: the tool and its version, the export time
(UTC), the Python and SQLite versions, each database's evidence files (path, size,
modification time, SHA-256), what was exported (source, scope, filters, columns), how many rows
were written, whether the export is complete, and how values are written (VALUE_ENCODING).

- CSV: the header row and the rows, nothing else (a spreadsheet opens it cleanly); the
  provenance goes into a manifest next to it.
- JSON: {"format", "version", "provenance", "columns", "rows", "end"}, written as the rows come
  (millions of rows never sit in memory); "end" says how many rows were written and whether the
  export is complete, since that is only known at the end.
- HTML: the tool's one report design (engine.html_report): the provenance as a cover, the rows
  embedded as data chunks while they stream and shown by a searchable, sortable table; a
  table larger than limit html_rows_per_part continues in part files next to it (listed in
  the page and in the manifest).

After writing, the export file's SHA-256 and size are computed and a manifest
<file name>.manifest.json is written next to it (never replacing a file: _2, _3 ... is added).
A stopped export is still a valid file, marked incomplete in "end" and in the manifest.
Nothing is written inside the evidence folder: `protected` (path -> True there) makes the
writer raise ExportError before any file is created.

No Tk here: the UI drives it on a worker thread.
"""

import base64
import csv
import datetime
import hashlib
import json
import math
import os
import platform
import sqlite3
import time
from collections import OrderedDict

from .csvcells import FORMULA_NOTE, NUL_NOTE, RAW_NOTE, csv_text, csv_writer
from .decode import summary as blob_summary
from .evidence import CHUNK
from .fileformat.record import InvalidText
from .schema import Locator
from .tags import PartialBlob

TOOL_NAME = "SQLite GUI Analyzer"
EXPORT_FORMAT = "sqlite-gui-analyzer-export"
MANIFEST_FORMAT = "sqlite-gui-analyzer-export-manifest"
EXPORT_VERSION = 1
FORMATS = ("csv", "json")
BLOB_MODES = ("hex", "base64", "summary")
BLOB_LOSSLESS = {"hex": True, "base64": True, "summary": False}
FOLDER_MANIFEST = "export_manifest.json"

VALUE_ENCODING = OrderedDict([
    ("csv", OrderedDict([
        ("file", "UTF-8 with a byte order mark, one header row with the column names"),
        ("NULL", "the word NULL (a text value 'NULL' looks the same in CSV: use JSON to tell "
                 "them apart)"),
        ("INTEGER", "decimal digits"),
        ("REAL", "Python repr(): round-trips exactly (inf, -inf and nan as those words)"),
        ("TEXT", "as it is, no truncation; %s; %s (a text that reads like a BLOB, x'00ff' "
                 "or base64:..., looks the same as that BLOB in CSV: use JSON to tell them "
                 "apart)" % (FORMULA_NOTE, NUL_NOTE)),
        ("invalid text", "text whose bytes are not valid UTF-8: the valid parts as text, each "
                         "invalid byte as \\xNN (then as TEXT)"),
        ("BLOB", OrderedDict([
            ("hex", "x'<every byte as hex>' (lossless)"),
            ("base64", "base64:<every byte as base64> (lossless)"),
            ("summary", "[BLOB <n> bytes, SHA-256 <hex>] (not lossless: the bytes are not in "
                        "the export)")])),
        ("several values", "a cell holding several values (a record's values, a list of WAL "
                           "frames) is the JSON text of them, each written as in a JSON "
                           "export"),
    ])),
    ("json", OrderedDict([
        ("NULL", "null"),
        ("INTEGER", "a number"),
        ("REAL", "a number; one JSON cannot hold as {\"real\": \"inf\" / \"-inf\" / \"nan\"}"),
        ("TEXT", "a string, as it is"),
        ("invalid text", "{\"invalid_text_hex\": <the bytes as hex>}"),
        ("BLOB", OrderedDict([
            ("hex", "{\"blob_hex\": <every byte as hex>, \"size\": n} (lossless)"),
            ("base64", "{\"blob_base64\": <every byte as base64>, \"size\": n} (lossless)"),
            ("summary", "{\"blob_summary\": <decoded summary>, \"size\": n, \"sha256\": <hex>} "
                        "(not lossless)")])),
        ("partial BLOB", "a BLOB kept only in part (a tag snapshot of a large value) adds "
                         "\"partial\": {\"bytes_kept\", \"sha256\"}; size is the whole value's"),
        ("several values", "a list (or object) of values, each written as above"),
    ])),
])


def value_encoding(spreadsheet_safe=True):
    """VALUE_ENCODING as it applies to an export: with spreadsheet_safe False, CSV text is
    written without the ' added in front of formula-like text."""
    if spreadsheet_safe:
        return VALUE_ENCODING
    enc = json.loads(json.dumps(VALUE_ENCODING), object_pairs_hook=OrderedDict)
    enc["csv"]["TEXT"] = enc["csv"]["TEXT"].replace(FORMULA_NOTE, RAW_NOTE)
    return enc


class ExportError(Exception):
    """The export cannot be written where asked (e.g. inside the evidence folder) or failed."""


def utc_text(t=None):
    """'YYYY-MM-DD HH:MM:SS UTC' for epoch seconds t (default: now)."""
    if t is None:
        d = datetime.datetime.now(datetime.timezone.utc)
    else:
        d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M:%S UTC")


def mtime_utc(mtime_ns):
    """'YYYY-MM-DD HH:MM:SS UTC' of a file time in nanoseconds ('' when unreadable)."""
    try:
        return utc_text(mtime_ns / 1e9)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


_mtime_utc = mtime_utc


def _check_blob_mode(blob_mode):
    if blob_mode not in BLOB_MODES:
        raise ExportError("unknown BLOB mode %r (one of %s)" % (blob_mode, ", ".join(BLOB_MODES)))


# -- cells -----------------------------------------------------------------------------------
def _partial(v):
    return isinstance(v, PartialBlob) and v.size != len(v)


def csv_cell(v, blob_mode="hex", formulas=True):
    """The text one value is written as in a CSV export (see VALUE_ENCODING). Text has NUL
    written as \\x00 and, with formulas (spreadsheet-safe, the default), a ' in front when
    it starts with = + - @, a tab or a carriage return (engine.csvcells); numbers never."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, InvalidText):
        return csv_text(bytes(v).decode("utf-8", "backslashreplace"), formulas)
    if isinstance(v, str):
        return csv_text(v, formulas)
    if isinstance(v, (bytes, bytearray, memoryview)):
        raw = bytes(v)
        if blob_mode == "summary":
            size = v.size if _partial(v) else len(raw)
            digest = v.sha256 if _partial(v) else hashlib.sha256(raw).hexdigest()
            return "[BLOB %d bytes, SHA-256 %s]" % (size, digest)
        body = ("base64:" + base64.b64encode(raw).decode("ascii") if blob_mode == "base64"
                else "x'%s'" % raw.hex())
        if _partial(v):
            body += " [first %d of %d bytes, SHA-256 of all %s]" % (len(raw), v.size, v.sha256)
        return body
    if isinstance(v, Locator):
        return csv_text(v.display(), formulas)
    if isinstance(v, (list, tuple, dict)):
        # several values in one cell (a record's values, a WAL frame list): as the JSON of
        # their JSON cells, so BLOBs, NULL and invalid text keep their encoding
        return csv_text(json.dumps(json_cell(v, blob_mode), ensure_ascii=False), formulas)
    return csv_text(str(v), formulas)


def json_cell(v, blob_mode="hex"):
    """The JSON value one value is written as in a JSON export (see VALUE_ENCODING)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else {"real": repr(v)}
    if isinstance(v, InvalidText):
        return {"invalid_text_hex": bytes(v).hex()}
    if isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        raw = bytes(v)
        size = v.size if _partial(v) else len(raw)
        if blob_mode == "summary":
            out = OrderedDict([("blob_summary", blob_summary(raw)), ("size", size),
                               ("sha256", v.sha256 if _partial(v)
                                else hashlib.sha256(raw).hexdigest())])
        elif blob_mode == "base64":
            out = OrderedDict([("blob_base64", base64.b64encode(raw).decode("ascii")),
                               ("size", size)])
        else:
            out = OrderedDict([("blob_hex", raw.hex()), ("size", size)])
        if _partial(v):
            out["partial"] = {"bytes_kept": len(raw), "sha256": v.sha256}
        return out
    if isinstance(v, Locator):
        return v.display()
    if isinstance(v, (list, tuple)):
        return [json_cell(x, blob_mode) for x in v]
    if isinstance(v, dict):
        return OrderedDict((str(k), json_cell(x, blob_mode)) for k, x in v.items())
    return str(v)


# -- provenance ------------------------------------------------------------------------------
def _hash_file(path, cancel=None, progress=None):
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel():
                return None
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
            done += len(block)
            if progress is not None:
                progress(done)
    return h.hexdigest()


def evidence_record(evidence, wait=True, cancel=None, progress=None):
    """The evidence files of an engine EvidenceSet as [{role, path, size, mtime_ns, mtime_utc,
    sha256}] (size and time as when the database was opened). With wait, it waits for the
    set's background hashing (calling progress(hashed_bytes, total_bytes)) and then hashes
    any file still without a SHA-256 itself (chunked). cancel() true stops both: files not
    hashed by then get sha256 None and "note": "not computed: stopped". Without wait, only
    the hashes known now are given (the others: "note": "not computed yet"). A file that
    cannot be read gets "error". Reads only; call it on a worker thread."""
    if evidence is None:
        return []
    fps = list(evidence.fingerprints.values())
    total = sum(fp.size for fp in fps)
    stopped = False
    if wait:
        thread = getattr(evidence, "_thread", None)
        while thread is not None and thread.is_alive() and not evidence.hashing_done:
            if cancel is not None and cancel():
                stopped = True
                break
            if progress is not None:
                progress(min(evidence.hashed_bytes, total), total)
            thread.join(0.1)
    out = []
    done = sum(fp.size for fp in fps if fp.sha256)
    for fp in fps:
        d = OrderedDict([("role", fp.role), ("path", fp.path), ("size", fp.size),
                         ("mtime_ns", fp.mtime_ns), ("mtime_utc", _mtime_utc(fp.mtime_ns)),
                         ("sha256", fp.sha256)])
        if not d["sha256"]:
            if not wait:
                d["note"] = "not computed yet"
            elif stopped or (cancel is not None and cancel()):
                stopped = True
                d["note"] = "not computed: stopped"
            else:
                base = done
                report = (None if progress is None
                          else (lambda n, base=base: progress(base + n, total)))
                try:
                    digest = _hash_file(fp.path, cancel, report)
                except OSError as e:
                    digest = None
                    d["error"] = str(e)
                if digest is None and "error" not in d:
                    stopped = True
                    d["note"] = "not computed: stopped"
                elif digest is not None:
                    d["sha256"] = digest
                    done += fp.size
        out.append(d)
    if progress is not None and not stopped:
        progress(done, total)
    return out


def provenance(version, databases, source, scope="", filters="", columns=None, rows=None,
               blob_mode="hex", extra=None, spreadsheet_safe=True):
    """What an export states about itself. databases: [(label, files)] with files from
    evidence_record(); source: what was exported, e.g. "Browse table 'urls' (DB, WAL
    applied)"; scope and filters in words; columns: the column names; rows: how many were
    written (the writer fills it in, with complete). spreadsheet_safe: a CSV export adds a '
    in front of formula-like text (engine.csvcells; stated in value_encoding)."""
    _check_blob_mode(blob_mode)
    info = OrderedDict()
    info["tool"] = OrderedDict([("name", TOOL_NAME), ("version", str(version))])
    info["exported_utc"] = utc_text()
    info["python"] = platform.python_version()
    info["sqlite"] = sqlite3.sqlite_version
    info["databases"] = [OrderedDict([("label", label), ("files", list(files or ()))])
                         for label, files in (databases or ())]
    info["source"] = source
    info["scope"] = scope
    info["filters"] = filters
    if columns is not None:
        info["columns"] = [str(c) for c in columns]
    info["rows"] = rows
    info["blob_mode"] = blob_mode
    info["blob_lossless"] = BLOB_LOSSLESS[blob_mode]
    info["spreadsheet_safe"] = bool(spreadsheet_safe)
    info["value_encoding"] = value_encoding(spreadsheet_safe)
    if extra:
        info["extra"] = OrderedDict(extra)
    info["complete"] = None
    return info


# -- manifest --------------------------------------------------------------------------------
def _refuse(protected, path):
    if not path:
        raise ExportError("no export location given")
    if protected is not None and protected(path):
        raise ExportError("refusing to write %s: it is inside the evidence folder" % path)


def _manifest_candidates(target):
    if os.path.isdir(target):
        stem, tail = os.path.join(target, FOLDER_MANIFEST[:-len(".json")]), ".json"
    else:
        stem, tail = target, ".manifest.json"
    yield stem + tail
    n = 2
    while True:
        yield "%s_%d%s" % (stem, n, tail)
        n += 1


def _file_entry(f, base):
    if isinstance(f, dict):
        d = OrderedDict(f)
        p = d.get("path")
    else:
        p = f
        d = OrderedDict([("path", p)])
    if p and ("size" not in d or "sha256" not in d):
        d["size"] = os.path.getsize(p)
        d["sha256"] = _hash_file(p)
    if p and os.path.isabs(p):
        try:
            rel = os.path.relpath(p, base)
        except ValueError:          # another drive
            rel = None
        if rel and rel != os.pardir and not rel.startswith(os.pardir + os.sep):
            d["path"] = rel.replace(os.sep, "/")
    return d


def write_manifest(target, info, files, complete=True, protected=None):
    """Write the manifest of an export and return its path. target: the export file (the
    manifest goes next to it as <name>.manifest.json) or a folder of exported files (inside
    it as export_manifest.json). An existing file is never replaced: _2, _3 ... is added
    before the extension. files: dicts {path, size, sha256} or paths (size and SHA-256 are
    then computed); paths inside the manifest's folder are written relative to it."""
    target = os.path.abspath(target)
    first = next(_manifest_candidates(target))
    _refuse(protected, first)
    base = os.path.dirname(first)
    prov = OrderedDict(info or ())
    prov["complete"] = bool(complete)
    data = OrderedDict([("format", MANIFEST_FORMAT), ("version", EXPORT_VERSION),
                        ("provenance", prov),
                        ("files", [_file_entry(f, base) for f in (files or ())])])
    text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    for path in _manifest_candidates(target):
        _refuse(protected, path)
        try:
            with open(path, "x", encoding="utf-8", errors="backslashreplace",
                      newline="\n") as f:
                f.write(text)
                f.write("\n")
            return path
        except FileExistsError:
            continue


# -- the writer ------------------------------------------------------------------------------
class ExportResult(object):
    """write_rows()'s result: path, rows (written), complete, stopped (why not complete, in
    words, or ""), manifest (its path), sha256 and size of the export file, seconds."""

    def __init__(self, path):
        self.path = path
        self.rows = 0
        self.complete = False
        self.stopped = ""
        self.manifest = None
        self.sha256 = None
        self.size = None
        self.seconds = 0.0
        self.parts = []             # HTML: the part files [{path, size, sha256, ...}], if any

    def __repr__(self):
        return "ExportResult(%r, rows=%d, complete=%r)" % (self.path, self.rows, self.complete)


def _rows_loop(rows, emit, cancel, progress, every, result):
    """Feed rows to emit(row) until the end, a cancel or an error reading them."""
    it = iter(rows)
    n = 0
    while True:
        if cancel is not None and cancel():
            result.stopped = "by the user after %s" % _count(n, "row")
            break
        try:
            row = next(it)
        except StopIteration:
            result.complete = True
            break
        except Exception as e:  # noqa: BLE001 - a read error ends the export, marked so
            result.stopped = "by an error after %s: %s" % (_count(n, "row"), e)
            break
        emit(row)
        n += 1
        result.rows = n
        if progress is not None and every and n % every == 0:
            progress(n)
    if progress is not None:
        progress(n)


def _drop_partial(path):
    """Remove the partly written export file after a failed write; the text to add to the
    error (what became of the file)."""
    try:
        os.remove(path)
        return " (the partly written file was removed)"
    except OSError as e:
        return " (the partly written file %s is INCOMPLETE and could not be removed: %s)" % (
            path, e)


def _part_files(path):
    """The names of the files next to path that look like its HTML part files
    (<stem>_partNNN.html)."""
    stem, ext = os.path.splitext(path)
    ext = ext or ".html"
    prefix = os.path.basename(stem) + "_part"
    try:
        return set(n for n in os.listdir(os.path.dirname(path) or ".")
                   if n.startswith(prefix) and n.endswith(ext))
    except OSError:
        return set()


def _drop_html_partial(path, before=()):
    """_drop_partial plus the HTML part files (<stem>_partNNN.html) this failed export
    wrote: only files that were not there before it started (`before`: their names), so an
    earlier report's parts are never deleted. Returns the text to add to the error."""
    note = _drop_partial(path)
    folder = os.path.dirname(path) or "."
    removed = 0
    for name in _part_files(path) - set(before):
        try:
            os.remove(os.path.join(folder, name))
            removed += 1
        except OSError:
            pass
    if removed:
        note += " (%d part file(s) removed)" % removed
    return note


def _count(n, word):
    """'1 row', '52,001 rows'."""
    return "%s %s%s" % (format(n, ","), word, "" if n == 1 else "s")


def write_rows(path, fmt, columns, rows, info, blob_mode="hex", cancel=None, progress=None,
               protected=None, every=500, html=None, delimiter=",", encoding=None):
    """Write rows (any iterable of sequences, e.g. a generator over millions of rows: they
    are streamed) as fmt "csv", "json" or "html" to path, then its manifest next to it. info:
    from provenance(). cancel() true stops after the current row (the file is still closed
    properly and marked incomplete); progress(n) every `every` rows. protected(path) true for
    the export or manifest path: ExportError before anything is written. html: options of
    the HTML page (engine.html_report.write_export: case_name, title, badges, detail, dates,
    tags, notes, key_columns ...). Returns an ExportResult."""
    if fmt not in FORMATS + ("html",):
        raise ExportError("unknown export format %r (csv, json or html)" % (fmt,))
    _check_blob_mode(blob_mode)
    path = os.path.abspath(path) if path else path
    _refuse(protected, path)
    _refuse(protected, next(_manifest_candidates(path)))
    columns = [str(c) for c in columns]
    info = OrderedDict(info or ())
    info["blob_mode"] = blob_mode
    info["blob_lossless"] = BLOB_LOSSLESS[blob_mode]
    info.setdefault("columns", columns)
    safe = info.get("spreadsheet_safe", True) is not False
    # The formula prefix is a CSV-only defense (csv_cell); an HTML export that claims
    # spreadsheet_safe would overstate what was done.
    info["spreadsheet_safe"] = safe and fmt == "csv"
    info["value_encoding"] = value_encoding(safe)
    if fmt == "csv":
        info["csv_delimiter"] = delimiter
        info["csv_encoding"] = encoding or "utf-8-sig"
    result = ExportResult(path)
    start = time.time()
    created = False
    parts_before = ()
    try:
        if fmt == "csv":
            enc = encoding or "utf-8-sig"
            with open(path, "w", encoding=enc, errors="backslashreplace",
                      newline="") as f:
                created = True
                # cells are made by csv_cell
                w = csv_writer(f, formulas=False, delimiter=delimiter)
                w.writerow([csv_text(c, safe) for c in columns])
                _rows_loop(rows, lambda r: w.writerow([csv_cell(v, blob_mode, safe) for v in r]),
                           cancel, progress, every, result)
        elif fmt == "html":
            from .html_report import ReportError, write_export
            parts_before = _part_files(path)
            created = True  # Report.write opens the path (and part files) itself
            try:
                rr = write_export(path, columns, rows, info, blob_mode, cancel, progress, every,
                                  protected, html)
            except ReportError as e:
                raise ExportError(str(e))
            result.rows, result.complete, result.stopped = rr.rows, rr.complete, rr.stopped
            result.parts = rr.parts
        else:
            head = OrderedDict((k, v) for k, v in info.items() if k not in ("rows", "complete"))
            with open(path, "w", encoding="utf-8", errors="backslashreplace",
                      newline="\n") as f:
                created = True
                f.write('{\n "format": %s,\n "version": %d,\n "provenance": '
                        % (json.dumps(EXPORT_FORMAT), EXPORT_VERSION))
                f.write(json.dumps(head, ensure_ascii=False, indent=1, default=str)
                        .replace("\n", "\n "))
                f.write(',\n "columns": %s,\n "rows": [' % json.dumps(columns, ensure_ascii=False))
                state = {"first": True}

                def emit(r):
                    f.write("\n  " if state["first"] else ",\n  ")
                    state["first"] = False
                    f.write(json.dumps([json_cell(v, blob_mode) for v in r],
                                       ensure_ascii=False))
                _rows_loop(rows, emit, cancel, progress, every, result)
                end = OrderedDict([("rows", result.rows), ("complete", result.complete)])
                if result.stopped:
                    end["stopped"] = result.stopped
                f.write("%s],\n \"end\": %s\n}\n" % ("" if state["first"] else "\n ",
                                                       json.dumps(end, ensure_ascii=False)))
    except (OSError, csv.Error, ValueError, UnicodeError) as e:
        drop = _drop_html_partial(path, parts_before) if (created and fmt == "html") else \
            (_drop_partial(path) if created else "")
        raise ExportError("cannot write %s: %s%s" % (path, e, drop))
    result.seconds = time.time() - start
    result.size = os.path.getsize(path)
    result.sha256 = _hash_file(path)
    info["rows"] = result.rows
    info["complete"] = result.complete
    if result.stopped:
        info["stopped"] = result.stopped
    try:
        result.manifest = write_manifest(
            path, info, [{"path": path, "size": result.size, "sha256": result.sha256}] +
            [{"path": p["path"], "size": p["size"], "sha256": p["sha256"]}
             for p in result.parts], result.complete, protected)
    except OSError as e:
        raise ExportError("the export was written but not its manifest: %s" % e)
    return result
