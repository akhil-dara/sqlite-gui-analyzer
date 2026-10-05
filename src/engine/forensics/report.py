"""Forensic reports: one self-contained HTML, CSV or JSON file.

A report holds the tool version (passed in: the engine does not import the UI), the evidence
files with size, modification time and SHA-256, the open mode, audit findings, recovered
records with provenance and confidence, a WAL history summary per table and recovered schema
objects. It is written only to the path the caller chose, and never inside the evidence folder.

HTML is the tool's one report design (engine.html_report): no external resources, every
value escaped, the recovered records as a searchable, sortable table (a very large recovery
continues in part files next to the report, which it lists). CSV is one file of sections (first
column = section name); text cells starting with = + - @, a tab or a carriage return are
neutralised with a leading apostrophe and NUL is written as \\x00 (engine.csvcells). JSON
carries everything in full (BLOBs as hex).
"""

import datetime
import io
import json
import os
import platform
import sqlite3

from ..csvcells import csv_writer
from ..evidence import _sha256
from ..export import TOOL_NAME

FORMATS = ("html", "csv", "json")
HTML_CELL_CAP = 2000
BLOB_PREVIEW = 64


class ReportError(Exception):
    """The report cannot be written where asked (e.g. inside the evidence folder)."""


def check_target(session, path):
    """Raise ReportError when `path` is not an acceptable report location."""
    if not path:
        raise ReportError("no report path given")
    if session.evidence.is_protected(path):
        raise ReportError("refusing to write a report inside the evidence folder (%s)"
                          % session.evidence.directory)
    if os.path.isdir(path):
        raise ReportError("%s is a folder" % path)
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise ReportError("folder %s does not exist" % parent)


def evidence_hashes(session, wait=None):
    """Evidence fingerprints with SHA-256: waits for the session's hashing thread, or hashes
    synchronously when it was never started (or did not finish)."""
    ev = session.evidence
    if not ev.hashing_done and ev._thread is not None:
        ev.wait_hashing(wait)
    for fp in ev.fingerprints.values():
        if not fp.sha256:
            try:
                fp.sha256 = _sha256(fp.path)
            except OSError as e:
                fp.sha256 = None
                ev.hash_error = "%s: %s" % (fp.path, e)
    return ev.summary()


def build(fx, version, records=None, findings=None, dropped=None, history=None,
          carve_stats=None, title=None):
    """The report content as a JSON-safe dict."""
    s = fx.session
    h = s.pager.header
    data = {
        "title": title or "SQLite forensic report",
        "tool": {"name": TOOL_NAME, "version": str(version),
                 "python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "database": {"path": s.evidence.main, "open_mode": s.mode,
                     "page_size": s.pager.page_size, "page_count": s.pager.page_count,
                     "encoding": h.encoding, "tables": len(s.tables()),
                     "wal_frames": len(s.wal.frames) if s.wal is not None else 0,
                     "journal": s.evidence.path("journal")},
        "evidence": evidence_hashes(s),
        "findings": [f.as_dict() for f in (findings or [])],
        "records": [r.as_dict() for r in (records or [])],
        "carve": dict(carve_stats or {}),
        "wal_history": [hs.as_dict() for hs in (history or [])],
        "dropped_schema": [d.as_dict() for d in (dropped or [])],
    }
    return data


def write_report(fx, path, fmt, version, case_name="", **kw):
    """Write the report; returns the path. Raises ReportError for a refused location.
    case_name: the case the HTML cover names (optional)."""
    fmt = (fmt or "").lower().lstrip(".")
    if fmt not in FORMATS:
        raise ReportError("unknown report format %r (use html, csv or json)" % fmt)
    check_target(fx.session, path)
    data = build(fx, version, **kw)
    if fmt == "html":
        from ..html_report import ReportError as _Refused
        try:
            html_report(data, case_name=case_name).write(path, fx.session.evidence.is_protected)
        except _Refused as e:
            raise ReportError(str(e))
        return path
    text = {"csv": to_csv, "json": to_json}[fmt](data)
    f = open(path, "w", encoding="utf-8-sig" if fmt == "csv" else "utf-8",
             errors="backslashreplace", newline="")
    try:
        with f:
            f.write(text)
    except Exception as e:  # noqa: BLE001 - any failure removes the partial file
        try:
            os.remove(path)
            what = "the partly written file was removed"
        except OSError:
            what = "the partly written file is INCOMPLETE"
        raise ReportError("cannot write %s: %s (%s)" % (path, e, what))
    return path


