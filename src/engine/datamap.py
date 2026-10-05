"""The Database Map, and the helpers it shares with "Copy with related" (engine.related_copy);
no Tk here: datamap_ui.py drives both.

database_map(session, relmap, options, cancel, progress, tool_version) -> DatabaseMap
    Every table (columns, declared types and affinity, keys, row counts, WITHOUT ROWID, the
    storage classes found in sampled rows), the relationship map (declared and verified links
    with their evidence, weaker links apart), date columns with an example converted value,
    BLOB columns with what sampled values decode to, a JOIN query per referring table that
    follows its links (depth <= 3, with date conversions), optional sample rows (off by
    default, values truncated), the diagram as SVG (engine.linkgraph), the evidence hashes and
    the tool version. Renderers: map_html (one self-contained, printable file in the tool's
    report design, engine.html_report), map_markdown, map_json; write_map() writes one of
    them to a file piece by piece.

Built to stay fast on databases of millions of rows: the links are checked on samples
(engine.relations), dates and BLOB kinds are judged from sampled rows, and a table whose row
count would take a long scan is given an estimate (max rowid, marked "≈") instead. No row is
dumped unless sample rows are asked for.

Everything is read through the Session; nothing is written except by write_text() and
write_map(), which refuse a path their is_protected() callback rejects (the evidence folder).
Functions take the session (and its relation map) as arguments, so a case of several databases
can build one map per database.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

from . import limits as lim
from . import timeline as tl
from .decode import summary as blob_summary
from .filenames import safe_file_name
from .sqltext import display_sql
from .fileformat.record import InvalidText
from .linkgraph import Link, LinkGraph, ValueLink
from .relations import ROWID, plain_reason, relation_map
from .schema import HIDDEN_STORED_GEN, HIDDEN_VIRTUAL_GEN, HIDDEN_VTAB, column_affinity, \
    quote_ident
from .session import is_interrupt

MAP_FORMAT = "sqlite-gui-analyzer-database-map"
# every cap is a named limit (engine.limits: default, range, settings.json override)
SAMPLE_CHARS = lim.DEFAULTS["map_sample_chars"]

# SQLite expressions turning a raw date of each kind into 'YYYY-MM-DD HH:MM:SS' (UTC); {x} is
# the column. Integer division keeps whole seconds, as the examples in the map show them.
SQL_DATE = {
    "unix_s": "datetime({x}, 'unixepoch')",
    "unix_ms": "datetime({x} / 1000, 'unixepoch')",
    "unix_us": "datetime({x} / 1000000, 'unixepoch')",
    "unix_ns": "datetime({x} / 1000000000, 'unixepoch')",
    "cocoa_s": "datetime({x} + 978307200, 'unixepoch')",
    "cocoa_ns": "datetime({x} / 1000000000 + 978307200, 'unixepoch')",
    "webkit_us": "datetime({x} / 1000000 - 11644473600, 'unixepoch')",
    "filetime": "datetime({x} / 10000000 - 11644473600, 'unixepoch')",
    "hfs_s": "datetime({x} - 2082844800, 'unixepoch')",
    "dotnet_ticks": "datetime({x} / 10000000 - 62135596800, 'unixepoch')",
    "ole_days": "datetime({x} + 2415018.5)",
    "gps_s": "datetime({x} + 315964800, 'unixepoch')",
    tl.ISO: "datetime({x})",
}


class Cancelled(Exception):
    """cancel() turned true, or the running statement was interrupted."""


def _check(cancel):
    if cancel is not None and cancel():
        raise Cancelled()


def utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def database_name(session):
    """The evidence file's name ('History', 'msgstore.db')."""
    return os.path.basename(session.evidence.main)


# -- values ----------------------------------------------------------------------------------
def sql_literal(v):
    """v written as an SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) if math.isfinite(v) else "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "X'%s'" % bytes(v).hex()
    return "'%s'" % str(v).replace("'", "''")


def sql_date(expr, kind):
    """The SQLite expression converting expr (a column) of a date kind to UTC text."""
    return SQL_DATE[kind].format(x=expr)


def json_value(v):
    """A raw value as JSON can hold it (BLOBs are described apart, see blob_info)."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else {"real": repr(v)}
    if isinstance(v, InvalidText):
        return {"invalid_text_hex": bytes(v).hex()}
    if isinstance(v, (bytes, bytearray)):
        return None
    return str(v)


def storage_class(v):
    if v is None:
        return "null"
    if isinstance(v, InvalidText):
        return "text (invalid)"
    if isinstance(v, (bytes, bytearray)):
        return "blob"
    if isinstance(v, bool) or isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "real"
    return "text"


def blob_info(v, include_hex=False):
    """{size, decodes_to, sha256[, hex]} of a BLOB."""
    b = bytes(v)
    d = {"size": len(b), "decodes_to": blob_summary(b), "sha256": hashlib.sha256(b).hexdigest()}
    if include_hex:
        d["hex"] = b.hex()
    return d


