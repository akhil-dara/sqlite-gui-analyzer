"""Utility functions for SQLite GUI Analyzer."""

import re
import os
import hashlib
from datetime import datetime, timezone

from constants import _SIGS, _EXT_MAP, VERSION
from engine.fileformat.record import InvalidText
from engine.filenames import safe_file_name
from engine.sqltext import display_sql


# ── utility functions ────────────────────────────────────────────────────
def plural(n, word, words=None):
    """'1 row', '2,500 rows': a count (with thousands separators) and its noun in the right
    number; words is the plural when it is not word + 's'."""
    return "%s %s" % (format(n, ","), word if n == 1 else (words or word + "s"))


def longest(texts, n=20):
    """The n longest texts (by characters): the ones worth measuring in pixels to size a
    column (measuring every text of thousands took the Tk thread over 100 ms)."""
    texts = list(set(texts))
    if len(texts) <= n:
        return texts
    return sorted(texts, key=len, reverse=True)[:n]


def _q(s):
    """Quote SQL identifier."""
    return '"' + s.replace('"', '""') + '"'


READ_STATEMENTS = ("SELECT", "WITH", "VALUES", "EXPLAIN", "PRAGMA")


def sql_first_keyword(sql):
    """The first keyword of an SQL text, upper-case, after any whitespace, -- line comments,
    /* block */ comments and opening parentheses ('' when there is none)."""
    s, i, n = sql or "", 0, len(sql or "")
    while i < n:
        c = s[i]
        if c.isspace() or c == "(":
            i += 1
        elif s.startswith("--", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    m = re.match(r"[A-Za-z_]+", s[i:])
    return m.group(0).upper() if m else ""


def sql_reads_only(sql):
    """True when an SQL text starts with a statement that reads (SELECT, WITH, VALUES,
    EXPLAIN, PRAGMA). The connection is read-only anyway: SQLite refuses any write, and a
    PRAGMA that would change something fails with 'attempt to write a readonly database'."""
    return sql_first_keyword(sql) in READ_STATEMENTS


# ── evidence write guard ─────────────────────────────────────────────────
_write_guard = None


def set_write_guard(fn):
    """Register fn(path) -> True if writing to path would touch the evidence folder."""
    global _write_guard
    _write_guard = fn


def write_allowed(path):
    """False for an empty path, or (with an error dialog) for a path inside the evidence folder."""
    if not path:
        return False
    if _write_guard is not None and _write_guard(path):
        from tkinter import messagebox
        messagebox.showerror(
            "Refused", "That location is inside an evidence folder, or the file there has "
            "another name (a hard link) that may be evidence:\n%s\n\n"
            "Choose another folder or name so the evidence stays untouched." % os.path.abspath(path))
        return False
    return True


def safe_filename(text, limit=80):
    """Make text usable as part of a file name (row locators may contain quotes, colons...)."""
    return safe_file_name(text, limit, default="x", strip="_")


def blob_file_name(table, locator, column, ext):
    """File name for one exported BLOB: table, row and column in readable form, plus a short
    hash of the exact row locator. safe_filename() folds punctuation and truncates, so two
    rows (e.g. WITHOUT ROWID keys 'C:\\x/y' and 'C:\\x:y') would otherwise share a name."""
    digest = hashlib.sha256(repr(locator).encode("utf-8", "backslashreplace")).hexdigest()[:10]
    return "%s_r%s_%s_%s%s" % (safe_filename(table, 40), safe_filename(locator, 40), digest,
                               safe_filename(column, 40), ext)


def create_new_file(folder, name):
    """Open folder/name for binary writing without ever replacing an existing file.

    If the name is taken, _2, _3, ... is added before the extension. Returns (file, path).
    """
    base, ext = os.path.splitext(name)
    if base.endswith(".xml"):               # name_2.xml.plist, not name.xml_2.plist
        base, ext = base[:-4], ".xml" + ext
    for n in range(1, 10000):
        path = os.path.join(folder, name if n == 1 else "%s_%d%s" % (base, n, ext))
        try:
            return open(path, "xb"), path
        except FileExistsError:
            continue
    raise OSError("no free file name for %s in %s" % (name, folder))


# ── row flags from the engine (Row.flags) ────────────────────────────────
# flag -> (short label, explanation, Treeview tag). 'virtual_generated' is explained once per
# page by the engine's page note instead of per row.
ROW_FLAGS = {
    "damaged_record": ("damaged record", "the record is damaged: columns that could not be "
                       "decoded are shown as NULL", "flag_damaged"),
    "extra_values": ("extra values", "the record holds more values than the table has columns "
                     "(shown after the last column)", "flag_damaged"),
    "pre_alter": ("older than ALTER TABLE", "written before ALTER TABLE ADD COLUMN: the added "
                  "columns show their DEFAULT, as SQLite does", "flag_prealter"),
}


def row_flag_tag(flags):
    """Treeview tag for a row with these flags ('' when nothing needs marking)."""
    tags = [ROW_FLAGS[f][2] for f in flags or () if f in ROW_FLAGS]
    return "flag_damaged" if "flag_damaged" in tags else (tags[0] if tags else "")


def flag_summary(rows):
    """One line counting the flagged rows of a page, e.g. '2 rows: damaged record (...)'."""
    counts = {}
    for r in rows:
        for f in getattr(r, "flags", None) or ():
            if f in ROW_FLAGS:
                counts[f] = counts.get(f, 0) + 1
    return " | ".join("%d row%s: %s (%s)" % (n, "" if n == 1 else "s", ROW_FLAGS[f][0], ROW_FLAGS[f][1])
                      for f, n in sorted(counts.items()))


BLOB_FILE_MODES = ("raw", "json", "xml")


def export_row_blobs(folder, table, cols, rows, progress=None, cancel=None, files=None,
                     mode="raw", keep_raw=False, skipped=None):
    """Write every non-empty BLOB of rows ([locator, value, ...], cols = ['_rid', ...]) to folder.

    mode 'raw': the bytes as they are; 'json': each BLOB's decoded value as a .json file
    (BLOBs nothing decodes are written raw); 'xml': plist BLOBs (also inside compression) as
    an XML property list .plist file (other BLOBs are left out and counted in skipped, a
    dict reason -> count). keep_raw also writes the bytes beside each decoded file.
    Each file gets a unique name (blob_file_name) and existing files are never replaced.
    progress(rows_scanned, written) is called every 200 rows; cancel() true stops before the
    next row. files, a list, receives (path, size, sha256) of every file written (for the
    export's manifest). Returns (written, errors, first_error_text) so the caller can report
    exactly what happened.
    """
    if mode not in BLOB_FILE_MODES:
        raise ValueError("unknown BLOB file mode %r" % (mode,))
    written = errors = 0
    first_error = ""

    def put(name, content):
        f, path = create_new_file(folder, name)
        with f:
            f.write(content)
        if files is not None:
            files.append((path, len(content), hashlib.sha256(bytes(content)).hexdigest()))

    for ri, row in enumerate(rows):
        if cancel is not None and cancel():
            break
        loc = row[0] if row else ri
        for ci, v in enumerate(row[1:], start=1):
            if isinstance(v, (bytes, bytearray)) and len(v) > 0:
                data = bytes(v)
                ext = _EXT_MAP.get(blob_type(data), ".bin")
                col = cols[ci] if ci < len(cols) else "col%d" % ci
                try:
                    decoded = None
                    if mode != "raw":
                        from engine.decode.render import decoded_file
                        decoded, why = decoded_file(data, mode)
                        if decoded is None and mode == "xml":
                            if skipped is not None:
                                skipped[why] = skipped.get(why, 0) + 1
                            continue
                    if decoded is not None:
                        put(blob_file_name(table, loc, col, why), decoded)
                        written += 1
                        if keep_raw:
                            put(blob_file_name(table, loc, col, ext), data)
                    else:
                        put(blob_file_name(table, loc, col, ext), data)
                        written += 1
                except Exception as e:
                    errors += 1
                    first_error = first_error or str(e)
        if progress is not None and ri % 200 == 0:
            progress(ri + 1, written)
    return written, errors, first_error


def fmtb(b):
    """Format byte count."""
    if b is None:
        return "0B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024.0:
            if unit == "B":
                return f"{int(b)}{unit}"
            return f"{b:.1f}{unit}"
        b /= 1024.0
    return f"{b:.1f}PB"

def vb(v):
    """Browse display value."""
    if v is None:
        return "NULL"
    if isinstance(v, InvalidText):
        return "⚠ invalid text: " + " ".join("%02x" % b for b in v[:24]) + (" ..." if len(v) > 24 else "")
    if isinstance(v, bytes):
        bt = blob_type(v)
        # Known binary format — show type and size
        if bt != "BLOB":
            return f"[{bt} {fmtb(len(v))}]"
        # Unknown type — try UTF-8 decode (many DBs store text in BLOB columns)
        try:
            decoded = v.decode("utf-8")
            # Reject if too many control chars (likely binary, not text)
            ctrl = sum(1 for ch in decoded[:200] if ord(ch) < 32 and ch not in '\n\r\t')
            if ctrl <= 2:
                if len(decoded) <= 500:
                    return decoded
                return decoded[:300] + "..."
        except (UnicodeDecodeError, ValueError):
            pass
        return f"[BLOB {fmtb(len(v))}]"
    s = str(v)
    return s[:300] + "..." if len(s) > 300 else s

def plain_text(v):
    """A value as text for copying and CSV export, never truncated: NULL as 'NULL', invalid
    text with its undecodable bytes as \\x escapes, a BLOB as its text when it holds readable
    UTF-8, as x'hex' when small, else as '[TYPE n bytes]'."""
    if v is None:
        return "NULL"
    if isinstance(v, InvalidText):
        return bytes(v).decode("utf-8", "backslashreplace")
    if isinstance(v, bytes):
        bt = blob_type(v)
        if bt == "BLOB" and v:
            try:
                text = v.decode("utf-8")
                if sum(1 for ch in text[:200] if ord(ch) < 32 and ch not in "\n\r\t") <= 2:
                    return text
            except UnicodeDecodeError:
                pass
        if bt == "BLOB" and len(v) <= 256:
            return "x'%s'" % v.hex()
        return "[%s %d bytes]" % (bt, len(v))
    if isinstance(v, float):
        return repr(v)
    return str(v)


def json_value(v):
    """A value for JSON export: numbers and text as they are, NULL as null, anything else
    (BLOBs, invalid text, row locators) as plain_text()."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and abs(v) != float("inf") else repr(v)
    return plain_text(v)


def _snippet(text, term, mode_key, ctx=150):
    """Extract snippet of text around where term matches, for search display."""
    if text is None:
        return "NULL"
    s = str(text)
    if len(s) <= 400:
        return s
    # Find match position
    if mode_key == "rx":
        try:
            m = re.search(term, s)
            pos = m.start() if m else -1
        except Exception:
            pos = -1
    elif mode_key == "cs":
        pos = s.find(term)
    else:
        pos = s.lower().find(term.lower())
    if pos == -1:
        return s[:400] + "..."
    start = max(0, pos - ctx)
    end = min(len(s), pos + len(term) + ctx)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(s) else ""
    return f"{prefix}{s[start:end]}{suffix}"

def fmt_count(rc):
    """Format row count safely. Handles int, '~N' (approx), and '?' values."""
    if isinstance(rc, int):
        return f"{rc:,}"
    if isinstance(rc, str):
        if rc.startswith("~"):
            try:
                return f"~{int(rc[1:]):,}"
            except (ValueError, TypeError):
                pass
        return rc
    try:
        return f"{int(rc):,}"
    except (ValueError, TypeError):
        return str(rc)

def _int_count(rc, default=0):
    """Extract integer from count cache value. Handles int, '~N', '?'."""
    if isinstance(rc, int):
        return rc
    if isinstance(rc, str) and rc.startswith("~"):
        try:
            return int(rc[1:])
        except (ValueError, TypeError):
            pass
    return default

def blob_type(data):
    """Detect blob type from magic bytes."""
    if not data or not isinstance(data, bytes):
        return "BLOB"
    for sig, name in _SIGS:
        if data[:len(sig)] == sig:
            if name == "RIFF" and len(data) >= 12 and data[8:12] == b'WEBP':
                return "WEBP"
            return name
    # ftyp at offset 4 for MP4/HEIF
    if len(data) >= 8:
        ftyp = data[4:8]
        if ftyp == b'ftyp':
            brand = data[8:12] if len(data) >= 12 else b''
            if brand in (b'heic', b'heix', b'hevc', b'mif1'):
                return "HEIF"
            return "MP4"
    if _looks_protobuf(data):
        return "Protobuf?"
    return "BLOB"


PROTOBUF_SNIFF = 1 << 16        # longer BLOBs are left to the decoders (Inspect BLOB)


def _looks_protobuf(data):
    """True when every byte of data parses as protobuf fields (a quick check for the cell
    label: hashes, UUIDs and text almost never do). Wants two fields or more, or one
    length-delimited field that is most of the value, and no field number above 1000."""
    end = len(data)
    if end < 2 or end > PROTOBUF_SNIFF:
        return False
    if not any(b < 0x20 and b not in (9, 10, 13) for b in data[:256]):
        return False                    # printable text (real tags of fields 1-3 are < 0x20)
    pos = fields = 0
    while pos < end:
        tag, pos = _varint(data, pos, end)
        if tag is None:
            return False
        field, wt = tag >> 3, tag & 7
        if not 1 <= field <= 1000:
            return False
        if wt == 0:
            v, pos = _varint(data, pos, end)
            if v is None:
                return False
        elif wt == 1:
            pos += 8
        elif wt == 5:
            pos += 4
        elif wt == 2:
            n, pos = _varint(data, pos, end)
            if n is None:
                return False
            if fields == 0 and pos + n == end and (n * 4 >= end * 3 or end - n <= 3):
                return n > 0            # one field holding nearly the whole value
            pos += n
        else:
            return False
        if pos > end:
            return False
        fields += 1
    return fields >= 2


def _varint(data, pos, end):
    """(value, next position) of a canonical varint, or (None, pos)."""
    result = shift = 0
    start = pos
    while pos < end and pos - start < 10:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            if b == 0 and pos - start > 1:
                return None, pos
            return result, pos
        shift += 7
    return None, pos

def is_image(data):
    """Check if data is a displayable image."""
    if not data or not isinstance(data, bytes):
        return False
    if data[:3] == b'\xff\xd8\xff':
        return True
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return True
    if data[:4] in (b'GIF8',):
        return True
    if data[:2] == b'BM':
        return True
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return True
    return False

# ── Schema formatting ────────────────────────────────────────────────────
def _build_schema_text(db, tbl, row_count=None):
    """Build clear, readable schema text for a table."""
    cols_full = db.columns_full(tbl)
    uniq = db.unique_columns(tbl)
    hdr = f"Table: {tbl}"
    if row_count is not None:
        hdr += f"  ({fmt_count(row_count)} rows)"
    lines = [hdr, "=" * len(hdr), ""]

    # Calculate column widths for alignment
    max_name = max((len(cn) for cn, *_ in cols_full), default=10)
    max_type = max((len(ct or "") for _, ct, *_ in cols_full), default=4)
    max_name = max(max_name, 6)
    max_type = max(max_type, 4)

    # Header row
    lines.append(f"  {'Column':<{max_name}}  {'Type':<{max_type}}  Constraints")
    lines.append(f"  {'-'*max_name}  {'-'*max_type}  {'-'*30}")

    for cn, ct, notnull, default, pk in cols_full:
        constraints = []
        if pk:
            constraints.append("PK")
        if notnull:
            constraints.append("NOT NULL")
        if cn in uniq:
            constraints.append("UNIQUE")
        if default is not None:
            constraints.append(f"DEFAULT {default}")
        c_str = ", ".join(constraints) if constraints else "-"
        lines.append(f"  {cn:<{max_name}}  {(ct or ''):<{max_type}}  {c_str}")

    idxs = db.indexes(tbl)
    if idxs:
        lines.append("")
        lines.append(f"Indexes ({len(idxs)}):")
        for name, unique, idx_cols in idxs:
            u = " UNIQUE" if unique else ""
            lines.append(f"  {name}{u} ({', '.join(idx_cols)})")
    # Foreign keys — prefer fkeys_full() for ON DELETE/UPDATE details
    try:
        fks_full = db.fkeys_full(tbl)
    except Exception:
        fks_full = None
    if fks_full:
        lines.append("")
        lines.append(f"Foreign Keys ({len(fks_full)}):")
        for fk in fks_full:
            actions = []
            if fk.get("on_update"):
                actions.append(f"ON UPDATE {fk['on_update']}")
            if fk.get("on_delete"):
                actions.append(f"ON DELETE {fk['on_delete']}")
            act_str = f"  [{', '.join(actions)}]" if actions else ""
            lines.append(f"  {fk['from']} -> {fk['table']}({fk['to']}){act_str}")
    else:
        fks = db.fkeys(tbl)
        if fks:
            lines.append("")
            lines.append(f"Foreign Keys ({len(fks)}):")
            for ref_tbl, from_col, to_col in fks:
                lines.append(f"  {from_col} -> {ref_tbl}({to_col})")
    return "\n".join(lines)


def _schema_report(db, filename, version, tables=None, row_counts=None, evidence=None,
                   case_name=""):
    """The schema report as an engine.html_report.Report (the tool's one report design)."""
    from engine.html_report import Markup, Report, badge, code_html, esc
    if tables is None:
        tables = db.tables() if db.ok else []
    row_counts = row_counts or {}
    fname = os.path.basename(filename) if filename else "database"
    rep = Report("Schema: %s" % fname, kind="Schema report", case_name=case_name,
                 tool=("SQLite GUI Analyzer", str(version)),
                 evidence=[("", f) for f in (evidence or ())],
                 details=[("Database", filename or "")], auto_summary=False)
    info = []
    n_idx = n_fk = 0
    for t in tables:
        idxs, fks = db.indexes(t), db.fkeys_full(t)
        n_idx += len(idxs)
        n_fk += len(fks)
        info.append((t, idxs, fks))
    try:
        triggers = db.trigger_details()
    except Exception:
        triggers = []
    known = [c for c in (row_counts.get(t) for t in tables) if isinstance(c, int)]
    rep.add_summary_cards([
        ("Tables", format(len(tables), ","), None, None),
        ("Rows", format(sum(known), ","), "counted in %s of %s tables" % (
            format(len(known), ","), format(len(tables), ",")) if len(known) < len(tables)
         else None, None),
        ("Indexes", format(n_idx, ","), None, None),
        ("Foreign keys", format(n_fk, ","), None, None),
        ("Triggers", format(len(triggers), ","), None, None)])
    rep.add_summary_html('<p><button type="button" class="btn js-only" data-act="copy-all" '
                         'data-sel="pre.sql">Copy all SQL</button></p>')
    for t, idxs, fks in info:
        cnt = fmt_count(row_counts.get(t, "?"))
        rep.add_section(t, id="tbl-" + t, title_html=Markup("%s %s" % (
            esc(t), badge("%s rows" % cnt, "info"))))
        uniq = db.unique_columns(t)
        fk_cols = set(fk["from"] for fk in fks)
        rows = []
        for ci, (cn, ct, notnull, default, pk) in enumerate(db.columns_full(t)):
            marks = []
            if pk:
                marks.append(badge("PK", "warn"))
            if notnull:
                marks.append(badge("NOT NULL", "muted"))
            if cn in uniq:
                marks.append(badge("UNIQUE", "info"))
            if default is not None:
                marks.append(badge("DEFAULT %s" % default, "muted"))
            if cn in fk_cols:
                marks.append(badge("FK", "ok"))
            rows.append([ci + 1, Markup("<strong>%s</strong>" % esc(cn)) if pk else cn,
                         ct or "", Markup(" ".join(marks)) if marks else "-"])
        rep.add_simple_table(["#", "Column", "Type", "Constraints"], rows, num=(0,))
        if idxs:
            rep.add_html("<h3>Indexes (%d)</h3><ul>%s</ul>" % (len(idxs), "".join(
                "<li><span class='mono'>%s</span>%s (%s)</li>" % (
                    esc(name), " " + badge("UNIQUE", "info") if unique else "",
                    esc(", ".join(idx_cols))) for name, unique, idx_cols in idxs)))
        if fks:
            rep.add_html("<h3>Foreign keys (%d)</h3><ul>%s</ul>" % (len(fks), "".join(
                "<li>%s &rarr; %s(%s)%s</li>" % (
                    esc(fk["from"]), esc(fk["table"]), esc(fk["to"]),
                    "".join(" " + badge("%s %s" % (k, fk[key]), "muted") for k, key in (
                        ("ON UPDATE", "on_update"), ("ON DELETE", "on_delete")) if fk.get(key)))
                for fk in fks)))
        checks = db.check_constraints(t)
        if checks:
            rep.add_html("<h3>CHECK constraints (%d)</h3><ul>%s</ul>" % (len(checks), "".join(
                "<li class='mono'>CHECK(%s)</li>" % esc(chk) for chk in checks)))
        sql = db.create_sql(t) or ""
        if sql:
            rep.add_code(display_sql(sql), "CREATE statement")
    if triggers:
        rep.add_section("Triggers", id="triggers")
        for tname, tsql in triggers:
            rep.add_code(display_sql(tsql or ""), "Trigger %s" % (tname or ""))
    return rep


def _build_schema_html(db, filename, version, tables=None, row_counts=None, evidence=None,
                       case_name=""):
    """The full schema report (HTML, the tool's report design) for all tables. evidence: the
    evidence files ({role, path, size, sha256}) the report names, with their SHA-256;
    case_name: the case the cover names (optional)."""
    return _schema_report(db, filename, version, tables, row_counts, evidence,
                          case_name).render()
