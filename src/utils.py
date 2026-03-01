"""Utility functions for SQLite GUI Analyzer."""

import re
import os
import html as _html
from datetime import datetime, timezone, timedelta

from constants import _SIGS, _EXT_MAP, VERSION


# ── utility functions ────────────────────────────────────────────────────
def _q(s):
    """Quote SQL identifier."""
    return '"' + s.replace('"', '""') + '"'

def _le(s):
    """Escape string for LIKE."""
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")

def _regex_literal_hint(pattern):
    """Extract the longest guaranteed literal substring from a regex for LIKE pre-filter.

    Uses Python's own regex parser (sre_parse) for correct handling of all
    syntax: lookarounds, groups, quantifiers, flags, named groups, etc.
    Returns "" if no useful literal substring can be extracted.
    """
    try:
        from re import _parser as _sp, _constants as _sc
    except ImportError:
        import sre_parse as _sp, sre_constants as _sc

    try:
        parsed = _sp.parse(pattern)
    except Exception:
        return ""

    LITERAL = _sc.LITERAL
    SUBPATTERN = _sc.SUBPATTERN
    BRANCH = _sc.BRANCH
    ASSERT = _sc.ASSERT
    ASSERT_NOT = _sc.ASSERT_NOT
    MAX_REPEAT = _sc.MAX_REPEAT
    MIN_REPEAT = _sc.MIN_REPEAT

    # Top-level alternation — no single hint works
    if len(parsed) == 1 and parsed[0][0] == BRANCH:
        return ""
    for op, _ in parsed:
        if op == BRANCH:
            return ""

    def _walk(items):
        """Yield runs of guaranteed literal characters."""
        buf = []
        for op, av in items:
            if op == LITERAL:
                buf.append(chr(av))
            elif op == SUBPATTERN:
                # Group (...) — recurse; merge literals across group boundary
                sub_runs = list(_walk(av[-1]))
                if sub_runs:
                    # Attach first sub-run to current buffer (continuity)
                    buf.append(sub_runs[0])
                    if len(sub_runs) > 1:
                        # Group had internal breaks — yield merged first part,
                        # yield middle runs, keep last for continuation
                        yield ''.join(buf)
                        buf = []
                        for run in sub_runs[1:-1]:
                            yield run
                        buf.append(sub_runs[-1])
            elif op in (ASSERT, ASSERT_NOT):
                # Lookahead / lookbehind — zero-width, skip entirely
                if buf:
                    yield ''.join(buf)
                    buf = []
            elif op in (MAX_REPEAT, MIN_REPEAT):
                min_count, _max_count, sub = av
                if min_count >= 1:
                    sub_runs = list(_walk(sub))
                    # Single repeated literal char — include min_count copies
                    if len(sub_runs) == 1 and len(sub_runs[0]) == 1:
                        buf.append(sub_runs[0] * min_count)
                    else:
                        if buf:
                            yield ''.join(buf)
                            buf = []
                        yield from sub_runs
                else:
                    # min=0 (optional) — can't guarantee presence
                    if buf:
                        yield ''.join(buf)
                        buf = []
            elif op == BRANCH:
                # Nested alternation — break
                if buf:
                    yield ''.join(buf)
                    buf = []
            else:
                # IN, ANY, NOT_LITERAL, AT, GROUPREF, etc. — non-literal
                if buf:
                    yield ''.join(buf)
                    buf = []
        if buf:
            yield ''.join(buf)

    runs = list(_walk(parsed))
    if not runs:
        return ""
    return max(runs, key=len)

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

def tr(s, n=220):
    """Truncate string."""
    if s is None:
        return ""
    s = str(s)
    return s[:n] + "..." if len(s) > n else s

def vb(v):
    """Browse display value."""
    if v is None:
        return "NULL"
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
    # protobuf heuristic
    if len(data) >= 2 and (data[0] & 0x07) in (0, 1, 2, 5) and data[0] >> 3 > 0:
        try:
            tag = data[0]
            wt = tag & 0x07
            if wt == 0 and len(data) >= 2:
                return "Protobuf?"
            if wt == 2 and len(data) >= 3:
                ln = data[1]
                if 0 < ln < len(data):
                    return "Protobuf?"
        except Exception:
            pass
    return "BLOB"

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