def convert_date(value, kind, text_numbers=False):
    """The raw value as UTC date text of `kind` ('YYYY-MM-DD HH:MM:SS[.fff]'), or None."""
    if text_numbers and isinstance(value, str):
        value = tl._as_number(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0 \
            and kind != tl.ISO:
        return None                     # a sentinel (0, -1): no date; the raw value is kept
    return tl.formatter(kind)(value)


def _loc_json(loc):
    v = loc.value
    if isinstance(v, tuple):
        v = [json_value(x) if not isinstance(x, (bytes, bytearray)) else bytes(x).hex()
             for x in v]
    return {"kind": loc.kind, "value": v}


# -- schema ----------------------------------------------------------------------------------
def table_schema(session, relmap, name):
    """{name, kind, sql, columns, without_rowid, rowid, rowid_alias, primary_key,
    reference_key, indexes} of a table (plain data, JSON-ready)."""
    info = session.info(name)
    cols = []
    for c in info.columns:
        extra = {HIDDEN_VTAB: "hidden", HIDDEN_VIRTUAL_GEN: "generated (virtual)",
                 HIDDEN_STORED_GEN: "generated (stored)"}.get(c.hidden, "")
        cols.append({"name": c.name, "type": c.decl_type, "affinity": column_affinity(c.decl_type),
                     "pk": c.pk_pos, "not_null": bool(c.notnull), "default": c.default_sql,
                     "note": extra, "visible": c.hidden != HIDDEN_VTAB})
    key = False
    if relmap is not None and name in relmap.build().tables:
        key = relmap.key(name)
    indexes = [{"name": e.name, "sql": e.sql or ""} for e in session.schema.of_type("index")
               if e.tbl_name == name]
    return {"name": name, "kind": info.kind, "sql": info.sql or "", "columns": cols,
            "without_rowid": bool(info.without_rowid),
            "rowid": None if info.without_rowid or info.kind != "table" else
            (info.rowid_name or "rowid"),
            "rowid_alias": info.columns[info.rowid_alias].name if info.rowid_alias is not None
            else None,
            "primary_key": [info.columns[i].name for i in info.pk_columns],
            "reference_key": None if key is False else ("rowid" if key is ROWID else key),
            "indexes": indexes}


def create_statements(schema):
    """The stored CREATE statements of a table and its indexes, one per line group, to show
    or copy: each cut at its first complete statement, anything stored after it only as
    comment lines (engine.sqltext.display_sql)."""
    parts = [display_sql(schema["sql"])] + [display_sql(ix["sql"]) for ix in schema["indexes"]]
    return "\n".join(p for p in parts if p) or "-- no CREATE statement"


def _visible(schema):
    return [c["name"] for c in schema["columns"] if c["visible"]]


def _col_sql(schema, alias, column):
    if column is ROWID:
        return "%s.%s" % (alias, schema["rowid"] or "rowid")
    return "%s.%s" % (alias, quote_ident(column))


def _order_sql(schema, alias):
    if schema["rowid"]:
        return "%s.%s" % (alias, schema["rowid"])
    return ", ".join("%s.%s" % (alias, quote_ident(c)) for c in schema["primary_key"])


def _key_sql(schema, alias, locators):
    """A WHERE term selecting exactly these rows (rowid or PRIMARY KEY locators)."""
    rowids = [l.value for l in locators if l.kind == "rowid"]
    parts = []
    if rowids:
        parts.append("%s.%s IN (%s)" % (alias, schema["rowid"] or "rowid",
                                        ", ".join(sql_literal(v) for v in rowids)))
    for l in locators:
        if l.kind == "pk":
            parts.append("(%s)" % " AND ".join(
                "%s.%s = %s" % (alias, quote_ident(c), sql_literal(v))
                for c, v in zip(schema["primary_key"], l.value)))
    if not parts:
        return "0"
    return parts[0] if len(parts) == 1 else "(%s)" % " OR ".join(parts)


# -- links -----------------------------------------------------------------------------------
class Via(object):
    """The link a related row came through, from the row it was reached from (table, column)
    to the related table's column, with its evidence."""
    __slots__ = ("table", "column", "other", "other_column", "direction", "kind", "fraction",
                 "sampled", "score", "reason", "why", "through", "db", "other_db")

    def __init__(self, rel, db="", other_db=""):
        """db, other_db: the names of the two databases, in a case (shown in the text); a
        link between databases (kind 'value') is always 'matched by value'."""
        self.through = rel.via          # (table, key) both columns of a 'same target' refer to
        self.table, self.column = rel.table, rel.column
        self.other, self.other_column = rel.other, rel.other_column
        self.direction, self.kind, self.score = rel.direction, rel.kind, rel.score
        self.db, self.other_db = db or "", other_db or ""
        ovs = [l.overlap for l in rel.links if l.overlap is not None and l.overlap.sampled
               and not l.overlap.error]
        self.fraction = min(ov.fraction for ov in ovs) if ovs else None
        self.sampled = min(ov.sampled for ov in ovs) if ovs else 0
        self.reason = rel.reasons[0] if self.cross and rel.reasons else plain_reason(rel)
        self.why = rel.why()

    @property
    def cross(self):
        """True for a link between two databases of a case (matched by value)."""
        return self.kind == "value"

    def ends(self):
        """((db, table, column), (db, table, column)): the referring side first ('peer': as
        found)."""
        a = (self.db, self.table, self.column)
        b = (self.other_db, self.other, self.other_column)
        return (b, a) if self.direction == "in" else (a, b)

    def evidence(self):
        parts = []
        if self.kind == "fk":
            parts.append("declared foreign key")
        if self.cross:
            parts.append("matched by value" + (" %d%%" % round(100 * self.fraction)
                                               if self.fraction is not None else ""))
        elif self.fraction is not None:
            parts.append("matched %d%%" % round(100 * self.fraction))
        if self.through is not None:
            parts.append("both refer to %s.%s" % (self.through[0], _cname(self.through[1])))
        return ", ".join(parts) or "not checked"

    @staticmethod
    def _where(db, table, column):
        name = "%s.%s" % (table, _cname(column))
        return "%s › %s" % (db, name) if db else name

    def text(self):
        a, b = self.ends()
        return "%s %s %s, %s" % (self._where(*a), "=" if self.direction == "peer" else "→",
                                 self._where(*b), self.evidence())

    def as_dict(self):
        (d1, t1, c1), (d2, t2, c2) = self.ends()
        out = {"text": self.text(), "from": "%s.%s" % (t1, _cname(c1)),
               "to": "%s.%s" % (t2, _cname(c2)), "direction": self.direction,
               "kind": {"fk": "declared foreign key", "name": "name",
                        "same_name": "same column name", "shared": "same target",
                        "value": "matched by value"}.get(self.kind, self.kind),
               "matched_pct": None if self.fraction is None else round(100 * self.fraction, 1),
               "values_checked": self.sampled, "score": self.score, "reason": self.reason}
        if d1 or d2:
            out["from_database"], out["to_database"] = d1, d2
        return out


def _cname(c):
    return "rowid" if c is ROWID else c


# -- Markdown ----------------------------------------------------------------------------------
_MD_ENTITY = re.compile(r"&(?=#?\w+;)")
_MD_BACKSLASH = re.compile(r"\\(?=[!-/:-@\[-`{-~])")


def md_escape(text):
    """Text safe inside a Markdown table cell: never taken for HTML, a table border or code;
    line breaks become <br>."""
    s = _MD_ENTITY.sub("&amp;", str(text)).replace("<", "&lt;").replace(">", "&gt;")
    s = _MD_BACKSLASH.sub(r"\\\\", s).replace("|", "\\|").replace("`", "\\`")
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return s


_BACKTICK_RUN = re.compile(r"`+")
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,})")


def md_code_block(text, lang="sql"):
    """Lines of a fenced Markdown code block holding text verbatim. The fence is one backtick
    longer than the longest backtick run in text (at least 3), so no line of text can close it
    (CommonMark closes a fence only with one at least as long); line breaks are normalised."""
    body = str(text).replace("\r\n", "\n").replace("\r", "\n")
    runs = [len(m) for m in _BACKTICK_RUN.findall(body)]
    fence = "`" * max(3, max(runs or [0]) + 1)
    return [fence + lang] + body.split("\n") + [fence]