# -- JSON ------------------------------------------------------------------------
def to_json(data):
    return json.dumps(data, indent=1, ensure_ascii=False, default=str)


# -- CSV -------------------------------------------------------------------------
def _cell(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return "" if v is None else v


def to_csv(data):
    """The report as CSV text. Text cells are spreadsheet-safe and NUL is written as \\x00
    (engine.csvcells, as in every CSV of the tool)."""
    buf = io.StringIO()
    w = csv_writer(buf, formulas=True)

    def row(*cells):
        w.writerow([cells[0]] + [_cell(c) for c in cells[1:]])

    row("section", "field", "value")
    row("meta", "title", data["title"])
    row("meta", "tool", "%s %s (Python %s, SQLite %s)" % (
        data["tool"]["name"], data["tool"]["version"], data["tool"]["python"],
        data["tool"]["sqlite"]))
    row("meta", "generated_utc", data["generated_utc"])
    for k, v in sorted(data["database"].items()):
        row("database", k, v)
    row("evidence", "role", "path", "size", "mtime_ns", "sha256")
    for e in data["evidence"]:
        row("evidence", e["role"], e["path"], e["size"], e["mtime_ns"], e["sha256"])
    row("finding", "level", "code", "message", "details")
    for f in data["findings"]:
        row("finding", f["level"], f["code"], f["message"], f["details"])
    row("record", "id", "table", "confidence", "source", "file", "page", "offset", "frame",
        "frame_state", "rowid", "flags", "reasons", "values", "copies", "index")
    for r in data["records"]:
        p = r["provenance"]
        row("record", r["id"], r["table"], r["confidence"], p["source"], p["file"], p["page"],
            p["offset"], p["frame"], p["frame_state"], r["rowid"], " ".join(r["flags"]),
            "; ".join(r["reasons"]), dict(zip(r["columns"], r["values"])), len(r["copies"]),
            r.get("index"))
    row("wal_history", "table", "keys_seen", "multi_version", "deleted", "reused")
    for hs in data["wal_history"]:
        row("wal_history", hs["table"], hs["keys_seen"], len(hs["multi_version"]),
            len(hs["deleted"]), len(hs["reused"]))
    row("dropped_schema", "type", "name", "status", "rootpage", "root_status", "sql")
    for d in data["dropped_schema"]:
        row("dropped_schema", d["type"], d["name"], d["status"], d["rootpage"],
            d["root_status"], d["sql"])
    for k, v in sorted(data["carve"].items()):
        row("carve", k, v)
    return buf.getvalue()


# -- HTML ------------------------------------------------------------------------
def _val(v):
    """Display text for one JSON-safe value."""
    if isinstance(v, dict):
        if "blob_hex" in v:
            hx = v["blob_hex"]
            return "BLOB %d bytes: %s%s" % (v.get("size", len(hx) // 2), hx[:2 * BLOB_PREVIEW],
                                            "..." if len(hx) > 2 * BLOB_PREVIEW else "")
        if "invalid_text_hex" in v:
            return "invalid text (hex): %s" % v["invalid_text_hex"][:2 * BLOB_PREVIEW]
        if "real" in v:
            return v["real"]
    if v is None:
        return "NULL"
    s = str(v)
    return s if len(s) <= HTML_CELL_CAP else s[:HTML_CELL_CAP] + " ... (%d chars)" % len(s)


RECORD_FIELDS = ("id", "table", "confidence", "source", "file", "page", "offset", "frame",
                 "frame_state", "rowid", "values", "reasons", "flags", "copies")
_LEVEL_TONE = {"error": "bad", "warning": "warn", "info": "info"}


def _record_rows(records):
    for r in records:
        pv = r["provenance"]
        table = r["table"] or "(unknown)"
        if r.get("index"):
            table = "%s (index entry of %s)" % (table, r["index"])
        yield [r["id"], table, r["confidence"], pv["source"], pv["file"], pv["page"],
               pv["offset"], pv["frame"], pv["frame_state"], r["rowid"],
               "\n".join("%s = %s" % (c, _val(v)) for c, v in zip(r["columns"], r["values"])),
               "; ".join(r["reasons"]), " ".join(r["flags"]), len(r["copies"])]


def html_report(data, limits=None, case_name=""):
    """The report as an engine.html_report.Report (write() or render() it)."""
    from ..html_report import Report, badge
    t = data["tool"]
    db = data["database"]
    rep = Report(data["title"], kind="Forensic report", tool=(t["name"], t["version"]),
                 case_name=case_name,
                 python=t.get("python"), sqlite=t.get("sqlite"),
                 exported_utc=data["generated_utc"], limits=limits,
                 evidence=[("", e) for e in data["evidence"]],
                 details=[("Database", db.get("path") or ""),
                          ("Open mode", db.get("open_mode") or "")])
    levels = {}
    for f in data["findings"]:
        levels[f["level"]] = levels.get(f["level"], 0) + 1
    rep.add_summary_cards([
        ("Audit findings", format(len(data["findings"]), ","),
         ", ".join("%s %s" % (format(n, ","), k) for k, n in sorted(levels.items())) or None,
         "bad" if levels.get("error") else ("warn" if levels.get("warning") else None)),
        ("Recovered records", format(len(data["records"]), ","), None, None),
        ("WAL frames", format(db.get("wal_frames") or 0, ","), None, None),
        ("Dropped / changed objects", format(len(data["dropped_schema"]), ","), None,
         "warn" if data["dropped_schema"] else None)])
    rep.add_section("Database", id="database")
    rep.add_simple_table(["Field", "Value"], [[k, "" if v is None else v]
                                              for k, v in sorted(db.items())], mono=(1,))
    rep.add_section("Audit findings (%d)" % len(data["findings"]), id="findings")
    if data["findings"]:
        rep.add_simple_table(
            ["Level", "Code", "Message", "Details"],
            [[badge(f["level"], _LEVEL_TONE.get(f["level"], "muted")), f["code"], f["message"],
              json.dumps(f["details"], ensure_ascii=False, default=str)]
             for f in data["findings"]],
            classes=[_LEVEL_TONE.get(f["level"], "") for f in data["findings"]], mono=(3,))
    else:
        rep.add_text("No finding.")
    recs = data["records"]
    rep.add_section("Recovered records (%d)" % len(recs), id="records")
    if data["carve"]:
        rep.add_text(", ".join("%s: %s" % kv for kv in sorted(data["carve"].items())), "muted")
    rep.add_table("Recovered records", list(RECORD_FIELDS), _record_rows(recs),
                  total=len(recs), dates=False,
                  detail={"prov": ["source", "file", "page", "offset", "frame", "frame_state",
                                   "confidence", "reasons", "flags", "copies"],
                          "path": [os.path.basename(db.get("path") or "") or "database",
                                   {"col": "table"}, {"col": "rowid"}]})
    if data["wal_history"]:
        rep.add_section("WAL history", id="wal-history")
        rep.add_simple_table(["Table", "Keys seen", "Several versions", "Deleted", "Key reused"],
                             [[h["table"], h["keys_seen"], len(h["multi_version"]),
                               len(h["deleted"]), len(h["reused"])]
                              for h in data["wal_history"]], num=(1, 2, 3, 4))
    if data["dropped_schema"]:
        rep.add_section("Dropped / changed schema objects", id="dropped")
        rep.add_simple_table(["Type", "Name", "Status", "Root page", "Root status", "SQL"],
                             [[d["type"], d["name"], d["status"], d["rootpage"],
                               d["root_status"], d["sql"]] for d in data["dropped_schema"]],
                             mono=(5,))
    return rep


def to_html(data):
    return html_report(data).render()