def try_decode_timestamp(val):
    """Try to decode numeric value as various timestamp formats.
    Only triggers for values that are plausibly timestamps, not small
    integers that are likely IDs, counts, sizes, or dimensions."""
    if not isinstance(val, (int, float)):
        return None
    v = val
    # Skip small values — IDs, row counts, pixel sizes, file sizes < 10MB
    # Unix timestamp 946684800 = 2000-01-01, a sane minimum for modern data
    if isinstance(v, int) and -100000 < v < 946684800:
        return None
    if isinstance(v, float) and -100000 < v < 946684800:
        return None
    results = []
    try:
        # Unix seconds (2000-01-01 to 2099-12-31)
        if 946684800 <= v < 4102444800:
            dt = datetime.fromtimestamp(v, tz=timezone.utc)
            if 2000 <= dt.year <= 2099:
                results.append(("Unix seconds", dt.strftime("%Y-%m-%d %H:%M:%S UTC")))
    except (OSError, OverflowError, ValueError):
        pass
    try:
        # Unix milliseconds
        if 9.46e11 < v < 4.1e15:
            dt = datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
            if 2000 <= dt.year <= 2099:
                results.append(("Unix ms", dt.strftime("%Y-%m-%d %H:%M:%S.%f UTC")[:-3]))
    except (OSError, OverflowError, ValueError):
        pass
    try:
        # Unix microseconds
        if 9.46e14 < v < 4.1e18:
            dt = datetime.fromtimestamp(v / 1e6, tz=timezone.utc)
            if 2000 <= dt.year <= 2099:
                results.append(("Unix \u00b5s", dt.strftime("%Y-%m-%d %H:%M:%S.%f UTC")))
    except (OSError, OverflowError, ValueError):
        pass
    try:
        # Mac/Cocoa Absolute Time (seconds since 2001-01-01)
        # Minimum ~1 year after epoch to avoid small-number false positives
        mac_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
        if 31536000 < v < 1e10:
            dt = mac_epoch + timedelta(seconds=v)
            if 2002 <= dt.year <= 2099:
                results.append(("Mac Absolute", dt.strftime("%Y-%m-%d %H:%M:%S UTC")))
    except (OverflowError, ValueError):
        pass
    try:
        # Chrome/WebKit (microseconds since 1601-01-01)
        if 1e16 < v < 1.5e17:
            epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
            dt = epoch + timedelta(microseconds=v)
            if 1970 <= dt.year <= 2099:
                results.append(("Chrome/WebKit", dt.strftime("%Y-%m-%d %H:%M:%S UTC")))
    except (OverflowError, ValueError):
        pass
    try:
        # Windows FILETIME (100ns since 1601-01-01)
        if 1e17 < v < 3e18:
            epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
            dt = epoch + timedelta(microseconds=v / 10)
            if 1970 <= dt.year <= 2099:
                results.append(("Windows FILETIME", dt.strftime("%Y-%m-%d %H:%M:%S UTC")))
    except (OverflowError, ValueError):
        pass
    return results if results else None


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