def md_code_span(text):
    """text as inline code inside a Markdown table cell: one line (breaks become spaces), no
    backtick (becomes '), no unescaped '|' (would end the cell)."""
    s = " ".join(str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "`%s`" % s.replace("`", "'").replace("|", "\\|")


# -- the Database Map ----------------------------------------------------------------------------
class MapOptions(object):
    """What the map includes: sample_rows rows per table (0: none, values cut to the limit
    map_sample_chars); weaker=True lists the weaker links in a section of their own and draws
    them dotted in the diagram (otherwise only their number is given). limits: engine.limits
    values (checked; defaults for the rest). chain_depth, blob_samples and sample_chars set
    those limits directly. ValueError for a value out of range."""

    _SHORT = (("chain_depth", "map_query_depth"), ("blob_samples", "map_blob_samples"),
              ("sample_chars", "map_sample_chars"))

    def __init__(self, sample_rows=0, weaker=False, limits=None, chain_depth=None,
                 blob_samples=None, sample_chars=None):
        L = lim.checked(limits) if limits is not None else lim.current()
        given = {"chain_depth": chain_depth, "blob_samples": blob_samples,
                 "sample_chars": sample_chars}
        for short, name in self._SHORT:
            v = given[short]
            if v is not None:
                _check_limit(name, v)
                L[name] = v
        hi = lim.LIMITS["map_sample_rows"][2]
        if isinstance(sample_rows, bool) or not isinstance(sample_rows, int) or \
                not 0 <= sample_rows <= hi:
            raise ValueError("sample rows must be a whole number from 0 to %s, not %r"
                             % (format(hi, ","), sample_rows))
        self.limits, self.sample_rows, self.weaker = L, sample_rows, bool(weaker)
        self.chain_depth = L["map_query_depth"]
        self.blob_samples = L["map_blob_samples"]
        self.sample_chars = L["map_sample_chars"]

    def as_dict(self):
        return {"sample_rows": self.sample_rows, "sample_chars": self.sample_chars,
                "weaker_links": self.weaker, "chain_depth": self.chain_depth,
                "blob_samples": self.blob_samples,
                "limits": dict((k, v) for k, v in self.limits.items() if k.startswith("map_"))}


def _check_limit(name, v):
    lo, hi = lim.LIMITS[name][1:3]
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError("%s must be a whole number from %s to %s, not %r"
                         % (name, format(lo, ","), format(hi, ","), v))


class DatabaseMap(object):
    """database_map()'s result: plain data (as_dict() is the JSON export) and the SVG."""

    def __init__(self):
        self.data = {}
        self.svg = ""
        self.seconds = 0.0

    def __getitem__(self, key):
        return self.data[key]

    def as_dict(self):
        d = dict(self.data)
        d["diagram_svg"] = self.svg
        return d


STAGES = ("Checking links against the values", "Reading tables", "Finding date columns",
          "Decoding BLOB samples", "Writing queries", "Reading sample rows",
          "Waiting for the evidence hashes")


def database_map(session, relmap, options=None, cancel=None, progress=None, tool_version=""):
    """The DatabaseMap of the session's database, or None when cancel() stopped it.
    progress(stage, done, total) reports each stage (see STAGES)."""
    t0 = time.perf_counter()
    options = options or MapOptions()

    def report(stage, done, total):
        if progress is not None:
            progress(stage, done, total)
    try:
        return _build_map(session, relmap, options, cancel, report, tool_version, t0)
    except Cancelled:
        return None
    except sqlite3.Error as e:
        if is_interrupt(e):
            return None
        raise


def _build_map(session, relmap, options, cancel, report, tool_version, t0):
    m = DatabaseMap()
    d = m.data
    L = options.limits
    relmap.build()
    notes = []
    stage = STAGES[0]
    if not relmap.mapped:
        report(stage, 0, len(relmap.tables))
        if not relmap.map_links(cancel, lambda done, total: report(stage, done, total)):
            raise Cancelled()
    _check(cancel)
    rels = relmap.links()
    links = [Link(r, rows=relmap.known_rows) for r in rels]
    # tables
    names = [n for n in session.schema.names("table") if not n.lower().startswith("sqlite_")]
    tables = []
    for i, name in enumerate(names):
        report(STAGES[1], i, len(names))
        _check(cancel)
        s = table_schema(session, relmap, name)
        s["rows"], s["rows_estimated"] = _count(session, relmap, name, notes,
                                                L["map_exact_count_rows"])
        s["source"] = session.source(name)
        types, sampled = _storage_classes(session, name, cancel, L["map_type_rows"])
        s["sampled_rows"] = sampled
        for c in s["columns"]:
            c["stored_as"] = types.get(c["name"], {})
        s["links_out"] = [_link_dict(l) for l in links if l.src[1] == name and l.confident]
        s["links_in"] = [_link_dict(l) for l in links if l.dst[1] == name and l.confident]
        tables.append(s)
    report(STAGES[1], len(names), len(names))
    other = []
    for kind in ("view", "virtual"):
        for name in session.schema.names(kind):
            other.append({"name": name, "kind": kind, "sql": session.info(name).sql or ""})
    for e in session.schema.of_type("trigger"):
        other.append({"name": e.name, "kind": "trigger", "table": e.tbl_name, "sql": e.sql or ""})
    for name in session.schema.names("table"):
        if name.lower().startswith("sqlite_"):
            other.append({"name": name, "kind": "internal table",
                          "sql": session.info(name).sql or ""})
    confident = [_link_dict(l) for l in links if l.confident]
    weaker = [_link_dict(l) for l in links if not l.confident]
    confident.sort(key=lambda x: (x["from"].lower(), x["to"].lower()))
    weaker.sort(key=lambda x: (-x["score"], x["from"].lower()))
    weaker_found = len(weaker)
    if not options.weaker:
        weaker = []
    # date columns
    _check(cancel)
    det = tl.detect(session, tables=names, cancel=cancel,
                    progress=lambda done, total: report(STAGES[2], done, total))
    if det.cancelled:
        raise Cancelled()
    notes.extend(det.notes)
    dates, unconfirmed = [], []
    samples_cache = {}
    date_kinds = {}
    for tc in det.columns:
        if tc.kind is None:
            unconfirmed.append({"table": tc.table, "column": tc.column, "reason": tc.reason})
            continue
        date_kinds[(tc.table, tc.column)] = (tc.kind, tc.text_numbers)
        raw, utc = _date_example(session, tc, samples_cache)
        dates.append({"table": tc.table, "column": tc.column, "kind": tc.kind,
                      "label": tl.LABELS.get(tc.kind, tc.kind),
                      "short": tl.SHORT.get(tc.kind, tc.kind), "confidence": tc.confidence,
                      "reason": tc.reason, "example_raw": json_value(raw), "example_utc": utc,
                      "sampled_from": tl.fmt_time(tc.first) if tc.first else None,
                      "sampled_to": tl.fmt_time(tc.last) if tc.last else None,
                      "sql": sql_date(quote_ident(tc.column), tc.kind)})
    samples_cache.clear()
    # BLOB columns
    blobs = []
    cands = [(s, c) for s in tables for c in s["columns"]
             if c["stored_as"].get("blob") or "BLOB" in (c["type"] or "").upper()]
    for i, (s, c) in enumerate(cands):
        report(STAGES[3], i, len(cands))
        _check(cancel)
        if not s["rows"]:
            continue
        info = _blob_column(session, s["name"], c["name"], L, cancel)
        if info is not None:
            blobs.append(info)
    report(STAGES[3], len(cands), len(cands))
    # queries
    report(STAGES[4], 0, 1)
    schemas = dict((s["name"], s) for s in tables)
    queries, qnotes = chain_queries(links, schemas, date_kinds, L)
    notes.extend(qnotes)
    estimated = [s["name"] for s in tables if s["rows_estimated"]]
    if estimated:
        notes.append("%d table%s larger than %s rows (limit map_exact_count_rows) show an "
                     "estimated row count (≈, the largest rowid): %s" % (
                         len(estimated), "" if len(estimated) == 1 else "s",
                         format(L["map_exact_count_rows"], ","), ", ".join(estimated[:20]) +
                         (" …" if len(estimated) > 20 else "")))
    # sample rows
    samples = {}
    if options.sample_rows:
        todo = [s for s in tables if s["rows"]]
        for i, s in enumerate(todo):
            report(STAGES[5], i, len(todo))
            _check(cancel)
            samples[s["name"]] = _sample_rows(session, s["name"], options, date_kinds)
    # the diagram
    graph = LinkGraph(links, options.weaker)
    graph.layout()
    m.svg = graph.svg()
    # evidence
    ev = session.evidence
    if getattr(ev, "_thread", None) is not None:
        while not ev.hashing_done and not ev.hash_error:
            report(STAGES[6], ev.hashed_bytes, max(1, ev.total_bytes))
            _check(cancel)
            if not ev._thread.is_alive():
                break
            ev.wait_hashing(0.2)
    evidence = ev.summary()
    if ev.hash_error:
        notes.append("hashing failed: %s" % ev.hash_error)
    notes = [p for p in ("%s: %s" % (t, why) for t, why in relmap.problems)] + notes
    fp = ev.fingerprints.get("main")
    d.update({
        "format": MAP_FORMAT, "database": database_name(session), "path": ev.main,
        "generated_utc": utc_now_text(), "tool_version": tool_version,
        "sqlite_version": sqlite3.sqlite_version,
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "file": {"size": fp.size if fp else None, "page_size": session.pager.page_size,
                 "page_count": session.pager.page_count, "encoding": session.encoding,
                 "open_mode": session.mode, "wal": session.wal is not None},
        "status": [b.text for b in session.banners()],
        "summary": {"tables": len(tables),
                    "rows": sum(s["rows"] for s in tables if isinstance(s["rows"], int)),
                    "rows_estimated": any(s["rows_estimated"] for s in tables),
                    "confident_links": len(confident), "weaker_links": weaker_found,
                    "weaker_links_listed": options.weaker,
                    "date_columns": len(dates), "blob_columns": len(blobs),
                    "queries": len(queries)},
        "tables": tables, "other_objects": other,
        "links": {"confident": confident, "weaker": weaker},
        "date_columns": {"detected": dates, "unconfirmed": unconfirmed},
        "blob_columns": blobs, "queries": queries, "samples": samples,
        "evidence": evidence, "options": options.as_dict(), "notes": notes})
    m.seconds = time.perf_counter() - t0
    d["seconds"] = round(m.seconds, 2)
    return m


def estimate_rows(session, name):
    """An instant estimate of a table's rows: its largest rowid (SQL-served rowid tables), or
    None."""
    t = session.info(name)
    if session.source(name) != "sql" or t.kind != "table" or t.without_rowid or not t.rowid_name:
        return None
    try:
        r = session.conn().execute("SELECT max(%s) FROM %s" % (t.rowid_name,
                                                               quote_ident(name))).fetchone()
    except sqlite3.Error as e:
        if is_interrupt(e):
            raise Cancelled()
        return None
    return r[0] if isinstance(r[0], int) else 0


def _count(session, relmap, name, notes, exact_max):
    """(rows, estimated): the exact count when it is known or quick, else the largest rowid
    for a table larger than exact_max (the limit map_exact_count_rows: counting it would read
    millions of rows)."""
    try:
        known = relmap.known_rows(name) if name in relmap.tables else None
        if known is not None:
            return known, False
        est = estimate_rows(session, name)
        if est is not None and est > exact_max:
            return est, True
        if name in relmap.tables:
            return relmap.table_rows(name), False
        return session.count(name), False
    except sqlite3.Error as e:
        if is_interrupt(e):
            raise Cancelled()
        notes.append("%s: could not count the rows (%s)" % (name, e))
    except Exception as e:          # noqa: BLE001 - one table must not stop the map
        notes.append("%s: could not count the rows (%s)" % (name, e))
    return None, False


def _link_dict(l):
    rel = l.relation
    return {"from": "%s.%s" % (l.src[1], _cname(l.src_col)),
            "to": "%s.%s" % (l.dst[1], _cname(l.dst_col)),
            "from_table": l.src[1], "from_column": _cname(l.src_col),
            "to_table": l.dst[1], "to_column": _cname(l.dst_col),
            "direction": "refers to" if rel.direction == "out" else "same values",
            "kind": l.kind_text(), "score": l.score,
            "matched_pct": None if l.overlap is None else round(100 * l.overlap, 1),
            "reason": l.reason, "why": rel.why(), "rows_from": l.src_rows, "rows_to": l.dst_rows}


def _storage_classes(session, name, cancel, type_rows):
    """({column: {class: count}}, rows sampled) over the first type_rows rows (the limit
    map_type_rows)."""
    cols = session.visible_columns(name)
    out = dict((c, {}) for c in cols)
    if not cols:
        return out, 0
    if session.source(name) == "sql":
        try:
            n = 0
            for start in range(0, len(cols), 300):
                part = cols[start:start + 300]
                sel = ["count(*)"]
                for c in part:
                    q = quote_ident(c)
                    sel += ["sum(typeof(%s) = '%s')" % (q, k)
                            for k in ("integer", "real", "text", "blob", "null")]
                row = session.conn().execute("SELECT %s FROM (SELECT %s FROM %s LIMIT %d)" % (
                    ", ".join(sel), ", ".join(quote_ident(c) for c in part), quote_ident(name),
                    type_rows)).fetchone()
                n = row[0] or 0
                for j, c in enumerate(part):
                    counts = row[1 + 5 * j:6 + 5 * j]
                    out[c] = dict((k, v) for k, v in zip(("integer", "real", "text", "blob",
                                                          "null"), counts) if v)
            return out, n
        except sqlite3.Error as e:
            if is_interrupt(e):
                raise Cancelled()
            out = dict((c, {}) for c in cols)
    n = 0
    try:
        for i, row in enumerate(session.iter_rows(name)):
            if i >= type_rows:
                break
            if i % 500 == 0:
                _check(cancel)
            n += 1
            for c, v in zip(cols, row.values):
                k = storage_class(v)
                out[c][k] = out[c].get(k, 0) + 1
    except sqlite3.Error as e:
        if is_interrupt(e):
            raise Cancelled()
    return out, n


def _date_example(session, tc, cache):
    """(raw, utc text) of one sampled value of a detected date column."""
    if tc.table not in cache:
        try:
            cache[tc.table] = tl.sample_table(session, tc.table, 50)
        except sqlite3.Error as e:
            if is_interrupt(e):
                raise Cancelled()
            cache[tc.table] = ([], [])
    cols, rows = cache[tc.table]
    if tc.column not in cols:
        return None, None
    i = cols.index(tc.column)
    for r in rows:
        v = r[i] if i < len(r) else None
        if v is None or v in (0, -1, ""):
            continue
        utc = convert_date(v, tc.kind, tc.text_numbers)
        if utc:
            return v, utc
    return None, None


_COUNTS_RE = re.compile(r"\s*\([^()]*\)")


def decode_kind(text):
    """'gzip → protobuf (5 fields)' -> 'gzip → protobuf': the summary without counts."""
    return _COUNTS_RE.sub("", text).strip() or text


def _blob_column(session, table, column, L, cancel):
    """{table, column, sampled, sizes, decodes_to: [(kind, count)], examples} of the BLOBs
    in the first map_blob_scan_rows rows of a column (at most map_blob_samples of them; BLOBs
    larger than map_blob_max_bytes are counted, not decoded), or None when it holds none
    there."""
    n, scan, max_bytes = L["map_blob_samples"], L["map_blob_scan_rows"], L["map_blob_max_bytes"]
    values, too_big = [], 0
    if session.source(table) == "sql":
        q = quote_ident(column)
        try:
            cur = session.conn().execute(
                "SELECT length(%s) > %d, CASE WHEN length(%s) > %d THEN NULL ELSE %s END "
                "FROM (SELECT %s FROM %s LIMIT %d) WHERE typeof(%s) = 'blob' LIMIT %d" % (
                    q, max_bytes, q, max_bytes, q, q, quote_ident(table), scan, q, n))
            try:
                for big, v in cur.fetchall():
                    if big:
                        too_big += 1
                    elif v is not None:
                        values.append(bytes(v))
            finally:
                cur.close()
        except sqlite3.Error as e:
            if is_interrupt(e):
                raise Cancelled()
            values = None
    else:
        values = None
    if values is None:
        values, too_big = [], 0
        cols = session.visible_columns(table)
        ci = cols.index(column)
        for i, row in enumerate(session.iter_rows(table)):
            if i >= scan or len(values) + too_big >= n:
                break
            if i % 500 == 0:
                _check(cancel)
            v = row.values[ci] if ci < len(row.values) else None
            if isinstance(v, (bytes, bytearray)) and not isinstance(v, InvalidText):
                if len(v) > max_bytes:
                    too_big += 1
                else:
                    values.append(bytes(v))
    if not values and not too_big:
        return None
    kinds = {}
    for v in values:
        _check(cancel)
        k = decode_kind(blob_summary(v))
        kinds[k] = kinds.get(k, 0) + 1
    sizes = [len(v) for v in values]
    return {"table": table, "column": column, "sampled": len(values) + too_big,
            "sample_limit": n, "scan_rows": scan, "max_bytes": max_bytes,
            "decoded": len(values), "too_large": too_big,
            "size_min": min(sizes) if sizes else None, "size_max": max(sizes) if sizes else None,
            "decodes_to": sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])),
            "first_bytes": [v[:16].hex() for v in values[:3]]}


def chain_queries(links, schemas, date_kinds, L=None):
    """([{title, base, tables, joins, sql}], notes): for each table that refers to others
    through confident links, a SELECT of its rows LEFT JOINed to the rows they refer to,
    following the links of the joined tables too (up to map_query_depth links from the base,
    at most map_query_joins joins and map_query_columns columns; at most map_queries queries).
    Each link joins a row to the one row of the key it names, so the query returns one row
    per row of the base table. Date columns get converted '(UTC)' columns. The notes say what
    a limit left out."""
    L = L or lim.DEFAULTS
    depth, max_joins = L["map_query_depth"], L["map_query_joins"]
    max_cols, max_queries = L["map_query_columns"], L["map_queries"]
    notes = []
    cut_joins, cut_cols = [], []
    out_links = {}
    for l in links:
        if l.confident and l.relation.direction == "out" and l.src[1] in schemas \
                and l.dst[1] in schemas:
            out_links.setdefault(l.src[1], []).append(l)
    for v in out_links.values():
        v.sort(key=lambda l: (str(l.src_col).lower(), l.dst[1].lower()))
    queries = []
    for base in sorted(out_links, key=lambda t: t.lower()):
        aliases = [(base, "t0", None)]       # (table, alias, (link, from alias))
        frontier = [(base, "t0")]
        for _level in range(depth):
            nxt = []
            for table, alias in frontier:
                for l in out_links.get(table, ()):
                    if len(aliases) > max_joins:
                        if base not in cut_joins:
                            cut_joins.append(base)
                        break
                    if l.dst[1] == table:
                        continue                # a table referring to itself: not joined
                    a = "t%d" % len(aliases)
                    aliases.append((l.dst[1], a, (l, alias)))
                    nxt.append((l.dst[1], a))
            frontier = nxt
        if len(aliases) == 1:
            continue
        cols, ncols = [], 0
        for table, alias, _j in aliases:
            s = schemas[table]
            for c in _visible(s):
                if ncols >= max_cols:
                    if base not in cut_cols:
                        cut_cols.append(base)
                    break
                ref = "%s.%s" % (alias, quote_ident(c))
                label = c if alias == "t0" else "%s.%s" % (table, c)
                cols.append("%s AS %s" % (ref, quote_ident(label)))
                ncols += 1
                dk = date_kinds.get((table, c))
                if dk is not None:
                    cols.append("%s AS %s" % (sql_date(ref, dk[0]), quote_ident(label + " (UTC)")))
        joins = []
        for table, alias, j in aliases[1:]:
            l, frm = j
            joins.append("LEFT JOIN %s AS %s ON %s = %s" % (
                quote_ident(table), alias, _col_sql(schemas[table], alias, l.dst_col),
                _col_sql(schemas[l.src[1]], frm, l.src_col)))
        sql = "SELECT %s\nFROM %s AS t0\n%s;" % (",\n       ".join(cols), quote_ident(base),
                                                 "\n".join(joins))
        tables = [t for t, _a, _j in aliases]
        queries.append({"title": " ⋈ ".join(tables), "base": base, "tables": tables,
                        "joins": len(aliases) - 1,
                        "links": ["%s.%s → %s.%s" % (j[0].src[1], _cname(j[0].src_col),
                                                     j[0].dst[1], _cname(j[0].dst_col))
                                  for _t, _a, j in aliases[1:]],
                        "sql": sql})
    queries.sort(key=lambda q: (-q["joins"], q["base"].lower()))
    if cut_joins:
        notes.append("queries of %s: more links than the limit map_query_joins (%d) - the "
                     "first are joined" % (", ".join(cut_joins[:10]) +
                                           (" …" if len(cut_joins) > 10 else ""), max_joins))
    if cut_cols:
        notes.append("queries of %s: more columns than the limit map_query_columns (%d) - "
                     "the first are selected" % (", ".join(cut_cols[:10]) +
                                                 (" …" if len(cut_cols) > 10 else ""), max_cols))
    if len(queries) > max_queries:
        notes.append("%d queries could be written; the first %d are included (limit "
                     "map_queries)" % (len(queries), max_queries))
    return queries[:max_queries], notes