def _build_schema_html(db, filename, version, tables=None, row_counts=None):
    """Build a full interactive HTML schema report for all tables."""
    E = _html.escape
    if tables is None:
        tables = db.tables() if db.ok else []
    if row_counts is None:
        row_counts = {}
    fname = E(os.path.basename(filename) if filename else "database")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_tables = len(tables)

    css = """
    *{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:1200px;
         margin:0 auto;padding:20px 24px;background:#f8f9fa;color:#1e1e2e}
    header{position:sticky;top:0;background:#f8f9fa;z-index:10;padding:8px 0 12px;
           border-bottom:2px solid #0052cc}
    h1{color:#0052cc;margin:0 0 4px}
    .meta{color:#5e6c84;font-size:13px;margin:0 0 10px}
    .search-bar{display:flex;gap:8px;align-items:center}
    .search-bar input{flex:1;padding:7px 12px;border:1px solid #c8cdd5;border-radius:6px;
                      font-size:14px;outline:none}
    .search-bar input:focus{border-color:#0052cc;box-shadow:0 0 0 2px rgba(0,82,204,.15)}
    .search-bar button{padding:7px 14px;background:#0052cc;color:#fff;border:none;
                       border-radius:6px;cursor:pointer;font-size:13px;white-space:nowrap}
    .search-bar button:hover{background:#0747a6}
    nav#toc{columns:3;column-gap:20px;margin:16px 0;padding:12px;
            background:#fff;border:1px solid #dfe2e8;border-radius:8px}
    nav#toc h3{margin:0 0 6px;color:#0747a6;column-span:all}
    nav#toc a{display:block;padding:2px 0;color:#0052cc;text-decoration:none;font-size:13px}
    nav#toc a:hover{text-decoration:underline}
    .table-section{background:#fff;border:1px solid #dfe2e8;border-radius:8px;
                   padding:16px 20px;margin:16px 0}
    .table-section h2{color:#0747a6;margin:0 0 8px;font-size:18px;
                      border-bottom:1px solid #eee;padding-bottom:6px}
    .badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;
           font-weight:600;margin-left:6px;vertical-align:middle}
    .badge-rows{background:#deebff;color:#0052cc}
    table{border-collapse:collapse;width:100%;margin:8px 0 12px;font-size:13px}
    th{background:#f0f3f8;color:#333;padding:7px 10px;text-align:left;font-weight:600;
       border-bottom:2px solid #c8cdd5}
    td{padding:6px 10px;border-bottom:1px solid #eee}
    tr:nth-child(even){background:#fafbfc}
    .b{font-weight:600}
    .c-pk{background:#ffeaea;color:#c0392b;padding:1px 6px;border-radius:3px;
          font-size:11px;font-weight:600;margin-right:4px;display:inline-block}
    .c-nn{background:#fff3e0;color:#e67e22;padding:1px 6px;border-radius:3px;
          font-size:11px;font-weight:600;margin-right:4px;display:inline-block}
    .c-uq{background:#f3e5f5;color:#8e24aa;padding:1px 6px;border-radius:3px;
          font-size:11px;font-weight:600;margin-right:4px;display:inline-block}
    .c-def{background:#e8f5e9;color:#2e7d32;padding:1px 6px;border-radius:3px;
           font-size:11px;font-weight:600;margin-right:4px;display:inline-block}
    .c-fk{background:#e3f2fd;color:#1565c0;padding:1px 6px;border-radius:3px;
          font-size:11px;font-weight:600;margin-right:4px;display:inline-block}
    .c-chk{background:#e0f2f1;color:#00695c;padding:1px 6px;border-radius:3px;
           font-size:11px;font-weight:600;margin-right:4px;display:inline-block}
    .sub{margin:6px 0;font-size:13px;color:#444}
    .sub b{color:#333}
    .idx-badge{background:#f0f0ff;padding:3px 8px;border-radius:4px;margin:2px 4px 2px 0;
               display:inline-block;font-size:12px}
    .fk-badge{background:#fff8e1;padding:3px 8px;border-radius:4px;margin:2px 4px 2px 0;
              display:inline-block;font-size:12px}
    details{margin:6px 0}
    details summary{cursor:pointer;font-size:13px;color:#0052cc;font-weight:600}
    details pre{background:#f4f5f7;padding:10px;border-radius:6px;font-size:12px;
                overflow-x:auto;margin:6px 0;white-space:pre-wrap;word-break:break-word}
    .copy-btn{font-size:11px;padding:2px 8px;margin-left:8px;cursor:pointer;
              background:#eee;border:1px solid #ccc;border-radius:3px}
    .copy-btn:hover{background:#ddd}
    .hidden{display:none}
    @media print{header{position:static}.search-bar{display:none}
                 .table-section,.hidden{display:block!important}
                 nav#toc{break-inside:avoid}}
    """

    # Build FK column lookup per table for badge display
    fk_cols = {}
    for t in tables:
        fk_list = db.fkeys_full(t)
        for fk in fk_list:
            fk_cols.setdefault(t, set()).add(fk["from"])

    p = []  # parts
    p.append(f"<!DOCTYPE html>\n<html>\n<head>\n<meta charset='utf-8'>\n"
             f"<title>Schema \u2014 {fname}</title>\n<style>{css}</style>\n</head>\n<body>\n")

    # Header
    p.append(f"<header>\n<h1>Schema: {fname}</h1>\n"
             f"<p class='meta'>Tables: {n_tables} | Generated: {now}"
             f" | SQLite Forensic Analyzer v{E(str(version))}</p>\n"
             f"<div class='search-bar'>\n"
             f"<input type='text' id='search' placeholder='Search tables, columns, constraints...' "
             f"oninput='filterSchema()'>\n"
             f"<button onclick='copyAllSQL()'>Copy All SQL</button>\n"
             f"</div>\n</header>\n")

    # TOC
    p.append("<nav id='toc'>\n<h3>Tables</h3>\n")
    for t in tables:
        cnt = fmt_count(row_counts.get(t, "?"))
        p.append(f"<a href='#tbl-{E(t)}' data-tbl='{E(t)}'>{E(t)} ({cnt})</a>\n")
    p.append("</nav>\n\n<main>\n")

    # Per-table sections
    for t in tables:
        te = E(t)
        cnt = fmt_count(row_counts.get(t, "?"))
        cols_full = db.columns_full(t)
        uniq = db.unique_columns(t)
        sql = db.create_sql(t) or ""
        idxs = db.indexes(t)
        fks = db.fkeys_full(t)
        checks = db.check_constraints(t)
        t_fk_cols = fk_cols.get(t, set())

        p.append(f"<section class='table-section' data-name='{te}'>\n"
                 f"<h2 id='tbl-{te}'>{te} <span class='badge badge-rows'>{cnt} rows</span></h2>\n")

        # CREATE SQL
        if sql:
            p.append(f"<details><summary>CREATE SQL "
                     f"<button class='copy-btn' onclick=\"copySQL('{te}')\">Copy</button>"
                     f"</summary>\n<pre id='sql-{te}'>{E(sql)}</pre></details>\n")

        # Column table
        p.append("<table>\n<thead><tr><th>#</th><th>Column</th><th>Type</th>"
                 "<th>Constraints</th></tr></thead>\n<tbody>\n")
        for ci, (cn, ct, notnull, default, pk) in enumerate(cols_full):
            cne = E(cn)
            badges = []
            if pk:
                badges.append("<span class='c-pk'>PK</span>")
            if notnull:
                badges.append("<span class='c-nn'>NOT NULL</span>")
            if cn in uniq:
                badges.append("<span class='c-uq'>UNIQUE</span>")
            if default is not None:
                badges.append(f"<span class='c-def'>DEFAULT {E(str(default))}</span>")
            if cn in t_fk_cols:
                badges.append("<span class='c-fk'>FK</span>")
            name_cell = f"<b>{cne}</b>" if pk else cne
            p.append(f"<tr><td>{ci+1}</td><td>{name_cell}</td><td>{E(ct or '')}</td>"
                     f"<td>{' '.join(badges) if badges else '-'}</td></tr>\n")
        p.append("</tbody></table>\n")

        # Indexes
        if idxs:
            p.append(f"<div class='sub'><b>Indexes ({len(idxs)}):</b> ")
            for name, unique, idx_cols in idxs:
                u = " <span class='c-uq'>UNIQUE</span>" if unique else ""
                p.append(f"<span class='idx-badge'>{E(name)}{u} ({E(', '.join(idx_cols))})</span> ")
            p.append("</div>\n")

        # Foreign keys
        if fks:
            p.append(f"<div class='sub'><b>Foreign Keys ({len(fks)}):</b> ")
            for fk in fks:
                actions = []
                if fk["on_update"]:
                    actions.append(f"ON UPDATE {E(fk['on_update'])}")
                if fk["on_delete"]:
                    actions.append(f"ON DELETE {E(fk['on_delete'])}")
                act_str = (" <span class='c-fk'>" + " ".join(actions) + "</span>") if actions else ""
                p.append(f"<span class='fk-badge'>{E(fk['from'])} &rarr; "
                         f"{E(fk['table'])}({E(fk['to'])}){act_str}</span> ")
            p.append("</div>\n")

        # CHECK constraints
        if checks:
            p.append(f"<div class='sub'><b>CHECK constraints ({len(checks)}):</b> ")
            for chk in checks:
                p.append(f"<span class='c-chk'>CHECK({E(chk)})</span> ")
            p.append("</div>\n")

        p.append("</section>\n\n")

    # Triggers section
    try:
        triggers = db.trigger_details()
    except Exception:
        triggers = []
    if triggers:
        p.append("<section class='table-section'>\n<h2>Triggers</h2>\n")
        for tname, tsql in triggers:
            p.append(f"<details><summary>{E(tname or '')}</summary>\n"
                     f"<pre>{E(tsql or '')}</pre></details>\n")
        p.append("</section>\n")

    p.append("</main>\n")

    # JavaScript
    p.append("""<script>
function filterSchema(){
  var q=document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.table-section').forEach(function(s){
    var n=s.getAttribute('data-name');if(!n)return;
    var txt=s.textContent.toLowerCase();
    s.classList.toggle('hidden',q&&txt.indexOf(q)===-1);
  });
  document.querySelectorAll('#toc a').forEach(function(a){
    var t=a.getAttribute('data-tbl');if(!t)return;
    var sec=document.querySelector(".table-section[data-name='"+t+"']");
    a.style.display=(!sec||sec.classList.contains('hidden'))?'none':'';
  });
}
function copyAllSQL(){
  var sqls=[];
  document.querySelectorAll('pre[id^="sql-"]').forEach(function(el){
    sqls.push(el.textContent);
  });
  navigator.clipboard.writeText(sqls.join('\\n\\n')).then(function(){alert('All SQL copied!')});
}
function copySQL(id){
  var el=document.getElementById('sql-'+id);
  if(el)navigator.clipboard.writeText(el.textContent).then(function(){alert('Copied!')});
}
</script>\n""")

    p.append("</body>\n</html>")
    return "".join(p)