def _sample_rows(session, table, options, date_kinds):
    page = session.browse(table, 0, options.sample_rows)
    rows = []
    for r in page.rows:
        cells = []
        for c, v in zip(page.columns, r.values):
            dk = date_kinds.get((table, c))
            cells.append(sample_text(v, options.sample_chars, dk))
        rows.append(cells)
    return {"columns": list(page.columns), "rows": rows, "note": page.note or ""}


def sample_text(v, limit=SAMPLE_CHARS, date_kind=None):
    """A value in one short line: truncated text, a BLOB described, a date converted beside."""
    if v is None:
        return "NULL"
    if isinstance(v, InvalidText):
        return "invalid text (%d bytes)" % len(v)
    if isinstance(v, (bytes, bytearray)):
        return "BLOB %s bytes: %s" % (format(len(v), ","), blob_summary(bytes(v)))
    s = v if isinstance(v, str) else (repr(v) if isinstance(v, float) else str(v))
    s = s.replace("\r", " ").replace("\n", " ")
    if len(s) > limit:
        s = s[:limit] + "…"
    if date_kind is not None:
        utc = convert_date(v, date_kind[0], date_kind[1])
        if utc:
            s += " (UTC %s)" % utc
    return s


# -- map renderers -------------------------------------------------------------------------------
def map_json(m):
    return json.dumps(m.as_dict(), ensure_ascii=False, indent=1, default=str)


def _pct(v):
    return "" if v is None else "%g%%" % v


def _n(v):
    return "?" if v is None else format(v, ",")


def rows_text(n, estimated=False):
    """'1,234' or '≈ 3,585,703' (an estimate: the largest rowid)."""
    return ("≈ " if estimated and n is not None else "") + _n(n)


def _types_text(stored):
    return ", ".join("%s %s" % (k, format(v, ",")) for k, v in
                     sorted(stored.items(), key=lambda kv: -kv[1])) or "-"


def _keys_text(s):
    parts = []
    if s["rowid_alias"]:
        parts.append("%s is the rowid (INTEGER PRIMARY KEY)" % s["rowid_alias"])
    elif s["primary_key"]:
        parts.append("PRIMARY KEY (%s)" % ", ".join(s["primary_key"]))
    if s["without_rowid"]:
        parts.append("WITHOUT ROWID")
    elif not s["rowid_alias"] and s["rowid"]:
        parts.append("rows are identified by their rowid")
    ref = s["reference_key"]
    if ref and ref != "rowid" and ref != s["rowid_alias"] and [ref] != s["primary_key"]:
        parts.append("other tables refer to it by %s" % ref)
    return "; ".join(parts)


def _blob_scope(d):
    L = d["options"]["limits"]
    return ("Up to %s BLOB values per column are decoded, taken from its first %s rows (limits "
            "map_blob_samples, map_blob_scan_rows); BLOBs larger than %s bytes are counted, not "
            "decoded (map_blob_max_bytes). Storage classes are counted in the first %s rows of "
            "each table (map_type_rows)." % (
                _n(L["map_blob_samples"]), _n(L["map_blob_scan_rows"]),
                _n(L["map_blob_max_bytes"]), _n(L["map_type_rows"])))


def _weaker_text(s):
    n = s["weaker_links"]
    if s["weaker_links_listed"] or not n:
        return "%d weaker listed apart" % n
    return "%d weaker left out: export with 'Include weaker links' to list them" % n


def map_markdown(m):
    d = m.data
    e = md_escape
    s = d["summary"]
    out = ["# %s - Database Map" % e(d["database"]), "",
           "Generated %s by SQLite GUI Analyzer %s (SQLite %s). Read-only: nothing was "
           "written to the evidence." % (d["generated_utc"], d["tool_version"],
                                         d["sqlite_version"]), "",
           "%d tables, %s rows, %d confident links (%s), %d date columns, "
           "%d BLOB columns, %d generated queries." % (
               s["tables"], rows_text(s["rows"], s["rows_estimated"]), s["confident_links"],
               _weaker_text(s),
               s["date_columns"], s["blob_columns"], s["queries"]), ""]
    out += ["## Contents", ""] + ["- %s" % t for t in (
        "Evidence", "Relationships", "Tables", "Date columns", "BLOB columns", "Queries",
        "Views, triggers and other objects") + (("Sample rows",) if d["samples"] else ())] + [""]
    out += ["## Evidence", "", "| file | size | SHA-256 |", "|---|---|---|"]
    for fp in d["evidence"]:
        out.append("| %s (%s) | %s | %s |" % (e(fp["path"]), e(fp["role"]), _n(fp["size"]),
                                               fp["sha256"] or "not hashed"))
    f = d["file"]
    out += ["", "Page size %s, %s pages, %s, opened %s%s." % (
        f["page_size"], _n(f["page_count"]), f["encoding"], f["open_mode"],
        ", with a WAL" if f["wal"] else "")]
    for st in d["status"]:
        out.append("- %s" % e(st))
    out += ["", "## Relationships", "",
            "Confident links: declared foreign keys, and links whose values were found in the "
            "referred column (the share of sampled values found).", "",
            "| from | to | link | values found | why |", "|---|---|---|---|---|"]
    for l in d["links"]["confident"]:
        out.append("| %s | %s | %s | %s | %s |" % (e(l["from"]), e(l["to"]), e(l["kind"]),
                                                   _pct(l["matched_pct"]), e(l["reason"])))
    if not d["links"]["confident"]:
        out.append("| - | - | no confident link | | |")
    if d["links"]["weaker"]:
        out += ["", "### Weaker links (not trusted)", "",
                "Names or values only partly agree; they are not used in the queries.", "",
                "| from | to | score | values found | why |", "|---|---|---|---|---|"]
        for l in d["links"]["weaker"]:
            out.append("| %s | %s | %.2f | %s | %s |" % (e(l["from"]), e(l["to"]), l["score"],
                                                          _pct(l["matched_pct"]),
                                                          e("; ".join(l["why"]))))
    out += ["", "## Tables", ""]
    for t in d["tables"]:
        out += ["### %s" % e(t["name"]), "",
                "%s rows%s; %s" % (rows_text(t["rows"], t["rows_estimated"]),
                                   " (estimated: the largest rowid)" if t["rows_estimated"]
                                   else "", e(_keys_text(t)) or "no key"), "",
                "| column | declared type | affinity | key | stored as (first %s rows) |" % _n(
                    t["sampled_rows"]), "|---|---|---|---|---|"]
        for c in t["columns"]:
            out.append("| %s | %s | %s | %s | %s |" % (
                e(c["name"]), e(c["type"] or "-"), c["affinity"], "pk %d" % c["pk"] if c["pk"]
                else "", e(_types_text(c.get("stored_as") or {}))))
        for l in t["links_out"]:
            out.append("- refers: %s → %s (%s)" % (e(l["from"]), e(l["to"]), e(l["kind"])))
        for l in t["links_in"]:
            out.append("- referred by: %s → %s (%s)" % (e(l["from"]), e(l["to"]), e(l["kind"])))
        out += [""] + md_code_block(create_statements(t)) + [""]
    out += ["## Date columns", "", "| column | kind | confidence | example raw | example UTC | "
            "SQL |", "|---|---|---|---|---|---|"]
    for dc in d["date_columns"]["detected"]:
        out.append("| %s.%s | %s | %s | %s | %s | %s |" % (
            e(dc["table"]), e(dc["column"]), e(dc["label"]), dc["confidence"],
            e(dc["example_raw"]), dc["example_utc"] or "", md_code_span(dc["sql"])))
    if d["date_columns"]["unconfirmed"]:
        out += ["", "Named like dates, but the values do not read as dates (not converted):", ""]
        out += ["- %s.%s: %s" % (e(u["table"]), e(u["column"]), e(u["reason"]))
                for u in d["date_columns"]["unconfirmed"]]
    out += ["", "## BLOB columns", "", _blob_scope(d), "", "| column | sampled | decodes to | "
            "sizes |", "|---|---|---|---|"]
    for b in d["blob_columns"]:
        out.append("| %s.%s | %d | %s | %s |" % (
            e(b["table"]), e(b["column"]), b["sampled"],
            e(", ".join("%s (%d)" % kv for kv in b["decodes_to"])) +
            (" %d too large to decode" % b["too_large"] if b["too_large"] else ""),
            "%s - %s bytes" % (_n(b["size_min"]), _n(b["size_max"]))))
    out += ["", "## Queries", "", "Each query returns one row per row of its first table, with "
            "the rows its links refer to. Run them against a copy of the database.", ""]
    for q in d["queries"]:
        out += ["### %s" % e(q["title"]), ""] + md_code_block(q["sql"]) + [""]
    out += ["## Views, triggers and other objects", ""]
    if not d["other_objects"]:
        out += ["None.", ""]
    for o in d["other_objects"]:
        out += ["### %s %s" % (e(o["kind"]), e(o["name"])), ""]
        out += md_code_block(display_sql(o["sql"])) + [""]
    if d["samples"]:
        out += ["## Sample rows", "", "The first %d rows of each table; values cut to %d "
                "characters (limit map_sample_chars)." % (d["options"]["sample_rows"],
                                                          d["options"]["sample_chars"]), ""]
        for t, smp in d["samples"].items():
            out += ["### %s" % e(t), "", "| %s |" % " | ".join(e(c) for c in smp["columns"]),
                    "|%s|" % "|".join(["---"] * len(smp["columns"]))]
            out += ["| %s |" % " | ".join(e(v) for v in r) for r in smp["rows"]]
            out.append("")
    if d["notes"]:
        out += ["## Notes", ""] + ["- " + e(n) for n in d["notes"]] + [""]
    out += ["## Diagram", "", "The diagram is included as SVG in the HTML and JSON exports.", ""]
    return "\n".join(out)


def _map_report(title, d, what, evidence, case_name=""):
    """The Report (engine.html_report) a map is written into."""
    from .html_report import Report
    return Report(title, kind="Database Map", tool=("SQLite GUI Analyzer", d["tool_version"]),
                  case_name=case_name,
                  exported_utc=d["generated_utc"], python=d["python_version"],
                  sqlite=d["sqlite_version"], evidence=evidence, auto_summary=False,
                  details=[("Read-only", "%s opened read-only; nothing was written to the "
                                         "evidence." % what)])


def map_report(m, case_name=""):
    """The map of one database as an engine.html_report.Report."""
    d = m.data
    rep = _map_report("%s - Database Map" % d["database"], d, "The database was",
                      [("", fp) for fp in d["evidence"]], case_name)
    _map_sections(rep, m)
    rep.add_section("Build", id="build")
    rep.add_text("Built in %.1f s." % d["seconds"], "muted")
    return rep


def map_html(m):
    return map_report(m).render()


def _overview_cards(s):
    return [("Tables", _n(s["tables"]), None, None),
            ("Rows", rows_text(s["rows"], s["rows_estimated"]),
             "≈: an estimate (the largest rowid)" if s["rows_estimated"] else None, None),
            ("Confident links", _n(s["confident_links"]), None, None),
            ("Weaker links", _n(s["weaker_links"]),
             _weaker_text(s) if s["weaker_links"] else None,
             "warn" if s["weaker_links"] and not s["weaker_links_listed"] else None),
            ("Date columns", _n(s["date_columns"]), None, None),
            ("BLOB columns", _n(s["blob_columns"]), None, None),
            ("Generated queries", _n(s["queries"]), None, None)]


def _map_sections(rep, m, diagram=True, prefix="", level=2):
    """Add one database's sections to a report (level 3 and ids prefixed inside a case map)."""
    from .html_report import Markup, code_html, details_html, esc, simple_table_html
    d = m.data
    s = d["summary"]

    def sec(key, title):
        return rep.add_section(title, id=prefix + key, level=level)
    # evidence
    sec("evidence", "Evidence")
    rep.add_simple_table(["File", "Role", "Size", "SHA-256"], [
        [fp["path"], fp["role"], _n(fp["size"]), fp["sha256"] or "not hashed"]
        for fp in d["evidence"]], num=(2,), mono=(0, 3))
    f = d["file"]
    rep.add_text("Page size %s, %s pages, %s, opened %s%s." % (
        f["page_size"], _n(f["page_count"]), f["encoding"], f["open_mode"],
        ", with a WAL" if f["wal"] else ""))
    if d["status"]:
        rep.add_html("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(t) for t in d["status"]))
    # overview
    sec("overview", "Overview")
    rep.add_cards(_overview_cards(s))
    # diagram
    if diagram:
        sec("diagram", "Diagram")
        rep.add_svg(m.svg, None, "Solid line: declared foreign key; dashed: verified by "
                    "values%s. Hover a line for its reason." % (
                        "; dotted: weaker" if d["options"]["weaker_links"] else ""))
    # links
    sec("links", "Relationships")
    rep.add_text("Confident links: declared foreign keys, and links whose values were found in "
                 "the referred column (the share of sampled values found).", "muted")
    rep.add_html(_links_table(d["links"]["confident"], False))
    if d["links"]["weaker"]:
        rep.add_html(details_html(
            "Weaker links (%d): not trusted, not used in the queries" % len(
                d["links"]["weaker"]),
            Markup('<p class="muted">Names or values only partly agree.</p>' +
                   _links_table(d["links"]["weaker"], True))))
    elif s["weaker_links"] and not s["weaker_links_listed"]:
        rep.add_note("%s." % _weaker_text(s).capitalize(), "warn")
    # tables
    sec("tables", "Tables")
    for i, t in enumerate(d["tables"]):
        rep.add_html(_table_html(i, t, d, prefix))
    # dates
    sec("dates", "Date columns")
    if d["date_columns"]["detected"]:
        rep.add_simple_table(
            ["Column", "Kind", "Confidence", "Example raw", "Example UTC", "SQL", "Why"],
            [["%s.%s" % (dc["table"], dc["column"]), dc["label"], dc["confidence"],
              str(dc["example_raw"]), dc["example_utc"] or "",
              Markup("<code>%s</code>" % esc(dc["sql"])), dc["reason"]]
             for dc in d["date_columns"]["detected"]])
    else:
        rep.add_text("No column reads as dates.")
    if d["date_columns"]["unconfirmed"]:
        rep.add_html(details_html(
            "Named like dates, not confirmed by the values (%d)" % len(
                d["date_columns"]["unconfirmed"]),
            Markup("<ul>%s</ul>" % "".join("<li>%s.%s: <span class='muted'>%s</span></li>" % (
                esc(u["table"]), esc(u["column"]), esc(u["reason"]))
                for u in d["date_columns"]["unconfirmed"]))))
    # blobs
    sec("blobs", "BLOB columns")
    rep.add_text(_blob_scope(d), "muted")
    if d["blob_columns"]:
        rep.add_simple_table(
            ["Column", "Sampled", "Decodes to", "Sizes", "First bytes"],
            [["%s.%s" % (b["table"], b["column"]), b["sampled"], Markup(
                "<br>".join("%s (%d)" % (esc(k), n) for k, n in b["decodes_to"]) +
                ("<br><span class='muted'>%d too large to decode</span>" % b["too_large"]
                 if b["too_large"] else "")),
              "%s - %s bytes" % (_n(b["size_min"]), _n(b["size_max"])),
              Markup("<code>%s</code>" % "<br>".join(esc(x) for x in b["first_bytes"]))]
             for b in d["blob_columns"]], num=(1,))
    else:
        rep.add_text("No BLOB values in the sampled rows.")
    # queries
    sec("queries", "Queries")
    rep.add_text("One query per table that refers to others: its rows LEFT JOINed to the rows "
                 "its confident links refer to (up to %d links deep), with the date columns "
                 "converted. Run them against a copy of the database."
                 % d["options"]["chain_depth"], "muted")
    if not d["queries"]:
        rep.add_text("No table refers to another through a confident link.")
    for q in d["queries"]:
        rep.add_html(details_html(q["title"], Markup(
            '<p class="muted">%s</p>' % esc("; ".join(q["links"])) +
            code_html(q["sql"], "Query"))))
    # other objects
    sec("other", "Views, triggers and other objects")
    if not d["other_objects"]:
        rep.add_text("None.")
    for o in d["other_objects"]:
        rep.add_html(details_html("%s %s" % (o["kind"], o["name"]),
                                  code_html(display_sql(o["sql"]),
                                            "%s %s" % (o["kind"], o["name"]))))
    # samples
    if d["samples"]:
        sec("samples", "Sample rows")
        rep.add_text("The first %d rows of each table; values cut to %d characters (limit "
                     "map_sample_chars)." % (d["options"]["sample_rows"],
                                            d["options"]["sample_chars"]), "muted")
        for t, smp in d["samples"].items():
            rep.add_html(details_html("%s (%d rows)" % (t, len(smp["rows"])), Markup(
                simple_table_html(smp["columns"], smp["rows"], caption=t) +
                ('<p class="muted">%s</p>' % esc(smp["note"]) if smp["note"] else ""))))
    if d["notes"]:
        sec("notes", "Notes")
        rep.add_html("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(n) for n in d["notes"]))


def _links_table(links, weak):
    from .html_report import simple_table_html
    head = ["From", "To", "Link", "Values found", "Strength", "Why", "Rows (from / to)"]
    if not links:
        return simple_table_html(head, [["No confident link.", "", "", "", "", "", ""]],
                                 sortable=False)
    return simple_table_html(
        head, [[l["from"], l["to"], l["kind"], _pct(l["matched_pct"]), "%.2f" % l["score"],
                "; ".join(l["why"]) if weak else l["reason"],
                "%s / %s" % (_n(l["rows_from"]), _n(l["rows_to"]))] for l in links],
        classes=["warn" if weak else ""] * len(links), num=(3, 4, 6))


def _table_html(i, t, d, prefix=""):
    from .html_report import Markup, code_html, esc, simple_table_html
    dates = dict(((x["table"], x["column"]), x) for x in d["date_columns"]["detected"])
    blobs = dict(((x["table"], x["column"]), x) for x in d["blob_columns"])
    out = ["<details class='table' id='%st%d'><summary>%s <span class='muted'>(%s rows%s)"
           "</span></summary>" % (prefix, i, esc(t["name"]),
                                  rows_text(t["rows"], t["rows_estimated"]),
                                  ", WITHOUT ROWID" if t["without_rowid"] else "")]
    out.append("<p>%s. Read %s.</p>" % (esc(_keys_text(t) or "No key"),
                                        "with SQLite" if t["source"] == "sql" else
                                        "natively" if t["source"] == "native" else
                                        "- not readable"))
    rows = []
    for c in t["columns"]:
        reads = []
        dc = dates.get((t["name"], c["name"]))
        if dc is not None:
            reads.append("date: %s" % dc["short"])
        bc = blobs.get((t["name"], c["name"]))
        if bc is not None and bc["decodes_to"]:
            reads.append("BLOB: %s" % bc["decodes_to"][0][0])
        if c["note"]:
            reads.append(c["note"])
        rows.append([c["name"], c["type"] or "-", c["affinity"],
                     "pk %d" % c["pk"] if c["pk"] else "", "yes" if c["not_null"] else "",
                     c["default"] or "", _types_text(c.get("stored_as") or {}),
                     "; ".join(reads)])
    out.append(simple_table_html(
        ["Column", "Declared type", "Affinity", "Key", "Not null", "Default",
         "Stored as (first %s rows)" % _n(t["sampled_rows"]), "Reads as"], rows,
        caption=t["name"]))
    if t["links_out"] or t["links_in"]:
        out.append("<ul>%s%s</ul>" % (
            "".join("<li>refers: %s → %s <span class='muted'>(%s%s)</span></li>" % (
                esc(l["from"]), esc(l["to"]), esc(l["kind"]),
                ", %s found" % _pct(l["matched_pct"]) if l["matched_pct"] is not None else "")
                for l in t["links_out"]),
            "".join("<li>referred by: %s → %s <span class='muted'>(%s)</span></li>" % (
                esc(l["from"]), esc(l["to"]), esc(l["kind"])) for l in t["links_in"])))
    out.append(code_html(create_statements(t), "CREATE statements"))
    out.append("</details>")
    return Markup("".join(out))


def render_map(m, fmt):
    """The map (a DatabaseMap or a CaseMap) as 'html', 'markdown' or 'json' text."""
    if fmt not in MAP_EXTENSIONS:
        raise ValueError("unknown format %r (use html, markdown or json)" % (fmt,))
    if isinstance(m, CaseMap):
        return {"html": case_html, "markdown": case_markdown, "json": map_json}[fmt](m)
    return {"html": map_html, "markdown": map_markdown, "json": map_json}[fmt](m)


# -- the map of a whole case ---------------------------------------------------------------------
class MapDatabase(object):
    """One database of a case map: key (the case member's id), name, Session, relation map
    and colour (its boxes in the diagram)."""
    __slots__ = ("key", "name", "session", "relmap", "color")

    def __init__(self, key, name, session, relmap=None, color=""):
        self.key, self.name, self.session, self.color = key, name, session, color or ""
        self.relmap = relmap if relmap is not None else relation_map(session)


class CaseMap(object):
    """case_map()'s result: one DatabaseMap per database (maps), the links between them and
    the diagram of the whole case (svg)."""

    def __init__(self):
        self.maps = []              # [(MapDatabase, DatabaseMap)]
        self.data = {}
        self.svg = ""
        self.seconds = 0.0

    def as_dict(self):
        d = dict(self.data)
        d["database_maps"] = [m.as_dict() for _db, m in self.maps]
        d["diagram_svg"] = self.svg
        return d


def _cross_dict(l, names):
    a = "%s › %s.%s" % (names[l.src_db], l.src_table, l.src_col)
    b = "%s › %s.%s" % (names[l.dst_db], l.dst_table, l.dst_col)
    return {"from": a, "to": b, "from_database": names[l.src_db],
            "to_database": names[l.dst_db], "kind": "matched by value",
            "matched_pct": round(100 * l.fraction, 1) if l.overlap is not None else None,
            "score": l.score, "confident": bool(l.confident), "reason": l.reason()}


def case_map(databases, cross_links=(), options=None, cancel=None, progress=None,
             tool_version="", cross_note=""):
    """The CaseMap of several databases [MapDatabase]: each database's map (as
    database_map() builds it), the links between them (engine.crossdb.CrossLink, matched by
    value; weaker ones only with options.weaker) and one diagram with the databases' colours.
    None when cancel() stopped it. cross_note: how the links between databases were found
    (their limits), said in the map."""
    t0 = time.perf_counter()
    options = options or MapOptions()
    if not databases:
        raise ValueError("a case map needs at least one database")
    cm = CaseMap()
    n = len(databases)
    for i, db in enumerate(databases):
        def report(stage, done, total, name=db.name, i=i):
            if progress is not None:
                progress("%s (%d of %d): %s" % (name, i + 1, n, stage), done, total)
        m = database_map(db.session, db.relmap, options, cancel, report, tool_version)
        if m is None:
            return None
        cm.maps.append((db, m))
    names = dict((db.key, db.name) for db in databases)
    maps = dict((db.key, db.relmap) for db in databases)
    cross = [l for l in cross_links if l.src_db in names and l.dst_db in names]
    confident = [_cross_dict(l, names) for l in cross if l.confident]
    weaker = [_cross_dict(l, names) for l in cross if not l.confident]
    # one diagram: every database's links and the links between them, boxes in their colours
    links = []
    for db, _m in cm.maps:
        links.extend(Link(r, db.name, rows=db.relmap.known_rows) for r in db.relmap.links())
    links.extend(ValueLink(l, names, lambda k, t: maps[k].known_rows(t)) for l in cross)
    graph = LinkGraph(links, options.weaker)
    graph.layout()
    cm.svg = graph.svg(colors=dict((db.name, db.color) for db in databases if db.color))
    sums = [m.data["summary"] for _db, m in cm.maps]
    first = cm.maps[0][1].data
    cm.data.update({
        "format": MAP_FORMAT, "case": True, "generated_utc": utc_now_text(),
        "tool_version": tool_version, "sqlite_version": first["sqlite_version"],
        "python_version": first["python_version"],
        "databases": [{"name": db.name, "path": m.data["path"], "color": db.color,
                       "tables": m.data["summary"]["tables"], "rows": m.data["summary"]["rows"],
                       "rows_estimated": m.data["summary"]["rows_estimated"],
                       "links": m.data["summary"]["confident_links"],
                       "sha256": next((fp["sha256"] for fp in m.data["evidence"]
                                       if fp["role"] == "main"), None)}
                      for db, m in cm.maps],
        "summary": {"databases": n, "tables": sum(s["tables"] for s in sums),
                    "rows": sum(s["rows"] for s in sums),
                    "rows_estimated": any(s["rows_estimated"] for s in sums),
                    "links": sum(s["confident_links"] for s in sums),
                    "cross_links": len(confident), "weaker_cross_links": len(weaker),
                    "weaker_links_listed": options.weaker},
        "cross_links": {"confident": confident, "weaker": weaker if options.weaker else []},
        "cross_note": cross_note, "options": options.as_dict()})
    cm.seconds = time.perf_counter() - t0
    cm.data["seconds"] = round(cm.seconds, 2)
    return cm


def case_file_name(names, fmt):
    """'Case of 3 databases - Database Map.html'."""
    return "Case of %d databases - Database Map%s" % (len(names), MAP_EXTENSIONS[fmt])


def _cross_rows_html(links):
    from .html_report import simple_table_html
    head = ["From", "To", "Link", "Values found", "Why"]
    if not links:
        return simple_table_html(head, [["No link between the databases.", "", "", "", ""]],
                                 sortable=False)
    return simple_table_html(head, [
        [l["from"], l["to"], l["kind"] + ("" if l["confident"] else " (weaker)"),
         _pct(l["matched_pct"]), l["reason"]] for l in links], num=(3,))


def case_report(cm, case_name=""):
    """The map of a whole case as an engine.html_report.Report."""
    from .html_report import Markup, details_html, esc, valid_color
    d = cm.data
    s = d["summary"]
    evidence = []
    for db, m in cm.maps:
        evidence.extend((db.name, fp) for fp in m.data["evidence"])
    rep = _map_report("Case of %d databases - Database Map" % s["databases"], d,
                      "The databases were", evidence, case_name)
    rep.add_summary_cards([
        ("Databases", _n(s["databases"]), None, None), ("Tables", _n(s["tables"]), None, None),
        ("Rows", rows_text(s["rows"], s["rows_estimated"]), None, None),
        ("Links", _n(s["links"]), "inside the databases", None),
        ("Links between databases", _n(s["cross_links"]),
         "%d weaker" % s["weaker_cross_links"] if s["weaker_cross_links"] else None, None)])
    ids = [rep.uid("db%d" % i, "db") for i in range(len(d["databases"]))]
    rep.add_section("Databases", id="case-databases")
    rep.add_simple_table(["Database", "File", "Tables", "Rows", "Links", "SHA-256"], [
        [Markup("<a href='#%s'><span class='dbswatch' style='background:%s' aria-hidden='true'>"
                "</span>%s</a>" % (ids[i], valid_color(x["color"], "#475569"), esc(x["name"]))),
         x["path"], _n(x["tables"]), rows_text(x["rows"], x["rows_estimated"]), _n(x["links"]),
         x["sha256"] or "not hashed"] for i, x in enumerate(d["databases"])],
        num=(2, 3, 4), mono=(1, 5))
    rep.add_section("Links between databases", id="case-links")
    rep.add_text("Found by their values (no schema links two files): a link is trusted when "
                 "enough sampled values of one column are found in the other.%s" % (
                     " " + d["cross_note"] if d["cross_note"] else ""), "muted")
    rep.add_html(_cross_rows_html(d["cross_links"]["confident"]))
    if d["cross_links"]["weaker"]:
        rep.add_html(details_html(
            "Weaker links between databases (%d): not trusted, not followed"
            % len(d["cross_links"]["weaker"]), _cross_rows_html(d["cross_links"]["weaker"])))
    elif s["weaker_cross_links"]:
        rep.add_note("%d weaker link%s between databases left out: export with 'Include weaker "
                     "links' to list them." % (s["weaker_cross_links"],
                                               "" if s["weaker_cross_links"] == 1 else "s"),
                     "warn")
    rep.add_section("Diagram", id="case-diagram")
    rep.add_svg(cm.svg, None, "Each table's box is outlined in its database's colour. Solid "
                "line: declared foreign key; dashed: verified by values; orange short dashes: "
                "between databases, matched by value. Hover a line for its reason.")
    for i, (db, m) in enumerate(cm.maps):
        rep.add_section(db.name, id=ids[i], title_html=Markup(
            "<span class='dbswatch' style='background:%s' aria-hidden='true'></span>%s"
            % (valid_color(db.color, "#475569"), esc(db.name))))
        rep.add_text("%s - Database Map" % db.name, "muted")
        _map_sections(rep, m, diagram=False, prefix="d%d-" % i, level=3)
    rep.add_section("Build", id="build")
    rep.add_text("Built in %.1f s." % d["seconds"], "muted")
    return rep


def case_html(cm):
    return case_report(cm).render()


def _demoted(text):
    """Markdown with every heading one level deeper (outside code blocks)."""
    out, fence = [], ""
    for line in text.split("\n"):
        m = _FENCE_LINE.match(line)
        if m and not fence:
            fence = m.group(1)
        elif m and fence and line.strip() == "`" * len(line.strip()) \
                and len(line.strip()) >= len(fence):
            fence = ""
        out.append("#" + line if line.startswith("#") and not fence else line)
    return "\n".join(out)


def case_markdown(cm):
    d = cm.data
    e = md_escape
    s = d["summary"]
    out = ["# Case of %d databases - Database Map" % s["databases"], "",
           "Generated %s by SQLite GUI Analyzer %s. Read-only: nothing was written to the "
           "evidence." % (d["generated_utc"], d["tool_version"]), "",
           "## Databases", "", "| database | file | tables | rows | links | SHA-256 |",
           "|---|---|---|---|---|---|"]
    for x in d["databases"]:
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            e(x["name"]), e(x["path"]), _n(x["tables"]),
            rows_text(x["rows"], x["rows_estimated"]), _n(x["links"]),
            x["sha256"] or "not hashed"))
    out += ["", "## Links between databases", "",
            "Found by their values: a link is trusted when enough sampled values of one column "
            "are found in the other.%s" % (" " + e(d["cross_note"]) if d["cross_note"] else ""),
            "", "| from | to | link | values found | why |", "|---|---|---|---|---|"]
    rows = d["cross_links"]["confident"] + d["cross_links"]["weaker"]
    for l in rows:
        out.append("| %s | %s | %s | %s | %s |" % (
            e(l["from"]), e(l["to"]), l["kind"] + ("" if l["confident"] else " (weaker)"),
            _pct(l["matched_pct"]), e(l["reason"])))
    if not rows:
        out.append("| - | - | no link between the databases | | |")
    if s["weaker_cross_links"] and not s["weaker_links_listed"]:
        out += ["", "%d weaker link%s between databases left out: export with 'Include "
                "weaker links' to list them." % (s["weaker_cross_links"],
                                                 "" if s["weaker_cross_links"] == 1 else "s")]
    out.append("")
    for _db, m in cm.maps:
        out.append(_demoted(map_markdown(m)))
    return "\n".join(out)


MAP_EXTENSIONS = {"html": ".html", "markdown": ".md", "json": ".json"}


def safe_name(text):
    """Text usable as a file name (characters Windows refuses become '_'; engine.filenames)."""
    return safe_file_name(text, 150, default="database", strip=" .", strict=False)


def map_file_name(database, fmt):
    """'<database> - Database Map.html' (.md, .json), safe as a file name."""
    return "%s - Database Map%s" % (safe_name(database), MAP_EXTENSIONS[fmt])


def refuse_protected(path, is_protected):
    """ValueError (before anything is written) for a path is_protected() rejects."""
    if is_protected is not None and is_protected(path):
        raise ValueError("refusing to write %s: it is inside the evidence folder" % path)


def write_text(path, text, is_protected=None):
    """Write UTF-8 text; refuses (ValueError, nothing written) a path is_protected() rejects."""
    refuse_protected(path, is_protected)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return len(text.encode("utf-8"))


def write_map(path, fmt, m, is_protected=None, case_name=""):
    """Write the map as 'html' (engine.html_report, streamed), 'markdown' or 'json' (JSON is
    streamed by json.dump); returns the file's size. Refuses a path is_protected() rejects.
    case_name: the case the HTML cover names (optional)."""
    refuse_protected(path, is_protected)
    if fmt == "html":
        (case_report(m, case_name) if isinstance(m, CaseMap) else
         map_report(m, case_name)).write(path, is_protected)
        return os.path.getsize(path)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if fmt == "json":
            json.dump(m.as_dict(), f, ensure_ascii=False, indent=1, default=str)
        else:
            text = render_map(m, fmt)
            for i in range(0, len(text), 1 << 20):
                f.write(text[i:i + (1 << 20)])
    return os.path.getsize(path)
