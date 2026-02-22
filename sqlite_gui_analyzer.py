#!/usr/bin/env python3
"""SQLite GUI Analyzer v1.0 — Single-file desktop GUI for analyzing SQLite databases."""

import sqlite3, tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont
import threading, os, csv, json, re, sys, time, binascii, io, struct
from collections import OrderedDict
from datetime import datetime, timezone, timedelta

VERSION = "1.0"

# Windows taskbar icon fix — show app icon instead of Python icon
try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('sqlite.gui.analyzer.v1')
except Exception:
    pass

try:
    from PIL import Image as PILImage, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ── colours ──────────────────────────────────────────────────────────────
C = dict(
    bg="#ffffff", bg2="#f7f8fa", bg3="#eef0f4", bg4="#dfe2e8",
    border="#c8cdd5", text="#1a1a2e", text2="#5e6c84",
    accent="#0052cc", acl="#deebff", green="#00875a", gl="#e3fcef",
    red="#de350b", rl="#ffebe6", yellow="#ff991f", orange="#c25100",
    purple="#6554c0", tsel="#cce0ff", alt="#f8f9fb", hl="#fff0b3",
    hbg="#0747a6", hfg="#ffffff", sbg="#f4f5f7",
)

# ── tooltip helper ────────────────────────────────────────────────────────
class ToolTip:
    """Lightweight tooltip for any widget."""
    def __init__(self, widget, text, delay=500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._tip = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._hide()
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        if not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        lbl = tk.Label(tw, text=self.text, bg="#333333", fg="#ffffff",
                       font=("Segoe UI", 8), padx=6, pady=3, relief="solid", bd=1,
                       wraplength=300, justify="left")
        lbl.pack()

    def _hide(self, event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None

# ── treeview cell tooltip ────────────────────────────────────────────────
class TreeviewTooltip:
    """Hover tooltip for Treeview cells — shows full cell text when truncated."""
    def __init__(self, tree, delay=400):
        self.tree = tree
        self.delay = delay
        self._tip = None
        self._after_id = None
        self._last_cell = (None, None)
        self._font = None
        tree.bind("<Motion>", self._on_motion, add="+")
        tree.bind("<Leave>", self._hide, add="+")
        tree.bind("<ButtonPress>", self._hide, add="+")
        tree.bind("<MouseWheel>", self._hide, add="+")

    def _on_motion(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        cell = (row, col)
        if cell == self._last_cell:
            return
        self._last_cell = cell
        self._hide()
        if row and col:
            self._after_id = self.tree.after(self.delay, lambda: self._show(row, col, event))

    def _show(self, row, col, event):
        if not self.tree.winfo_exists():
            return
        try:
            col_idx = int(col.replace("#", "")) - 1
            values = self.tree.item(row, "values")
            if col_idx < 0 or col_idx >= len(values):
                return
            text = str(values[col_idx])
            if not text or len(text) < 10:
                return
            # Measure actual text width using font
            if not self._font:
                self._font = tkfont.nametofont("TkDefaultFont")
            col_id = self.tree.cget("columns")[col_idx] if self.tree.cget("columns") else col
            col_width = self.tree.column(col_id, "width")
            text_px = self._font.measure(text)
            if text_px <= col_width - 20:
                return  # fits — no tooltip needed
        except Exception:
            return
        x = self.tree.winfo_rootx() + event.x + 15
        y = self.tree.winfo_rooty() + event.y + 20
        self._tip = tw = tk.Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        lbl = tk.Label(tw, text=text, bg="#2d2d2d", fg="#f0f0f0",
                       font=("Consolas", 9), padx=8, pady=5, relief="solid", bd=1,
                       wraplength=500, justify="left")
        lbl.pack()
        # Keep tooltip within screen bounds
        tw.update_idletasks()
        sw = tw.winfo_screenwidth()
        sh = tw.winfo_screenheight()
        tw_w = tw.winfo_reqwidth()
        tw_h = tw.winfo_reqheight()
        if x + tw_w > sw - 10:
            x = sw - tw_w - 10
        if y + tw_h > sh - 10:
            y = event.y_root - tw_h - 5
        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self, event=None):
        if self._after_id:
            self.tree.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None
        self._last_cell = (None, None)

# ── search modes ─────────────────────────────────────────────────────────
SEARCH_MODES = OrderedDict([
    ("Case-Insensitive", "ci"),
    ("Case-Sensitive", "cs"),
    ("Exact Match", "ex"),
    ("Starts With", "sw"),
    ("Ends With", "ew"),
    ("Regex", "rx"),
    ("BLOB/Hex", "blob"),
    ("Column Name", "col"),
])

# ── blob signatures ──────────────────────────────────────────────────────
_SIGS = [
    (b'\xff\xd8\xff',         "JPEG"),
    (b'\x89PNG\r\n\x1a\n',   "PNG"),
    (b'GIF87a',               "GIF"),
    (b'GIF89a',               "GIF"),
    (b'RIFF',                 "RIFF"),
    (b'bplist',               "bplist"),
    (b'<?xml',                "XML/Plist"),
    (b'SQLite format 3',      "SQLite"),
    (b'%PDF',                 "PDF"),
    (b'PK\x03\x04',          "ZIP"),
    (b'\x1f\x8b',            "GZIP"),
    (b'II\x2a\x00',          "TIFF"),
    (b'MM\x00\x2a',          "TIFF"),
    (b'OggS',                 "OGG"),
    (b'\xff\xfb',            "MP3"),
    (b'\xff\xf3',            "MP3"),
    (b'\xff\xf2',            "MP3"),
    (b'ID3',                  "MP3"),
    (b'\x1a\x45\xdf\xa3',   "MKV/WEBM"),
    (b'\x00\x00\x01\x00',   "ICO"),
    (b'BM',                   "BMP"),
    (b'\x00asm',             "WASM"),
    (b'\x7fELF',             "ELF"),
    (b'MZ',                   "PE/EXE"),
    (b'\xfe\xed\xfa\xce',   "Mach-O"),
    (b'\xfe\xed\xfa\xcf',   "Mach-O"),
    (b'\xce\xfa\xed\xfe',   "Mach-O"),
    (b'\xcf\xfa\xed\xfe',   "Mach-O"),
    (b'dex\n',               "DEX"),
]

_EXT_MAP = {
    "JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp",
    "bplist": ".plist", "XML/Plist": ".plist", "SQLite": ".sqlite",
    "PDF": ".pdf", "ZIP": ".zip", "GZIP": ".gz", "TIFF": ".tif",
    "OGG": ".ogg", "MP3": ".mp3", "MKV/WEBM": ".mkv", "ICO": ".ico",
    "MP4": ".mp4", "HEIF": ".heif", "BMP": ".bmp", "WASM": ".wasm",
    "ELF": "", "PE/EXE": ".exe", "Mach-O": "", "DEX": ".dex",
    "RIFF": ".riff",
}

# ── utility functions ────────────────────────────────────────────────────
def _q(s):
    """Quote SQL identifier."""
    return '"' + s.replace('"', '""') + '"'

def _le(s):
    """Escape string for LIKE."""
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")

def _regex_literal_hint(pattern):
    """Extract the longest literal substring from a regex for LIKE pre-filter.

    Returns "" if the regex has top-level alternation or no useful literals.
    Used to build a fast SQL WHERE pre-filter before applying Python regex.
    """
    cleaned = []
    i = 0
    depth = 0  # parenthesis depth
    while i < len(pattern):
        ch = pattern[i]
        if ch == '(':
            depth += 1
            cleaned.append('\x00')
            i += 1
            continue
        if ch == ')':
            depth = max(0, depth - 1)
            cleaned.append('\x00')
            i += 1
            continue
        if ch == '|' and depth == 0:
            return ""  # top-level alternation — no single hint works
        if ch == '[':
            while i < len(pattern) and pattern[i] != ']':
                i += 1
            if i < len(pattern):
                i += 1
            cleaned.append('\x00')
            continue
        if ch == '{':
            while i < len(pattern) and pattern[i] != '}':
                i += 1
            if i < len(pattern):
                i += 1
            continue
        if ch == '\\' and i + 1 < len(pattern):
            nc = pattern[i + 1]
            if nc in r'\.^$*+?{}()|[]\\/':
                cleaned.append(nc)
            else:
                cleaned.append('\x00')
            i += 2
            continue
        if ch in '*?':
            if cleaned and cleaned[-1] != '\x00':
                cleaned[-1] = '\x00'  # previous char is optional — not guaranteed
            i += 1
            continue
        if ch in '.^$+|':
            cleaned.append('\x00')
            i += 1
            continue
        cleaned.append(ch)
        i += 1
    text = ''.join(cleaned)
    parts = [p for p in text.split('\x00') if p]
    if not parts:
        return ""
    return max(parts, key=len)

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
    fks = db.fkeys(tbl)
    if fks:
        lines.append("")
        lines.append(f"Foreign Keys ({len(fks)}):")
        for ref_tbl, from_col, to_col in fks:
            lines.append(f"  {from_col} -> {ref_tbl}({to_col})")
    return "\n".join(lines)


def _build_schema_html(db, tables, count_cache):
    """Build a full HTML schema report for all tables."""
    css = """
    body { font-family: 'Segoe UI', sans-serif; max-width: 1100px; margin: 0 auto; padding: 20px; background: #f8f9fa; color: #1a1a2e; }
    h1 { color: #0052cc; border-bottom: 3px solid #0052cc; padding-bottom: 8px; }
    h2 { color: #0747a6; margin-top: 32px; border-bottom: 1px solid #c8cdd5; padding-bottom: 4px; }
    .meta { color: #5e6c84; font-size: 13px; margin-bottom: 24px; }
    table { border-collapse: collapse; width: 100%; margin: 8px 0 16px 0; font-size: 13px; }
    th { background: #0052cc; color: white; padding: 6px 10px; text-align: left; font-weight: 600; }
    td { padding: 5px 10px; border-bottom: 1px solid #dfe2e8; }
    tr:nth-child(even) { background: #f4f5f7; }
    .pk { color: #de350b; font-weight: bold; }
    .nn { color: #ff991f; }
    .uq { color: #6554c0; }
    .def { color: #00875a; }
    .idx { background: #f0f0ff; padding: 4px 8px; border-radius: 4px; margin: 2px 0; display: inline-block; font-size: 12px; }
    .fk { background: #fff0e6; padding: 4px 8px; border-radius: 4px; margin: 2px 0; display: inline-block; font-size: 12px; }
    .toc { columns: 3; column-gap: 20px; margin: 12px 0; }
    .toc a { display: block; padding: 2px 0; color: #0052cc; text-decoration: none; font-size: 13px; }
    .toc a:hover { text-decoration: underline; }
    .badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; margin-left: 4px; }
    """
    fname = os.path.basename(db._path) if db._path else "database"
    parts = [f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
             f"<title>Schema Report - {fname}</title><style>{css}</style></head><body>"]
    parts.append(f"<h1>Schema Report: {fname}</h1>")
    parts.append(f"<p class='meta'>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                 f"| Tables: {len(tables)} | SQLite GUI Analyzer v{VERSION}</p>")

    # Table of contents
    parts.append("<h2>Table of Contents</h2><div class='toc'>")
    for t in tables:
        cnt = count_cache.get(t, "?")
        parts.append(f"<a href='#tbl-{t}'>{t} ({fmt_count(cnt)} rows)</a>")
    parts.append("</div>")

    # Each table
    for t in tables:
        cnt = count_cache.get(t, "?")
        cols_full = db.columns_full(t)
        uniq = db.unique_columns(t)
        parts.append(f"<h2 id='tbl-{t}'>{t} <span class='badge' style='background:#deebff;color:#0052cc;'>"
                     f"{fmt_count(cnt)} rows</span></h2>")

        # CREATE SQL
        sql = db.create_sql(t)
        if sql:
            parts.append(f"<details><summary>CREATE SQL</summary><pre style='background:#f4f5f7;padding:8px;"
                         f"border-radius:4px;font-size:12px;overflow-x:auto;'>{sql}</pre></details>")

        # Columns table
        parts.append("<table><tr><th>#</th><th>Column</th><th>Type</th><th>Constraints</th></tr>")
        for ci, (cn, ct, notnull, default, pk) in enumerate(cols_full):
            badges = []
            if pk:
                badges.append("<span class='pk'>PK</span>")
            if notnull:
                badges.append("<span class='nn'>NOT NULL</span>")
            if cn in uniq:
                badges.append("<span class='uq'>UNIQUE</span>")
            if default is not None:
                badges.append(f"<span class='def'>DEFAULT {default}</span>")
            parts.append(f"<tr><td>{ci+1}</td><td><b>{cn}</b></td><td>{ct or ''}</td>"
                         f"<td>{' '.join(badges) if badges else '-'}</td></tr>")
        parts.append("</table>")

        # Indexes
        idxs = db.indexes(t)
        if idxs:
            parts.append("<p><b>Indexes:</b> ")
            for name, unique, idx_cols in idxs:
                u = " UNIQUE" if unique else ""
                parts.append(f"<span class='idx'>{name}{u} ({', '.join(idx_cols)})</span> ")
            parts.append("</p>")

        # Foreign keys
        fks = db.fkeys(t)
        if fks:
            parts.append("<p><b>Foreign Keys:</b> ")
            for ref_tbl, from_col, to_col in fks:
                parts.append(f"<span class='fk'>{from_col} -> {ref_tbl}({to_col})</span> ")
            parts.append("</p>")

    parts.append("</body></html>")
    return "".join(parts)


# ── DB class ─────────────────────────────────────────────────────────────
class DB:
    def __init__(self):
        self._conn = None
        self._search_conn = None  # Separate connection for search (no row_factory)
        self._path = None

    def open(self, path):
        self.close()
        self._path = path
        uri = "file:" + path.replace("\\", "/") + "?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA cache_size = -8000")
            self._conn.execute("PRAGMA mmap_size = 268435456")
            self._conn.execute("PRAGMA temp_store = MEMORY")
            self._conn.execute("PRAGMA query_only = ON")
        except Exception:
            pass
        self._conn.create_function("REGEXP", 2, DB._safe_regexp)
        # Separate search connection — tuple mode, no row_factory overhead
        self._search_conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            self._search_conn.execute("PRAGMA cache_size = -8000")
            self._search_conn.execute("PRAGMA mmap_size = 268435456")
            self._search_conn.execute("PRAGMA temp_store = MEMORY")
            self._search_conn.execute("PRAGMA query_only = ON")
        except Exception:
            pass
        self._search_conn.create_function("REGEXP", 2, DB._safe_regexp)

    @staticmethod
    def _safe_regexp(pattern, value):
        if value is None:
            return False
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            return bool(re.search(pattern, str(value)))
        except Exception:
            return False

    def close(self):
        if self._search_conn:
            try:
                self._search_conn.close()
            except Exception:
                pass
            self._search_conn = None
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._path = None

    @property
    def ok(self):
        return self._conn is not None

    def tables(self):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def columns(self, tbl):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA table_info({_q(tbl)})").fetchall()
            return [(r[1], r[2]) for r in rows]
        except Exception:
            return []

    def columns_full(self, tbl):
        """Return full column info: (name, type, notnull, default, pk)."""
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA table_info({_q(tbl)})").fetchall()
            # r: cid, name, type, notnull, dflt_value, pk
            return [(r[1], r[2], bool(r[3]), r[4], int(r[5])) for r in rows]
        except Exception:
            return []

    def unique_columns(self, tbl):
        """Return set of column names that have a UNIQUE constraint (from indexes)."""
        if not self.ok:
            return set()
        try:
            result = set()
            idxs = self._conn.execute(f"PRAGMA index_list({_q(tbl)})").fetchall()
            for idx in idxs:
                if idx[2]:  # unique
                    info = self._conn.execute(f"PRAGMA index_info({_q(idx[1])})").fetchall()
                    if len(info) == 1:
                        result.add(info[0][2])
            return result
        except Exception:
            return set()

    def count(self, tbl):
        if not self.ok:
            return 0
        try:
            r = self._conn.execute(f"SELECT COUNT(*) FROM {_q(tbl)}").fetchone()
            return r[0] if r else 0
        except Exception:
            return 0

    def create_sql(self, tbl):
        if not self.ok:
            return ""
        try:
            r = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (tbl,)
            ).fetchone()
            return r[0] if r and r[0] else ""
        except Exception:
            return ""

    def indexes(self, tbl):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA index_list({_q(tbl)})").fetchall()
            result = []
            for r in rows:
                name = r[1]
                unique = r[2]
                cols = self._conn.execute(f"PRAGMA index_info({_q(name)})").fetchall()
                col_names = [c[2] for c in cols if c[2]]
                result.append((name, unique, col_names))
            return result
        except Exception:
            return []

    def fkeys(self, tbl):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA foreign_key_list({_q(tbl)})").fetchall()
            return [(r[2], r[3], r[4]) for r in rows]
        except Exception:
            return []

    def views(self):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def all_indexes(self):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%%' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def triggers(self):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def meta(self):
        if not self.ok:
            return {}
        info = {"path": self._path or ""}
        try:
            info["size"] = os.path.getsize(self._path) if self._path else 0
        except Exception:
            info["size"] = 0
        for prag in ("page_size", "page_count", "journal_mode", "encoding",
                      "auto_vacuum", "user_version", "freelist_count"):
            try:
                r = self._conn.execute(f"PRAGMA {prag}").fetchone()
                info[prag] = r[0] if r else ""
            except Exception:
                info[prag] = ""
        return info

    def integrity(self):
        if not self.ok:
            return "No database"
        try:
            r = self._conn.execute("PRAGMA quick_check(1)").fetchone()
            return r[0] if r else "ok"
        except Exception as e:
            return str(e)

    def search(self, tbl, cols, term, mode, limit, deep_blob, cancel):
        """Generator yielding search results using separate search connection."""
        if not self.ok or not term:
            return
        sconn = self._search_conn or self._conn
        mode_key = SEARCH_MODES.get(mode, "ci")
        col_names = [c[0] for c in cols]
        col_types = [c[1].upper() if c[1] else "" for c in cols]
        # Column Name mode
        if mode_key == "col":
            for cn in col_names:
                if cancel and cancel():
                    return
                if term.lower() in cn.lower():
                    yield dict(table=tbl, column=cn, rowid="-",
                               value=cn, type="column_name")
            return
        # Regex mode: use LIKE pre-filter when possible, cursor-based fetch
        if mode_key == "rx":
            try:
                rx = re.compile(term)
            except re.error:
                return
            found = 0
            hint = _regex_literal_hint(term)
            qtbl = _q(tbl)
            # Build SQL: use LIKE pre-filter if we have a literal hint
            if hint:
                esc_hint = _le(hint)
                parts = []
                params = []
                rx_indices = []
                for ci, cn in enumerate(col_names):
                    if not deep_blob and "BLOB" in col_types[ci]:
                        continue
                    parts.append(f"{_q(cn)} LIKE ? ESCAPE '\\'")
                    params.append(f"%{esc_hint}%")
                    rx_indices.append(ci)
                if parts:
                    sql = f"SELECT rowid, * FROM {qtbl} WHERE {' OR '.join(parts)}"
                else:
                    sql = f"SELECT rowid, * FROM {qtbl}"
                    params = []
                    rx_indices = list(range(len(col_names)))
            else:
                sql = f"SELECT rowid, * FROM {qtbl}"
                params = []
                rx_indices = [ci for ci in range(len(col_names))
                              if deep_blob or "BLOB" not in col_types[ci]]
            try:
                cur = sconn.execute(sql, params)
            except Exception:
                return
            scanned = 0
            # Bail-out only when NO hint (full table scan) — with LIKE pre-filter
            # the DB already narrows results, so scan ALL pre-filtered rows for accuracy
            max_scan = 0 if hint else max(limit * 50, 50000)
            while True:
                if cancel and cancel():
                    return
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                scanned += len(rows)
                for row in rows:
                    if cancel and cancel():
                        return
                    rid = row[0]
                    for i in rx_indices:
                        v = row[i + 1]
                        if v is None:
                            continue
                        if isinstance(v, bytes):
                            if not deep_blob:
                                continue
                            try:
                                s = v.decode("utf-8", "replace")
                            except Exception:
                                continue
                        else:
                            s = str(v)
                        if rx.search(s):
                            found += 1
                            yield dict(table=tbl, column=col_names[i], rowid=rid,
                                       value=tr(s), type=DB._dt(v))
                            if found >= limit:
                                return
                        if deep_blob and isinstance(v, bytes):
                            hx = binascii.hexlify(v).decode()
                            if rx.search(hx):
                                found += 1
                                yield dict(table=tbl, column=col_names[i], rowid=rid,
                                           value=f"[hex match in {fmtb(len(v))}]",
                                           type="blob_hex")
                                if found >= limit:
                                    return
                if max_scan and scanned >= max_scan:
                    break  # bail out — no hint, full scan, sparse matches
            return
        # Combined OR query — single table scan
        # Skip BLOB-typed columns in SQL WHERE (unless blob mode) to avoid
        # scanning huge binary data; Python-side already skips bytes.
        found = 0
        esc = _le(term)
        qtbl = _q(tbl)
        parts = []
        params = []
        search_col_indices = []  # indices of columns actually in WHERE
        for ci, cn in enumerate(col_names):
            # Skip BLOB-typed columns in WHERE to avoid slow scans
            if mode_key != "blob" and "BLOB" in col_types[ci]:
                continue
            qcol = _q(cn)
            if mode_key == "blob":
                parts.append(f"CAST({qcol} AS TEXT) LIKE ? ESCAPE '\\'")
                params.append(f"%{esc}%")
            elif mode_key == "cs":
                parts.append(f"instr({qcol},?)>0")
                params.append(term)
            elif mode_key == "ex":
                parts.append(f"{qcol}=?")
                params.append(term)
            elif mode_key == "sw":
                parts.append(f"{qcol} LIKE ? ESCAPE '\\'")
                params.append(f"{esc}%")
            elif mode_key == "ew":
                parts.append(f"{qcol} LIKE ? ESCAPE '\\'")
                params.append(f"%{esc}")
            else:
                parts.append(f"{qcol} LIKE ? ESCAPE '\\'")
                params.append(f"%{esc}%")
            search_col_indices.append(ci)
        if not parts:
            return
        try:
            sql = f"SELECT rowid, * FROM {qtbl} WHERE {' OR '.join(parts)} LIMIT {limit}"
            cur = sconn.execute(sql, params)
        except Exception:
            return
        # Inline column matching — only check columns included in WHERE
        check_indices = search_col_indices if mode_key != "blob" else list(range(len(col_names)))
        if mode_key == "ci" or mode_key not in ("cs", "ex", "sw", "ew", "blob"):
            tl = term.lower()
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and tl in str(v).lower():
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "cs":
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and term in str(v):
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "ex":
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and str(v) == term:
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "sw":
            tl = term.lower()
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and str(v).lower().startswith(tl):
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "ew":
            tl = term.lower()
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and str(v).lower().endswith(tl):
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "blob":
            tl = term.lower()
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is None:
                        continue
                    if isinstance(v, bytes):
                        try:
                            sv = v.decode("utf-8", "replace")
                        except Exception:
                            continue
                        if tl in sv.lower():
                            found += 1
                            yield dict(table=tbl, column=col_names[i], rowid=rid,
                                       value=tr(sv), type=DB._dt(v))
                            if found >= limit:
                                return
                    elif tl in str(v).lower():
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        # Deep blob hex search
        if deep_blob and mode_key == "blob":
            for cn in col_names:
                if cancel and cancel():
                    return
                if found >= limit:
                    return
                qcol = _q(cn)
                try:
                    sql2 = f"SELECT rowid, {qcol} FROM {_q(tbl)} WHERE typeof({qcol})='blob' LIMIT {limit - found}"
                    for br in sconn.execute(sql2):
                        if cancel and cancel():
                            return
                        bv = br[1]
                        if isinstance(bv, bytes):
                            hx = binascii.hexlify(bv).decode()
                            if term.lower() in hx.lower():
                                found += 1
                                yield dict(table=tbl, column=cn, rowid=br[0],
                                           value=f"[hex match in {fmtb(len(bv))}]",
                                           type="blob_hex")
                                if found >= limit:
                                    return
                except Exception:
                    pass

    def browse(self, tbl, lim, off, ocol=None, odir="ASC"):
        if not self.ok:
            return [], []
        try:
            order = ""
            if ocol:
                order = f" ORDER BY {_q(ocol)} {odir}"
            sql = f"SELECT rowid AS _rid, * FROM {_q(tbl)}{order} LIMIT {lim} OFFSET {off}"
            cur = self._conn.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return cols, [list(r) for r in rows]
        except Exception:
            return [], []

    def full_row(self, tbl, rid):
        if not self.ok:
            return {}, []
        try:
            sql = f"SELECT rowid AS _rid, * FROM {_q(tbl)} WHERE rowid = ?"
            cur = self._conn.execute(sql, (rid,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else {}, cols
        except Exception:
            return {}, []

    @staticmethod
    def _fv(v):
        """Format value for display."""
        if v is None:
            return "NULL"
        if isinstance(v, bytes):
            hx = binascii.hexlify(v[:50]).decode()
            if len(v) > 50:
                hx += "..."
            return hx
        return str(v)

    @staticmethod
    def _dt(v):
        """Detect type."""
        if v is None:
            return "NULL"
        if isinstance(v, int):
            return "INTEGER"
        if isinstance(v, float):
            return "REAL"
        if isinstance(v, bytes):
            return "BLOB"
        return "TEXT"

    @staticmethod
    def _match(v, t, m, db=None):
        if v is None:
            return False
        s = DB._fv(v)
        mk = SEARCH_MODES.get(m, "ci")
        if mk == "ci":
            return t.lower() in s.lower()
        if mk == "cs":
            return t in s
        if mk == "ex":
            return s == t
        if mk == "sw":
            return s.startswith(t)
        if mk == "ew":
            return s.endswith(t)
        if mk == "rx":
            try:
                return bool(re.search(t, s))
            except re.error:
                return False
        return t.lower() in s.lower()


# ── Theme setup ──────────────────────────────────────────────────────────
def setup_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    font_family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
    default_font = (font_family, 9)
    bold_font = (font_family, 9, "bold")
    small_font = (font_family, 8)

    style.configure("TFrame", background=C["bg"])
    style.configure("TLabel", background=C["bg"], foreground=C["text"], font=default_font)
    style.configure("M.TLabel", background=C["bg"], foreground=C["text2"], font=small_font)
    style.configure("B.TLabel", background=C["bg"], foreground=C["text"], font=bold_font)
    style.configure("H.TFrame", background=C["hbg"])
    style.configure("H.TLabel", background=C["hbg"], foreground=C["hfg"], font=(font_family, 14, "bold"))
    style.configure("HI.TLabel", background=C["hbg"], foreground=C["hfg"], font=default_font)
    style.configure("S.TFrame", background=C["sbg"])
    style.configure("S.TLabel", background=C["sbg"], foreground=C["text"], font=default_font)

    style.configure("TButton", font=default_font, padding=(8, 3))
    style.configure("P.TButton", background=C["accent"], foreground="#ffffff", font=bold_font, padding=(10, 4))
    style.map("P.TButton", background=[("active", "#003d99")])
    style.configure("D.TButton", background=C["red"], foreground="#ffffff", font=bold_font, padding=(10, 4))
    style.map("D.TButton", background=[("active", "#b52a00")])
    style.configure("G.TButton", background=C["green"], foreground="#ffffff", font=bold_font, padding=(10, 4))
    style.map("G.TButton", background=[("active", "#006644")])
    style.configure("Sm.TButton", font=small_font, padding=(4, 1))
    style.configure("HB.TButton", background="#0052cc", foreground="#ffffff", font=default_font, padding=(8, 3))
    style.map("HB.TButton", background=[("active", "#003d99")])

    style.configure("Treeview", background=C["bg"], fieldbackground=C["bg"],
                     foreground=C["text"], rowheight=24, font=default_font,
                     borderwidth=0)
    style.map("Treeview", background=[("selected", C["tsel"])],
              foreground=[("selected", C["text"])])
    style.configure("Treeview.Heading", background=C["bg3"], foreground=C["text"],
                     font=bold_font, relief="flat")

    style.configure("Sc.Treeview", background=C["sbg"], fieldbackground=C["sbg"],
                     foreground=C["text"], rowheight=24, font=default_font)
    style.configure("Sc.Treeview.Heading", background=C["bg3"], foreground=C["text"],
                     font=bold_font, relief="flat")
    style.map("Sc.Treeview", background=[("selected", C["tsel"])])
    style.configure("Treeview.Heading", relief="solid", borderwidth=1)

    style.configure("TCombobox", font=default_font)
    style.configure("TEntry", font=default_font)
    style.configure("SE.TEntry", font=(font_family, 11))
    style.configure("TProgressbar", troughcolor=C["bg3"], background=C["accent"])
    style.configure("TCheckbutton", background=C["bg"], font=default_font)


# ── HelpDialog ───────────────────────────────────────────────────────────
class HelpDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("SQLite GUI Analyzer - Help")
        self.geometry("750x600")
        self.configure(bg=C["bg"])
        self.transient(parent)

        txt = tk.Text(self, wrap="word", font=("Segoe UI", 10), bg=C["bg"],
                      fg=C["text"], relief="flat", padx=16, pady=12)
        sb = ttk.Scrollbar(self, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        txt.tag_configure("h1", font=("Segoe UI", 14, "bold"), foreground=C["accent"],
                          spacing1=12, spacing3=6)
        txt.tag_configure("h2", font=("Segoe UI", 11, "bold"), foreground=C["text"],
                          spacing1=10, spacing3=4)
        txt.tag_configure("code", font=("Consolas", 10), foreground=C["purple"],
                          background=C["bg2"])
        txt.tag_configure("tip", foreground=C["green"], font=("Segoe UI", 10, "italic"))
        txt.tag_configure("warn", foreground=C["red"], font=("Segoe UI", 10, "bold"))

        def h1(t): txt.insert("end", t + "\n", "h1")
        def h2(t): txt.insert("end", t + "\n", "h2")
        def p(t): txt.insert("end", t + "\n\n")
        def code(t): txt.insert("end", t + "\n", "code"); txt.insert("end", "\n")
        def tip(t): txt.insert("end", t + "\n\n", "tip")
        def warn(t): txt.insert("end", t + "\n\n", "warn")

        h1("SQLite GUI Analyzer v" + VERSION)

        h2("OVERVIEW")
        p("SQLite GUI Analyzer is a desktop tool for quickly searching, browsing, "
          "and analyzing SQLite databases. Designed for developers, data analysts, "
          "investigators, and anyone who needs to inspect database contents "
          "and understand schema structure efficiently — without writing SQL queries.")

        h2("SEARCH TAB")
        p("The Search tab scans all cell values across selected tables. Results show "
          "the table, column, row ID, matched value, and data type for each hit. "
          "Double-click any result to open the full row in a detail window.\n\n"
          "Use the Scope button to select which tables to search. Use Reset Scope "
          "to restore the selection to all tables. Filter results by table, column, "
          "or type using the drop-downs above the results list.")

        h2("SEARCH MODES")
        p("Case-Insensitive: Default. Matches text regardless of case.\n"
          "Case-Sensitive: Matches text with exact case.\n"
          "Exact Match: Cell value must equal the search term exactly.\n"
          "Starts With / Ends With: Value must begin or end with the term.\n"
          "Regex: Full Python regular expression matching.\n"
          "BLOB/Hex: Search within binary data (text cast). Enable Deep BLOB for hex-level matching.\n"
          "Column Name: Find columns whose name contains the search text.")

        h2("REGEX TIPS")
        warn("Type a SINGLE backslash in the search entry: \\d not \\\\d")
        p("The search entry passes text directly to Python's re module. "
          "Omit ^ and $ anchors to find patterns anywhere within values.\n\n"
          "Common patterns: \\d (digit), \\s (whitespace), \\w (word char), "
          "\\. (literal dot), \\b (word boundary).")
        tip("Email example: [a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}")

        h2("BROWSE TAB")
        p("Select a table from the drop-down to view its rows. Features include:\n"
          "- Click column headers to sort ascending/descending.\n"
          "- Filter rows by typing in the filter box; select a specific column or All Columns.\n"
          "- Export current page to CSV or export all BLOBs to a folder.\n"
          "- Click a row to see a preview; double-click to open the full row detail.")

        h2("SCHEMA SIDEBAR")
        p("The left sidebar shows the database schema: tables, views, indexes, and triggers. "
          "Expand a table to see its columns, indexes, and foreign keys. "
          "Right-click a table for quick actions: browse, copy CREATE SQL, copy schema, "
          "or set it as the search scope. Use the filter box and Cols checkbox to "
          "search table and column names.")

        h2("KEYBOARD SHORTCUTS")
        p("Ctrl+O  Open a database file\n"
          "Ctrl+F  Focus the search entry\n"
          "Escape  Cancel a running search\n"
          "Enter   Start search (when search entry is focused)")

        h2("TIPS FOR EFFICIENT USAGE")
        p("- Use Scope to limit searches to relevant tables for faster results.\n"
          "- Increase the Max/table limit for thorough searches; decrease it for quick scans.\n"
          "- Right-click tables in the schema sidebar to quickly search or browse them.\n"
          "- Copy Schema includes PRIMARY KEY, NOT NULL, DEFAULT, UNIQUE constraints.\n"
          "- Row Detail values are selectable (click and Ctrl+C to copy any value).\n"
          "- Install Pillow (pip install Pillow) for JPEG and WEBP image previews in BLOB viewer.")

        h2("ABOUT")
        p(f"SQLite GUI Analyzer v{VERSION}\n"
          f"Python {sys.version.split()[0]}\n"
          f"Pillow: {'Installed' if HAS_PIL else 'Not installed (optional, for JPEG/WEBP images)'}")

        txt.configure(state="disabled")

        btnf = ttk.Frame(self)
        btnf.pack(fill="x", padx=8, pady=8)

        def copy_help():
            txt.configure(state="normal")
            self.clipboard_clear()
            self.clipboard_append(txt.get("1.0", "end"))
            txt.configure(state="disabled")

        ttk.Button(btnf, text="Copy", command=copy_help).pack(side="left", padx=4)
        ttk.Button(btnf, text="Close", command=self.destroy).pack(side="right", padx=4)


# ── ScopeDlg ─────────────────────────────────────────────────────────────
class ScopeDlg(tk.Toplevel):
    def __init__(self, parent, tables, counts, selected):
        super().__init__(parent)
        self.title("Search Scope")
        self.geometry("450x550")
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.grab_set()
        self.result = None
        self._tables = tables
        self._counts = counts
        self._vars = {}
        self._visible_tables = []

        # Filter row
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))

        self._filter_var = tk.StringVar()
        fe = ttk.Entry(top, textvariable=self._filter_var, width=25)
        fe.pack(side="left", padx=(0, 6))
        self._filter_var.trace_add("write", lambda *_: self._rebuild_list())

        self._hide_empty = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Hide empty", variable=self._hide_empty,
                         command=self._rebuild_list).pack(side="left")

        # Buttons - operate on VISIBLE tables only
        btnrow = ttk.Frame(self)
        btnrow.pack(fill="x", padx=8, pady=4)
        ttk.Button(btnrow, text="All Visible", command=self._sel_all, style="Sm.TButton").pack(side="left", padx=2)
        ttk.Button(btnrow, text="None Visible", command=self._sel_none, style="Sm.TButton").pack(side="left", padx=2)
        ttk.Button(btnrow, text="Invert", command=self._sel_invert, style="Sm.TButton").pack(side="left", padx=2)
        ttk.Button(btnrow, text="Non-Empty", command=self._sel_nonempty, style="Sm.TButton").pack(side="left", padx=2)

        # Stats
        empty_count = sum(1 for t in tables if self._get_count(t) == 0)
        nonempty_count = len(tables) - empty_count
        self._stats_label = ttk.Label(
            self, text=f"Total: {len(tables)} tables  |  {nonempty_count} non-empty  |  {empty_count} empty",
            style="M.TLabel")
        self._stats_label.pack(fill="x", padx=8, pady=(0, 2))

        # Scrollable checkbox list
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=8, pady=4)
        self._canvas = tk.Canvas(container, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas_win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._canvas_win, width=e.width))
        def _scope_scroll(e):
            try:
                self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        self._scope_scroll_fn = _scope_scroll
        self._canvas.bind("<MouseWheel>", _scope_scroll)
        self._inner.bind("<MouseWheel>", _scope_scroll)

        # Create vars for ALL tables (persist across rebuilds)
        for t in tables:
            self._vars[t] = tk.BooleanVar(value=(t in selected))

        self._summary = ttk.Label(self, style="M.TLabel")
        self._summary.pack(fill="x", padx=8)

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=8)
        ttk.Button(bot, text="Apply", style="P.TButton", command=self._apply).pack(side="right", padx=4)
        ttk.Button(bot, text="Cancel", command=self.destroy).pack(side="right", padx=4)

        self._rebuild_list()

    def _get_count(self, t):
        return _int_count(self._counts.get(t, 0))

    def _rebuild_list(self):
        """Destroy and recreate checkbuttons based on filter + hide-empty."""
        for w in self._inner.winfo_children():
            w.destroy()
        filt = self._filter_var.get().lower().strip()
        he = self._hide_empty.get()
        self._visible_tables = []
        for t in self._tables:
            if filt and filt not in t.lower():
                continue
            cnt = self._get_count(t)
            if he and cnt == 0:
                continue
            self._visible_tables.append(t)
        for t in self._visible_tables:
            cnt = self._get_count(t)
            cnt_str = fmt_count(cnt)
            cb = ttk.Checkbutton(self._inner, text=f"{t}  ({cnt_str})",
                                  variable=self._vars[t], command=self._update_summary)
            cb.pack(anchor="w", pady=1, padx=4)
            cb.bind("<MouseWheel>", self._scope_scroll_fn)
        self._update_summary()

    def _sel_all(self):
        for t in self._visible_tables:
            self._vars[t].set(True)
        self._update_summary()

    def _sel_none(self):
        for t in self._visible_tables:
            self._vars[t].set(False)
        self._update_summary()

    def _sel_invert(self):
        for t in self._visible_tables:
            self._vars[t].set(not self._vars[t].get())
        self._update_summary()

    def _sel_nonempty(self):
        for t in self._visible_tables:
            self._vars[t].set(self._get_count(t) > 0)
        self._update_summary()

    def _update_summary(self):
        sel = sum(1 for v in self._vars.values() if v.get())
        total = len(self._vars)
        vis = len(self._visible_tables)
        vis_sel = sum(1 for t in self._visible_tables if self._vars[t].get())
        self._summary.configure(
            text=f"{vis_sel} of {vis} visible selected  |  {sel} of {total} total selected")

    def _apply(self):
        self.result = [t for t, v in self._vars.items() if v.get()]
        self.destroy()


# ── BlobViewer ───────────────────────────────────────────────────────────
class BlobViewer(tk.Toplevel):
    def __init__(self, parent, data, col_name="BLOB"):
        super().__init__(parent)
        self.title(f"BLOB Viewer - {col_name} ({fmtb(len(data))})")
        self.geometry("700x550")
        self.configure(bg=C["bg"])
        self._data = data
        self._pil_img = None
        self._tk_img = None
        self._zoom = 1.0

        # Pack buttons FIRST at bottom so they're always visible
        btnf = ttk.Frame(self)
        btnf.pack(side="bottom", fill="x", padx=8, pady=6)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        # Hex tab
        hex_frame = ttk.Frame(nb)
        nb.add(hex_frame, text="Hex Dump")
        hex_txt = tk.Text(hex_frame, font=("Consolas", 9), wrap="none", bg=C["bg2"])
        hex_sb = ttk.Scrollbar(hex_frame, orient="vertical", command=hex_txt.yview)
        hex_txt.configure(yscrollcommand=hex_sb.set)
        hex_sb.pack(side="right", fill="y")
        hex_txt.pack(fill="both", expand=True)
        self._fill_hex(hex_txt, data[:16384])

        # Text tab
        txt_frame = ttk.Frame(nb)
        nb.add(txt_frame, text="Text View")
        txt_txt = tk.Text(txt_frame, font=("Consolas", 9), wrap="word", bg=C["bg2"])
        txt_sb = ttk.Scrollbar(txt_frame, orient="vertical", command=txt_txt.yview)
        txt_txt.configure(yscrollcommand=txt_sb.set)
        txt_sb.pack(side="right", fill="y")
        txt_txt.pack(fill="both", expand=True)
        try:
            decoded = data[:65536].decode("utf-8", errors="replace")
        except Exception:
            decoded = "(cannot decode)"
        txt_txt.insert("1.0", decoded)
        txt_txt.configure(state="disabled")

        # Image tab
        if is_image(data):
            img_frame = ttk.Frame(nb)
            nb.add(img_frame, text="Image Preview")
            self._setup_image_tab(img_frame, data)

        def save_blob():
            bt = blob_type(data)
            ext = _EXT_MAP.get(bt, ".bin")
            path = filedialog.asksaveasfilename(defaultextension=ext,
                                                 filetypes=[("All files", "*.*")])
            if path:
                try:
                    with open(path, "wb") as f:
                        f.write(data)
                except Exception as e:
                    messagebox.showerror("Error", str(e))

        def copy_hex():
            self.clipboard_clear()
            self.clipboard_append(binascii.hexlify(data).decode())

        def copy_b64():
            import base64
            self.clipboard_clear()
            self.clipboard_append(base64.b64encode(data).decode())

        ttk.Button(btnf, text="Save", command=save_blob).pack(side="left", padx=4)
        ttk.Button(btnf, text="Copy Hex", command=copy_hex).pack(side="left", padx=4)
        ttk.Button(btnf, text="Copy Base64", command=copy_b64).pack(side="left", padx=4)
        ttk.Button(btnf, text="Close", command=self.destroy).pack(side="right", padx=4)

    def _fill_hex(self, txt, data):
        lines = []
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{i:08x}  {hex_part:<48s}  {ascii_part}")
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")

    def _setup_image_tab(self, frame, data):
        if HAS_PIL:
            try:
                self._pil_img = PILImage.open(io.BytesIO(data))
                info_bar = ttk.Frame(frame)
                info_bar.pack(fill="x", padx=4, pady=2)
                fmt = self._pil_img.format or "Unknown"
                w, h = self._pil_img.size
                mode = self._pil_img.mode
                ttk.Label(info_bar, text=f"Format: {fmt}  |  Size: {w}x{h}  |  Mode: {mode}",
                          style="M.TLabel").pack(side="left")

                ctrl = ttk.Frame(frame)
                ctrl.pack(fill="x", padx=4, pady=2)
                ttk.Button(ctrl, text="Fit", style="Sm.TButton",
                           command=lambda: self._zoom_fit()).pack(side="left", padx=2)
                ttk.Button(ctrl, text="100%", style="Sm.TButton",
                           command=lambda: self._set_zoom(1.0)).pack(side="left", padx=2)
                ttk.Button(ctrl, text="Zoom +", style="Sm.TButton",
                           command=lambda: self._set_zoom(self._zoom * 1.25)).pack(side="left", padx=2)
                ttk.Button(ctrl, text="Zoom -", style="Sm.TButton",
                           command=lambda: self._set_zoom(self._zoom / 1.25)).pack(side="left", padx=2)
                self._zoom_lbl = ttk.Label(ctrl, text="100%", style="M.TLabel")
                self._zoom_lbl.pack(side="left", padx=8)

                cvs_frame = ttk.Frame(frame)
                cvs_frame.pack(fill="both", expand=True)
                self._img_canvas = tk.Canvas(cvs_frame, bg=C["bg3"], highlightthickness=0)
                xsb = ttk.Scrollbar(cvs_frame, orient="horizontal", command=self._img_canvas.xview)
                ysb = ttk.Scrollbar(cvs_frame, orient="vertical", command=self._img_canvas.yview)
                self._img_canvas.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
                ysb.pack(side="right", fill="y")
                xsb.pack(side="bottom", fill="x")
                self._img_canvas.pack(fill="both", expand=True)
                self._img_canvas.bind("<MouseWheel>",
                    lambda e: self._set_zoom(self._zoom * (1.1 if e.delta > 0 else 0.9)))

                self.after(100, self._zoom_fit)
            except Exception as e:
                ttk.Label(frame, text=f"Cannot load image: {e}").pack(padx=20, pady=20)
        else:
            # Check for PNG/GIF for basic tk.PhotoImage
            if data[:8] == b'\x89PNG\r\n\x1a\n' or data[:4] in (b'GIF8',):
                try:
                    self._tk_img = tk.PhotoImage(data=data)
                    lbl = ttk.Label(frame, image=self._tk_img)
                    lbl.pack(padx=10, pady=10)
                except Exception:
                    ttk.Label(frame, text="Install Pillow for full image support:\npip install Pillow",
                              foreground=C["text2"]).pack(padx=20, pady=20)
            else:
                ttk.Label(frame, text="Install Pillow for JPEG/WEBP image support:\npip install Pillow",
                          foreground=C["text2"]).pack(padx=20, pady=20)

    def _zoom_fit(self):
        if not self._pil_img:
            return
        self._img_canvas.update_idletasks()
        cw = max(self._img_canvas.winfo_width(), 100)
        ch = max(self._img_canvas.winfo_height(), 100)
        iw, ih = self._pil_img.size
        z = min(cw / iw, ch / ih, 1.0)
        self._set_zoom(z)

    def _set_zoom(self, z):
        if not self._pil_img:
            return
        self._zoom = max(0.05, min(z, 10.0))
        iw, ih = self._pil_img.size
        nw = max(1, int(iw * self._zoom))
        nh = max(1, int(ih * self._zoom))
        resized = self._pil_img.resize((nw, nh), PILImage.LANCZOS if hasattr(PILImage, 'LANCZOS') else PILImage.BILINEAR)
        self._tk_img = ImageTk.PhotoImage(resized)
        self._img_canvas.delete("all")
        self._img_canvas.create_image(0, 0, anchor="nw", image=self._tk_img)
        self._img_canvas.configure(scrollregion=(0, 0, nw, nh))
        self._zoom_lbl.configure(text=f"{int(self._zoom * 100)}%")


# ── RowWin ───────────────────────────────────────────────────────────────
class RowWin(tk.Toplevel):
    _pool = {}

    @classmethod
    def show(cls, parent, db, tbl, rid, search_term="", match_col=""):
        key = (tbl, rid)
        if key in cls._pool:
            try:
                w = cls._pool[key]
                w._search_term = search_term
                w._match_col = match_col
                w.lift()
                # Re-highlight if search term changed
                if search_term:
                    w._highlight_match()
                return w
            except Exception:
                pass
        w = cls(parent, db, tbl, rid, search_term, match_col)
        cls._pool[key] = w
        return w

    def __init__(self, parent, db, tbl, rid, search_term="", match_col=""):
        super().__init__(parent)
        self.title(f"{tbl} [rowid={rid}]")
        self.geometry("720x500")
        self.configure(bg=C["bg"])
        self._db = db
        self._tbl = tbl
        self._rid = rid
        self._search_term = search_term
        self._match_col = match_col
        self._tk_imgs = []
        self._col_widgets = {}  # col_name -> (row_frame, value_widget)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Bottom toolbar — grouped, styled buttons
        bot = tk.Frame(self, bg=C["bg3"], bd=0)
        bot.pack(side="bottom", fill="x")
        tk.Frame(bot, bg=C["border"], height=1).pack(fill="x", side="top")

        bot_inner = tk.Frame(bot, bg=C["bg3"])
        bot_inner.pack(fill="x", padx=6, pady=4)

        btn_cfg = dict(font=("Segoe UI", 8), relief="flat", bd=0,
                       cursor="hand2", padx=8, pady=3)

        # Copy group
        grp1 = tk.Frame(bot_inner, bg=C["bg3"])
        grp1.pack(side="left")
        tk.Label(grp1, text="Copy:", font=("Segoe UI", 7), fg=C["text2"],
                 bg=C["bg3"]).pack(side="left", padx=(0, 2))
        for txt, cmd in [("JSON", self._copy_json), ("CSV", self._copy_csv),
                         ("Text", self._copy_text)]:
            b = tk.Button(grp1, text=txt, command=cmd, bg=C["bg"], fg=C["accent"],
                          activebackground=C["acl"], **btn_cfg)
            b.pack(side="left", padx=1)

        # Separator
        tk.Frame(bot_inner, bg=C["border"], width=1).pack(side="left", fill="y", padx=6, pady=2)

        # Schema group
        grp2 = tk.Frame(bot_inner, bg=C["bg3"])
        grp2.pack(side="left")
        tk.Label(grp2, text="Schema:", font=("Segoe UI", 7), fg=C["text2"],
                 bg=C["bg3"]).pack(side="left", padx=(0, 2))
        for txt, cmd in [("CREATE SQL", self._copy_create_sql), ("Schema", self._copy_schema_text)]:
            b = tk.Button(grp2, text=txt, command=cmd, bg=C["bg"], fg=C["purple"],
                          activebackground=C["bg2"], **btn_cfg)
            b.pack(side="left", padx=1)

        # Separator
        tk.Frame(bot_inner, bg=C["border"], width=1).pack(side="left", fill="y", padx=6, pady=2)

        # Export group
        b = tk.Button(bot_inner, text="Export BLOBs", command=self._export_blobs,
                      bg=C["bg"], fg=C["green"], activebackground=C["gl"], **btn_cfg)
        b.pack(side="left", padx=1)

        # Close — right side
        tk.Button(bot_inner, text="Close", command=self._on_close,
                  bg=C["bg4"], fg=C["text"], activebackground=C["border"],
                  **btn_cfg).pack(side="right", padx=1)

        # Scrollable body
        outer = tk.Frame(self, bg=C["bg"])
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self._body = tk.Frame(canvas, bg=C["bg"])
        self._body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._canvas_win = canvas.create_window((0, 0), window=self._body, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Configure>", lambda e, c=canvas: c.itemconfigure(self._canvas_win, width=e.width))
        self._rw_canvas = canvas
        def _rw_scroll(e):
            try:
                canvas.yview_scroll(int(-3 * (e.delta / 120)), "units")
            except Exception:
                pass
            return "break"
        self._scroll_fn = _rw_scroll

        self._populate()

    def _populate(self):
        data, cols = self._db.full_row(self._tbl, self._rid)
        if not data:
            tk.Label(self._body, text="Row not found", fg=C["red"], bg=C["bg"]).pack(padx=6, pady=6)
            return
        self._row_data = data
        self._row_cols = cols
        for i, col in enumerate(cols):
            val = data.get(col)
            bg = C["bg"] if i % 2 == 0 else C["alt"]
            row_f = tk.Frame(self._body, bg=bg, bd=0)
            row_f.pack(fill="x", padx=0, pady=0)
            row_f.columnconfigure(1, weight=1)

            # Column name - full name, no truncation
            col_lbl = tk.Label(row_f, text=col, font=("Consolas", 8, "bold"),
                     fg=C["accent"], bg=bg, anchor="nw")
            col_lbl.grid(row=0, column=0, sticky="nw", padx=(4, 2), pady=1)

            # Value
            vf = tk.Frame(row_f, bg=bg)
            vf.grid(row=0, column=1, sticky="nsew", padx=(0, 2), pady=1)
            val_widget = None  # Track widget for highlighting

            if val is None:
                tk.Label(vf, text="NULL", fg=C["text2"], bg=bg,
                         font=("Segoe UI", 9, "italic")).pack(side="left")
            elif isinstance(val, bytes):
                bt = blob_type(val)
                tk.Label(vf, text=f"{bt} ({fmtb(len(val))})",
                         fg=C["orange"], bg=bg, font=("Segoe UI", 8, "bold")).pack(side="left")
                tk.Button(vf, text="View", font=("Segoe UI", 7), padx=2, pady=0,
                          command=lambda v=val, c=col: BlobViewer(self, v, c)).pack(side="left", padx=2)
                tk.Button(vf, text="Export", font=("Segoe UI", 7), padx=2, pady=0,
                          command=lambda v=val, c=col: self._export_single(v, c)).pack(side="left", padx=2)
                if is_image(val) and HAS_PIL:
                    try:
                        pimg = PILImage.open(io.BytesIO(val))
                        pimg.thumbnail((80, 80))
                        tkimg = ImageTk.PhotoImage(pimg)
                        self._tk_imgs.append(tkimg)
                        tk.Label(vf, image=tkimg, bg=bg).pack(side="left", padx=4)
                    except Exception:
                        pass
            else:
                sv = str(val)
                is_multiline = '\n' in sv or '\r' in sv
                if is_multiline or len(sv) > 300:
                    # Multi-line or long text: Text widget with scrollbar
                    txt_frame = tk.Frame(vf, bg=bg)
                    txt_frame.pack(fill="x", expand=True)
                    line_count = sv.count('\n') + 1
                    h = min(8, max(2, line_count)) if is_multiline else min(6, max(2, len(sv) // 80))
                    t = tk.Text(txt_frame, height=h, wrap="word",
                                font=("Consolas", 9), bg=bg, relief="groove", bd=1)
                    tsb = ttk.Scrollbar(txt_frame, orient="vertical", command=t.yview)
                    t.configure(yscrollcommand=tsb.set)
                    t.insert("1.0", sv)
                    # Read-only but selectable: block keys except Ctrl+C, Ctrl+A, arrows
                    t.bind("<Key>", lambda e: None if (e.state & 4 and e.keysym.lower() in ('c', 'a')) else "break")
                    tsb.pack(side="right", fill="y")
                    t.pack(side="left", fill="both", expand=True)
                    val_widget = t
                else:
                    # Short single-line text: Entry widget (selectable, copyable, read-only)
                    e = tk.Entry(vf, font=("Segoe UI", 9), bg=bg, fg=C["text"],
                                 relief="flat", bd=0, readonlybackground=bg)
                    e.insert(0, sv)
                    e.configure(state="readonly")
                    e.pack(side="left", fill="x", expand=True)
                    val_widget = e
                if isinstance(val, (int, float)):
                    ts = try_decode_timestamp(val)
                    if ts:
                        for fmt_name, decoded in ts:
                            tk.Label(vf, text=f"{fmt_name}: {decoded}",
                                     fg=C["green"], bg=bg, font=("Segoe UI", 7)).pack(side="left", padx=4)

            # Copy button
            cpb = tk.Button(row_f, text="Copy", font=("Segoe UI", 7, "bold"),
                            fg=C["accent"], bg=C["bg2"], activebackground=C["acl"],
                            relief="flat", bd=0, cursor="hand2", padx=4, pady=1,
                            command=lambda v=val: self._copy_val(v))
            cpb.grid(row=0, column=2, sticky="ne", padx=2, pady=1)

            # Track widget for search highlight
            self._col_widgets[col] = (row_f, col_lbl, val_widget)

        # Bind mousewheel to ALL widgets in this window for smooth scrolling
        def _bind_scroll_all(w):
            w.bind("<MouseWheel>", self._scroll_fn)
            for child in w.winfo_children():
                _bind_scroll_all(child)
        _bind_scroll_all(self)

        # Highlight search match if opened from search results
        if self._search_term and self._match_col:
            self.after(150, self._highlight_match)

    def _highlight_match(self):
        """Scroll to and highlight the matched column/term from search."""
        col = self._match_col
        term = self._search_term
        if not col or not term or col not in self._col_widgets:
            return
        row_f, col_lbl, val_widget = self._col_widgets[col]

        # Highlight column name label with accent background
        col_lbl.configure(bg=C["hl"], fg=C["hbg"])

        # Scroll canvas so matched column row is visible
        self._body.update_idletasks()
        try:
            # Get position of row_f within the canvas scrollregion
            ry = row_f.winfo_y()
            canvas_h = self._rw_canvas.winfo_height()
            scroll_h = self._body.winfo_reqheight()
            if scroll_h > canvas_h:
                # Scroll so row is near top (with small offset)
                frac = max(0.0, min(1.0, (ry - 30) / scroll_h))
                self._rw_canvas.yview_moveto(frac)
        except Exception:
            pass

        # Highlight search term within the value widget
        if val_widget is None:
            return
        if isinstance(val_widget, tk.Text):
            # Tag-based highlight in Text widget
            val_widget.tag_configure("search_hl", background="#ff6b00", foreground="white")
            content = val_widget.get("1.0", "end-1c")
            tl = term.lower()
            cl = content.lower()
            start_idx = 0
            first_pos = None
            while True:
                pos = cl.find(tl, start_idx)
                if pos == -1:
                    break
                # Convert char offset to Text index
                line = content[:pos].count('\n') + 1
                col_off = pos - content[:pos].rfind('\n') - 1
                end_pos = pos + len(term)
                end_line = content[:end_pos].count('\n') + 1
                end_col = end_pos - content[:end_pos].rfind('\n') - 1
                tag_start = f"{line}.{col_off}"
                tag_end = f"{end_line}.{end_col}"
                val_widget.tag_add("search_hl", tag_start, tag_end)
                if first_pos is None:
                    first_pos = tag_start
                start_idx = pos + 1
            # Scroll Text widget to first match
            if first_pos:
                val_widget.see(first_pos)
        elif isinstance(val_widget, tk.Entry):
            # Entry widget: select the matched text
            content = val_widget.get()
            pos = content.lower().find(term.lower())
            if pos >= 0:
                val_widget.configure(state="normal")
                val_widget.selection_range(pos, pos + len(term))
                val_widget.icursor(pos)
                val_widget.xview(pos)
                val_widget.configure(state="readonly")

    def _copy_val(self, v):
        self.clipboard_clear()
        if isinstance(v, bytes):
            self.clipboard_append(binascii.hexlify(v).decode())
        elif v is None:
            self.clipboard_append("NULL")
        else:
            self.clipboard_append(str(v))

    def _copy_create_sql(self):
        sql = self._db.create_sql(self._tbl)
        if sql:
            self.clipboard_clear()
            self.clipboard_append(sql)
            self._flash("Copied CREATE SQL")
        else:
            messagebox.showinfo("Info", "No CREATE SQL found")

    def _copy_schema_text(self):
        self.clipboard_clear()
        self.clipboard_append(_build_schema_text(self._db, self._tbl))
        self._flash("Copied Schema")

    def _export_single(self, data, col):
        bt = blob_type(data)
        ext = _EXT_MAP.get(bt, ".bin")
        path = filedialog.asksaveasfilename(defaultextension=ext,
                                             initialfile=f"{self._tbl}_{col}{ext}")
        if path:
            try:
                with open(path, "wb") as f:
                    f.write(data)
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _flash(self, msg):
        """Show brief confirmation in title bar, then restore."""
        orig = self.title()
        self.title(msg)
        self.after(1500, lambda: self.title(orig))

    def _copy_json(self):
        self.clipboard_clear()
        d = {}
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes):
                d[c] = f"[BLOB {fmtb(len(v))}]"
            else:
                d[c] = v
        self.clipboard_append(json.dumps(d, indent=2, default=str))
        self._flash("Copied JSON")

    def _copy_csv(self):
        self.clipboard_clear()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(self._row_cols)
        vals = []
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes):
                vals.append(f"[BLOB {fmtb(len(v))}]")
            elif v is None:
                vals.append("")
            else:
                vals.append(str(v))
        w.writerow(vals)
        self.clipboard_append(out.getvalue())
        self._flash("Copied CSV")

    def _copy_text(self):
        self.clipboard_clear()
        lines = []
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes):
                lines.append(f"{c}: [BLOB {fmtb(len(v))}]")
            elif v is None:
                lines.append(f"{c}: NULL")
            else:
                lines.append(f"{c}: {v}")
        self.clipboard_append("\n".join(lines))
        self._flash("Copied Text")

    def _export_blobs(self):
        folder = filedialog.askdirectory(title="Select folder for BLOBs")
        if not folder:
            return
        count = 0
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes) and len(v) > 0:
                bt = blob_type(v)
                ext = _EXT_MAP.get(bt, ".bin")
                fname = f"{self._tbl}_r{self._rid}_{c}{ext}"
                try:
                    with open(os.path.join(folder, fname), "wb") as f:
                        f.write(v)
                    count += 1
                except Exception:
                    pass
        if count > 0:
            self._flash(f"Exported {count} BLOB(s)")
        else:
            messagebox.showinfo("Export", "No BLOBs found in this row")

    def _on_close(self):
        key = (self._tbl, self._rid)
        if key in RowWin._pool:
            del RowWin._pool[key]
        self.destroy()


# ── Main Application ─────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self, initial_path=None):
        super().__init__()
        self.title("SQLite GUI Analyzer v" + VERSION)
        self.geometry("1200x750")
        self.configure(bg=C["bg"])
        self.minsize(900, 500)
        # Start maximized
        try:
            self.state('zoomed')  # Windows/macOS
        except tk.TclError:
            try:
                self.attributes('-zoomed', True)  # Linux
            except tk.TclError:
                pass

        setup_theme(self)
        self._set_app_icon()
        self.db = DB()
        self._count_cache = {}
        self._search_cancel = False
        self._search_thread = None
        self._count_cancel = False
        self._scope_tables = []
        self._browse_cache_data = []
        self._browse_cache_cols = []
        self._browse_sort_col = None
        self._browse_sort_dir = "ASC"
        self._browse_offset = 0
        self._browse_filter_after = None
        self._schema_filter_after = None
        self._load_time = 0
        self._measure_font = None
        self._measure_bold_font = None

        self._build_header()
        self._build_body()
        self._bind_keys()
        self._setup_tooltips()

        if initial_path and os.path.isfile(initial_path):
            self.after(200, lambda: self._open_db(initial_path))

    def _set_app_icon(self):
        """Generate and set a programmatic app icon (no external file needed)."""
        try:
            # Create a 32x32 icon image using a PhotoImage
            icon_size = 32
            img = tk.PhotoImage(width=icon_size, height=icon_size)
            # Draw a simple database cylinder icon with blue/white colors
            # Background: transparent
            # Blue database body
            for y in range(8, 26):
                for x in range(6, 26):
                    img.put("#3b82f6", (x, y))
            # Top ellipse (lighter blue)
            for x in range(6, 26):
                dx = x - 16
                if abs(dx) <= 10:
                    for dy in range(-3, 4):
                        ry = 8 + dy
                        if 0 <= ry < icon_size and (dx * dx / 100 + dy * dy / 9) <= 1:
                            img.put("#60a5fa", (x, ry))
            # Bottom ellipse
            for x in range(6, 26):
                dx = x - 16
                if abs(dx) <= 10:
                    for dy in range(-3, 4):
                        ry = 24 + dy
                        if 0 <= ry < icon_size and (dx * dx / 100 + dy * dy / 9) <= 1:
                            img.put("#2563eb", (x, ry))
            # Magnifying glass (white circle + handle)
            for x in range(18, 30):
                for y in range(16, 28):
                    dx = x - 23
                    dy = y - 21
                    r2 = dx * dx + dy * dy
                    if 16 <= r2 <= 36:
                        if 0 <= x < icon_size and 0 <= y < icon_size:
                            img.put("white", (x, y))
            # Handle
            for i in range(5):
                px = 27 + i
                py = 25 + i
                if 0 <= px < icon_size and 0 <= py < icon_size:
                    img.put("white", (px, py))
                    if px + 1 < icon_size:
                        img.put("white", (px + 1, py))
            self._app_icon = img  # prevent GC
            self.wm_iconphoto(True, img)
        except Exception:
            pass  # Icon is cosmetic; don't crash if it fails

    # ── Header ───────────────────────────────────────────────────────
    def _build_header(self):
        hf = ttk.Frame(self, style="H.TFrame")
        hf.pack(fill="x")
        inner = ttk.Frame(hf, style="H.TFrame")
        inner.pack(fill="x", padx=10, pady=6)

        # Canvas logo - larger and more prominent
        logo = tk.Canvas(inner, width=48, height=44, bg=C["hbg"], highlightthickness=0)
        logo.pack(side="left", padx=(0, 10))
        # Database cylinder - stacked disks
        logo.create_rectangle(8, 10, 36, 34, fill="#3b82f6", outline="")
        logo.create_oval(8, 4, 36, 16, fill="#60a5fa", outline="#2563eb", width=1.5)
        logo.create_oval(8, 14, 36, 24, outline="#93c5fd", width=1)
        logo.create_oval(8, 28, 36, 38, fill="#2563eb", outline="#1d4ed8", width=1.5)
        logo.create_line(8, 10, 8, 33, fill="#1d4ed8", width=1.5)
        logo.create_line(36, 10, 36, 33, fill="#1d4ed8", width=1.5)
        # Magnifying glass - prominent white
        logo.create_oval(24, 18, 40, 34, outline="white", width=2.5)
        logo.create_line(38, 32, 46, 42, fill="white", width=3, capstyle=tk.ROUND)

        ttk.Label(inner, text="  SQLite GUI Analyzer", style="H.TLabel").pack(side="left")

        self._db_info = ttk.Label(inner, text="  No database loaded", style="HI.TLabel")
        self._db_info.pack(side="left", padx=(16, 0))

        # Buttons right side
        ttk.Button(inner, text="Close DB", style="HB.TButton",
                   command=self._close_db).pack(side="right", padx=3)
        ttk.Button(inner, text="Help", style="HB.TButton",
                   command=lambda: HelpDialog(self)).pack(side="right", padx=3)
        ttk.Button(inner, text="Info", style="HB.TButton",
                   command=self._show_info).pack(side="right", padx=3)
        ttk.Button(inner, text="Open", style="HB.TButton",
                   command=self._open_file).pack(side="right", padx=3)

    # ── Body ─────────────────────────────────────────────────────────
    def _build_body(self):
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        # Sidebar container with collapse toggle
        self._sidebar_visible = True
        sb_outer = ttk.Frame(body)
        sb_outer.pack(side="left", fill="y")
        self._sb_outer = sb_outer

        # Toggle button
        self._sb_toggle = tk.Button(
            sb_outer, text="\u25C0", font=("Segoe UI", 8), width=2, bd=0,
            bg=C["sbg"], fg=C["text2"], activebackground=C["bg4"],
            command=self._toggle_sidebar, relief="flat", cursor="hand2")
        self._sb_toggle.pack(side="right", fill="y")

        self._sidebar = ttk.Frame(sb_outer, style="S.TFrame", width=270)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)
        self._build_sidebar()

        # Main area with notebook
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)

        self._nb = ttk.Notebook(main)
        self._nb.pack(fill="both", expand=True)

        self._search_frame = ttk.Frame(self._nb)
        self._browse_frame = ttk.Frame(self._nb)

        self._nb.add(self._search_frame, text="  Search  ")
        self._nb.add(self._browse_frame, text="  Browse  ")

        self._build_search_tab()
        self._build_browse_tab()

    # ── Sidebar ──────────────────────────────────────────────────────
    def _build_sidebar(self):
        sf = self._sidebar

        ttk.Label(sf, text="Schema", style="B.TLabel").pack(padx=8, pady=(8, 2), anchor="w")

        filt_f = ttk.Frame(sf, style="S.TFrame")
        filt_f.pack(fill="x", padx=8, pady=2)
        self._schema_filter_var = tk.StringVar()
        self._schema_filter_var.trace_add("write", self._on_schema_filter)
        ttk.Entry(filt_f, textvariable=self._schema_filter_var, width=18).pack(side="left", fill="x", expand=True)
        self._schema_cols_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(filt_f, text="Cols", variable=self._schema_cols_var,
                         command=lambda: self._on_schema_filter()).pack(side="left", padx=4)

        tree_f = ttk.Frame(sf, style="S.TFrame")
        tree_f.pack(fill="both", expand=True, padx=4, pady=4)

        self._schema_tree = ttk.Treeview(tree_f, style="Sc.Treeview", show="tree", selectmode="browse")
        ssb_x = ttk.Scrollbar(tree_f, orient="horizontal", command=self._schema_tree.xview)
        ssb = ttk.Scrollbar(tree_f, orient="vertical", command=self._schema_tree.yview)
        self._schema_tree.configure(yscrollcommand=ssb.set, xscrollcommand=ssb_x.set)
        self._schema_tree.column("#0", minwidth=400, stretch=True)
        ssb.pack(side="right", fill="y")
        ssb_x.pack(side="bottom", fill="x")
        self._schema_tree.pack(fill="both", expand=True)
        self._schema_tree.bind("<<TreeviewOpen>>", self._on_schema_expand)
        self._schema_tree.bind("<Double-1>", self._on_schema_dblclick)
        self._schema_tree.bind("<Button-3>", self._on_schema_rightclick)

        # SQL preview
        self._sql_preview = tk.Text(sf, height=5, font=("Consolas", 8), bg=C["bg2"],
                                     fg=C["text"], wrap="word", state="disabled")
        self._sql_preview.pack(fill="x", padx=8, pady=(0, 4))

        btn_f = ttk.Frame(sf, style="S.TFrame")
        btn_f.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_f, text="Copy Schema", style="Sm.TButton",
                   command=self._copy_schema).pack(side="left", padx=2)
        ttk.Button(btn_f, text="Copy SQL", style="Sm.TButton",
                   command=self._copy_sql).pack(side="left", padx=2)

    def _populate_schema(self):
        tree = self._schema_tree
        tree.delete(*tree.get_children())
        if not self.db.ok:
            return
        tables = self.db.tables()
        self._scope_tables = list(tables)

        # Tables node
        tbl_node = tree.insert("", "end", text="Tables", open=True, tags=("header",))
        for t in tables:
            cnt = self._count_cache.get(t, "?")
            iid = tree.insert(tbl_node, "end", text=f"{t}  ({fmt_count(cnt)})", values=(t, "table"))
            tree.insert(iid, "end", text="loading...")  # placeholder

        # Views
        views = self.db.views()
        if views:
            v_node = tree.insert("", "end", text="Views", open=False, tags=("header",))
            for v in views:
                tree.insert(v_node, "end", text=v, values=(v, "view"))

        # Indexes
        indexes = self.db.all_indexes()
        if indexes:
            i_node = tree.insert("", "end", text="Indexes", open=False, tags=("header",))
            for idx in indexes:
                tree.insert(i_node, "end", text=idx, values=(idx, "index"))

        # Triggers
        triggers = self.db.triggers()
        if triggers:
            tr_node = tree.insert("", "end", text="Triggers", open=False, tags=("header",))
            for trg in triggers:
                tree.insert(tr_node, "end", text=trg, values=(trg, "trigger"))

    def _on_schema_expand(self, event):
        iid = self._schema_tree.focus()
        vals = self._schema_tree.item(iid, "values")
        if not vals or len(vals) < 2:
            return
        tbl, typ = vals[0], vals[1]
        if typ != "table":
            return
        children = self._schema_tree.get_children(iid)
        if len(children) == 1 and self._schema_tree.item(children[0], "text") == "loading...":
            self._schema_tree.delete(children[0])
            # Columns
            cols = self.db.columns(tbl)
            for cn, ct in cols:
                self._schema_tree.insert(iid, "end", text=f"  {cn}  ({ct})", values=(tbl, "column"))
            # Indexes
            idxs = self.db.indexes(tbl)
            for name, unique, icols in idxs:
                ustr = " UNIQUE" if unique else ""
                self._schema_tree.insert(iid, "end",
                    text=f"  IDX{ustr}: {name} ({', '.join(icols)})", values=(tbl, "index_detail"))
            # Foreign keys
            fks = self.db.fkeys(tbl)
            for ref_tbl, from_col, to_col in fks:
                self._schema_tree.insert(iid, "end",
                    text=f"  FK: {from_col} -> {ref_tbl}({to_col})", values=(tbl, "fk"))

        # Show CREATE SQL
        sql = self.db.create_sql(tbl)
        self._sql_preview.configure(state="normal")
        self._sql_preview.delete("1.0", "end")
        self._sql_preview.insert("1.0", sql)
        self._sql_preview.configure(state="disabled")

    def _on_schema_dblclick(self, event):
        iid = self._schema_tree.focus()
        vals = self._schema_tree.item(iid, "values")
        if not vals or len(vals) < 2:
            return
        tbl, typ = vals[0], vals[1]
        if typ in ("table", "column", "index_detail", "fk"):
            self._nb.select(self._browse_frame)
            self._browse_table_var.set(tbl)
            self._load_browse_table()

    def _on_schema_rightclick(self, event):
        iid = self._schema_tree.identify_row(event.y)
        if not iid:
            return
        self._schema_tree.selection_set(iid)
        vals = self._schema_tree.item(iid, "values")
        if not vals or len(vals) < 2:
            return
        tbl, typ = vals[0], vals[1]
        if typ not in ("table", "column", "index_detail", "fk"):
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"Browse '{tbl}'",
                         command=lambda: self._schema_ctx_browse(tbl))
        menu.add_command(label="Show 10 Rows",
                         command=lambda: self._schema_ctx_sample(tbl))
        menu.add_separator()
        menu.add_command(label="Copy CREATE SQL",
                         command=lambda: self._schema_ctx_copy_sql(tbl))
        menu.add_command(label="Copy Schema Text",
                         command=lambda: self._schema_ctx_copy_schema(tbl))
        menu.add_command(label="Copy Table Name",
                         command=lambda: self._schema_ctx_copy_name(tbl))
        menu.add_separator()
        menu.add_command(label="Export All Schema (HTML)",
                         command=self._schema_export_html)
        menu.add_separator()
        menu.add_command(label=f"Search in '{tbl}'",
                         command=lambda: self._schema_ctx_search(tbl))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _schema_ctx_browse(self, tbl):
        self._nb.select(self._browse_frame)
        self._browse_table_var.set(tbl)
        self._load_browse_table()

    def _schema_ctx_copy_sql(self, tbl):
        sql = self.db.create_sql(tbl)
        if sql:
            self.clipboard_clear()
            self.clipboard_append(sql)

    def _schema_ctx_copy_schema(self, tbl):
        self.clipboard_clear()
        self.clipboard_append(_build_schema_text(self.db, tbl, self._count_cache.get(tbl, "?")))

    def _schema_ctx_copy_name(self, tbl):
        self.clipboard_clear()
        self.clipboard_append(tbl)

    def _schema_ctx_sample(self, tbl):
        """Show 10 sample rows in a popup window."""
        cols, rows = self.db.browse(tbl, 10, 0)
        w = tk.Toplevel(self)
        w.title(f"Sample: {tbl} (first 10 rows)")
        w.geometry("900x400")
        w.configure(bg=C["bg"])
        # Tree
        tree = ttk.Treeview(w, columns=cols, show="headings", height=10)
        sb = ttk.Scrollbar(w, orient="vertical", command=tree.yview)
        sbh = ttk.Scrollbar(w, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=sb.set, xscrollcommand=sbh.set)
        sb.pack(side="right", fill="y")
        sbh.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=120, minwidth=60, anchor="w")
        for ri, row in enumerate(rows):
            tag = "odd" if ri % 2 else "even"
            tree.insert("", "end", values=[vb(v) for v in row], tags=(tag,))
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])
        # Bottom bar
        bot = tk.Frame(w, bg=C["bg3"])
        bot.pack(fill="x")
        tk.Label(bot, text=f"{len(rows)} rows  |  {len(cols)} columns",
                 font=("Segoe UI", 8), bg=C["bg3"], fg=C["text2"]).pack(side="left", padx=6, pady=3)
        tk.Button(bot, text="Open in Browse", font=("Segoe UI", 8),
                  command=lambda: (w.destroy(), self._schema_ctx_browse(tbl)),
                  relief="flat", bg=C["bg"], fg=C["accent"], padx=8).pack(side="right", padx=4, pady=3)
        tk.Button(bot, text="Close", font=("Segoe UI", 8),
                  command=w.destroy, relief="flat", bg=C["bg"], padx=8).pack(side="right", padx=4, pady=3)

    def _schema_export_html(self):
        """Export full schema report as HTML."""
        if not self.db.ok:
            return
        tables = self.db.tables()
        path = filedialog.asksaveasfilename(
            defaultextension=".html",
            initialfile="schema_report.html",
            filetypes=[("HTML", "*.html"), ("All", "*.*")])
        if not path:
            return
        try:
            html = _build_schema_html(self.db, tables, self._count_cache)
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            messagebox.showinfo("Exported", f"Schema report exported:\n{os.path.basename(path)}\n"
                                f"{len(tables)} tables documented.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _schema_ctx_search(self, tbl):
        self._scope_tables = [tbl]
        self._nb.select(self._search_frame)
        self._search_entry.focus_set()

    def _on_schema_filter(self, *args):
        if self._schema_filter_after:
            self.after_cancel(self._schema_filter_after)
        self._schema_filter_after = self.after(300, self._apply_schema_filter)

    def _apply_schema_filter(self):
        filt = self._schema_filter_var.get().lower().strip()
        search_cols = self._schema_cols_var.get()
        if not filt:
            self._populate_schema()
            return
        tree = self._schema_tree
        tree.delete(*tree.get_children())
        if not self.db.ok:
            return
        tables = self.db.tables()
        tbl_node = tree.insert("", "end", text=f"Matches", open=True)
        for t in tables:
            tbl_match = filt in t.lower()
            col_matches = []
            if search_cols:
                cols = self.db.columns(t)
                col_matches = [(cn, ct) for cn, ct in cols if filt in cn.lower()]
            if tbl_match or col_matches:
                cnt = self._count_cache.get(t, "?")
                iid = tree.insert(tbl_node, "end", text=f"{t}  ({fmt_count(cnt)})",
                                  values=(t, "table"), open=bool(col_matches))
                if col_matches:
                    # Show matching columns directly — no need to expand
                    for cn, ct in col_matches:
                        tree.insert(iid, "end",
                                    text=f"  >> {cn}  ({ct})",
                                    values=(t, "column"),
                                    tags=("col_match",))
                else:
                    tree.insert(iid, "end", text="loading...")
        tree.tag_configure("col_match", foreground=C["orange"])

    def _copy_schema(self):
        if not self.db.ok:
            return
        lines = []
        for t in self.db.tables():
            cols_full = self.db.columns_full(t)
            uniq = self.db.unique_columns(t)
            cnt = self._count_cache.get(t, "?")
            lines.append(f"TABLE: {t} ({fmt_count(cnt)} rows)")
            for cn, ct, notnull, default, pk in cols_full:
                flags = []
                if pk:
                    flags.append("PK")
                if notnull:
                    flags.append("NOT NULL")
                if default is not None:
                    flags.append(f"DEFAULT {default}")
                if cn in uniq:
                    flags.append("UNIQUE")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                lines.append(f"  {cn} ({ct or ''}){flag_str}")
            lines.append("")
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))

    def _copy_sql(self):
        self.clipboard_clear()
        self._sql_preview.configure(state="normal")
        txt = self._sql_preview.get("1.0", "end").strip()
        self._sql_preview.configure(state="disabled")
        self.clipboard_append(txt)

    # ── Search Tab ───────────────────────────────────────────────────
    def _build_search_tab(self):
        sf = self._search_frame

        # Top controls
        top = ttk.Frame(sf)
        top.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(top, text="Search:", style="B.TLabel").pack(side="left")
        self._search_var = tk.StringVar()
        self._search_entry = ttk.Entry(top, textvariable=self._search_var, width=40, style="SE.TEntry")
        self._search_entry.pack(side="left", padx=6, fill="x", expand=True)

        self._search_mode_var = tk.StringVar(value="Case-Insensitive")
        self._mode_combo = ttk.Combobox(top, textvariable=self._search_mode_var,
                                         values=list(SEARCH_MODES.keys()), state="readonly", width=16)
        self._mode_combo.pack(side="left", padx=4)
        self._mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)

        self._deep_blob_var = tk.BooleanVar(value=False)
        self._deep_blob_cb = ttk.Checkbutton(top, text="Deep BLOB", variable=self._deep_blob_var)
        self._deep_blob_cb.pack(side="left", padx=4)

        ttk.Label(top, text="Max/table:").pack(side="left", padx=(8, 2))
        self._limit_var = tk.StringVar(value="500")
        limit_combo = ttk.Combobox(top, textvariable=self._limit_var,
                                    values=["100", "500", "1000", "5000", "All"],
                                    state="readonly", width=6)
        limit_combo.pack(side="left")

        # Hint label
        self._hint_label = ttk.Label(sf, text="", style="M.TLabel", foreground=C["green"])
        self._hint_label.pack(fill="x", padx=16, pady=(0, 2))

        # Buttons row
        btn_row = ttk.Frame(sf)
        btn_row.pack(fill="x", padx=10, pady=2)

        self._search_btn = ttk.Button(btn_row, text="Search", style="P.TButton",
                   command=self._do_search)
        self._search_btn.pack(side="left", padx=3)
        self._stop_btn = ttk.Button(btn_row, text="Stop", style="D.TButton",
                   command=self._stop_search)
        self._stop_btn.pack(side="left", padx=3)
        self._scope_btn = ttk.Button(btn_row, text="Scope", command=self._show_scope)
        self._scope_btn.pack(side="left", padx=3)
        self._reset_scope_btn = ttk.Button(btn_row, text="Reset Scope", command=self._reset_scope)
        self._reset_scope_btn.pack(side="left", padx=3)

        ttk.Button(btn_row, text="Export CSV", command=self._search_export_csv).pack(side="right", padx=3)
        ttk.Button(btn_row, text="Export JSON", command=self._search_export_json).pack(side="right", padx=3)
        ttk.Button(btn_row, text="Copy Results", command=self._search_copy).pack(side="right", padx=3)

        self._search_err_btn = ttk.Button(btn_row, text="0 errors", style="Sm.TButton",
                                           command=self._show_search_errors)
        self._search_err_btn.pack(side="right", padx=3)

        # Progress
        prog_f = ttk.Frame(sf)
        prog_f.pack(fill="x", padx=10, pady=2)
        self._search_progress = ttk.Progressbar(prog_f, mode="determinate")
        self._search_progress.pack(fill="x", side="left", expand=True, padx=(0, 8))
        self._search_status = ttk.Label(prog_f, text="Ready", style="M.TLabel")
        self._search_status.pack(side="right")

        # (Search tip shown in the hint label above)

        # Compact filter + pagination bar
        self._sr_page = 0
        self._sr_page_size = 200
        self._sr_filtered = []
        self._search_start_time = time.time()

        fp_bar = ttk.Frame(sf)
        fp_bar.pack(fill="x", padx=10, pady=(1, 2))

        ttk.Label(fp_bar, text="Table:", font=("Segoe UI", 8)).pack(side="left")
        self._sr_table_filter = ttk.Combobox(fp_bar, values=["All"], state="readonly", width=32)
        self._sr_table_filter.set("All")
        self._sr_table_filter.pack(side="left", padx=(1, 4))
        self._sr_table_filter.bind("<<ComboboxSelected>>", lambda e: self._filter_search_results())
        # Auto-widen dropdown to fit longest entry
        self._sr_table_filter.configure(postcommand=lambda: self._auto_dropdown_width(self._sr_table_filter))

        ttk.Label(fp_bar, text="Col:", font=("Segoe UI", 8)).pack(side="left")
        self._sr_col_filter = ttk.Combobox(fp_bar, values=["All"], state="readonly", width=24)
        self._sr_col_filter.set("All")
        self._sr_col_filter.pack(side="left", padx=(1, 4))
        self._sr_col_filter.bind("<<ComboboxSelected>>", lambda e: self._filter_search_results())
        self._sr_col_filter.configure(postcommand=lambda: self._auto_dropdown_width(self._sr_col_filter))

        ttk.Label(fp_bar, text="Type:", font=("Segoe UI", 8)).pack(side="left")
        self._sr_type_filter = ttk.Combobox(fp_bar, values=["All"], state="readonly", width=8)
        self._sr_type_filter.set("All")
        self._sr_type_filter.pack(side="left", padx=(1, 6))
        self._sr_type_filter.bind("<<ComboboxSelected>>", lambda e: self._filter_search_results())

        ttk.Button(fp_bar, text="\u25C0", width=3, command=self._sr_prev_page).pack(side="left", padx=1)
        ttk.Button(fp_bar, text="\u25B6", width=3, command=self._sr_next_page).pack(side="left", padx=1)
        self._sr_page_label = ttk.Label(fp_bar, text="", font=("Segoe UI", 8))
        self._sr_page_label.pack(side="left", padx=4)

        # Results treeview in bordered frame
        border_frame = tk.Frame(sf, relief="solid", bd=1, bg=C["border"])
        border_frame.pack(fill="both", expand=True, padx=10, pady=(2, 10))

        cols = ("#", "Table", "Column", "RowID", "Matched Value", "Type")
        self._search_tree = ttk.Treeview(border_frame, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self._search_tree.heading(c, text=c)
        self._search_tree.column("#", width=50, minwidth=40, stretch=False)
        self._search_tree.column("Table", width=220, minwidth=120, stretch=True)
        self._search_tree.column("Column", width=170, minwidth=100, stretch=True)
        self._search_tree.column("RowID", width=70, minwidth=50, stretch=False)
        self._search_tree.column("Matched Value", width=400, minwidth=200, stretch=True)
        self._search_tree.column("Type", width=70, minwidth=50, stretch=False)

        xsb = ttk.Scrollbar(border_frame, orient="horizontal", command=self._search_tree.xview)
        ysb = ttk.Scrollbar(border_frame, orient="vertical", command=self._search_tree.yview)
        self._search_tree.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self._search_tree.pack(fill="both", expand=True)
        self._search_tree.bind("<Double-1>", self._on_search_dblclick)
        self._search_tree.bind("<Return>", self._on_search_dblclick)
        TreeviewTooltip(self._search_tree)

        self._search_results = []
        self._search_errors = []

    def _on_mode_change(self, event=None):
        mode = self._search_mode_var.get()
        mk = SEARCH_MODES.get(mode, "ci")
        if mk == "rx":
            self._hint_label.configure(
                text="Tip: Type \\. for literal dot (not \\\\.). Omit ^ and $ to find patterns within values.",
                foreground=C["green"])
        elif mk == "blob":
            self._hint_label.configure(
                text="Searches text in binary data. Enable 'Deep BLOB' for hex-level matching.",
                foreground=C["orange"])
        elif mk == "col":
            self._hint_label.configure(
                text="Finds columns whose name contains your search text.",
                foreground=C["purple"])
        else:
            self._hint_label.configure(text="")

    def _do_search(self):
        term = self._search_var.get()
        if not term or not self.db.ok:
            return
        self._stop_search()
        # Wait for previous search thread to fully stop (avoids connection contention)
        if self._search_thread and self._search_thread.is_alive():
            self._search_thread.join(timeout=3)
        self._search_cancel = False
        self._search_results = []
        self._search_errors = []
        self._search_tree.delete(*self._search_tree.get_children())
        self._search_err_btn.configure(text="0 errors")
        self._search_start_time = time.time()

        self._count_cancel = True  # Cancel bg counting to free connection

        mode = self._search_mode_var.get()
        self._search_term = term
        self._search_mode_key = SEARCH_MODES.get(mode, "ci")
        lv = self._limit_var.get()
        limit = 999999 if lv == "All" else int(lv)
        deep = self._deep_blob_var.get()
        tables = list(self._scope_tables) if self._scope_tables else self.db.tables()

        self._search_progress.configure(maximum=len(tables), value=0)
        self._search_status.configure(text=f"Searching {len(tables)} tables...")

        self._search_thread = threading.Thread(target=self._search_worker,
                                                args=(tables, term, mode, limit, deep), daemon=True)
        self._search_thread.start()

    def _search_worker(self, tables, term, mode, limit, deep):
        last_update = 0
        last_progress = 0
        count = 0
        self._search_table_counts = {}  # per-table result count
        for ti, tbl in enumerate(tables):
            if self._search_cancel:
                break
            cols = self.db.columns(tbl)
            tbl_count = 0
            try:
                for result in self.db.search(tbl, cols, term, mode, limit, deep,
                                              cancel=lambda: self._search_cancel):
                    if self._search_cancel:
                        break
                    count += 1
                    tbl_count += 1
                    self._search_results.append(result)
                    now = time.time()
                    if now - last_update >= 0.3:
                        last_update = now
                        self.after(0, self._update_search_ui, count, ti, len(tables))
            except Exception as e:
                self._search_errors.append(f"{tbl}: {e}")
            if tbl_count > 0:
                self._search_table_counts[tbl] = tbl_count
            now = time.time()
            if now - last_progress >= 0.3:
                last_progress = now
                self.after(0, lambda i=ti: self._search_progress.configure(value=i + 1))

        self.after(0, self._finalize_search, count, len(tables))

    def _update_search_ui(self, count, ti, total):
        elapsed = time.time() - self._search_start_time
        self._search_status.configure(text=f"Found {count}... ({ti + 1}/{total} tables, {elapsed:.1f}s)")
        self._search_err_btn.configure(text=f"{len(self._search_errors)} errors")
        # Progressive display: append only new results (no flicker)
        snap = list(self._search_results)
        self._sr_filtered = snap
        tree = self._search_tree
        existing = len(tree.get_children())
        page_size = self._sr_page_size
        term = getattr(self, '_search_term', '')
        mkey = getattr(self, '_search_mode_key', 'ci')
        # Only add items that are new and within page 0
        for i in range(existing, min(len(snap), page_size)):
            r = snap[i]
            tag = "odd" if i % 2 else "even"
            val = _snippet(r["value"], term, mkey) if term else r["value"]
            tree.insert("", "end", values=(i + 1, r["table"], r["column"],
                        r["rowid"], val, r["type"]), tags=(tag,))
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])
        total_results = len(snap)
        total_pages = max(1, (total_results + page_size - 1) // page_size)
        showing = min(total_results, page_size)
        self._sr_page_label.configure(
            text=f"Page 1 of {total_pages}  |  Showing {showing} of {total_results} results (searching...)")

    def _finalize_search(self, count, total):
        elapsed = time.time() - self._search_start_time
        status = "Cancelled" if self._search_cancel else "Complete"
        tc = getattr(self, '_search_table_counts', {})
        tables_hit = len(tc)
        self._search_status.configure(
            text=f"{status}: {count} results across {tables_hit} tables ({total} scanned, {elapsed:.1f}s)")
        self._search_err_btn.configure(text=f"{len(self._search_errors)} errors")
        self._search_progress.configure(value=total)
        # Update filter combos — table filter shows per-table counts
        tbl_labels = [f"{t} ({tc[t]})" for t in sorted(tc.keys())]
        columns = sorted(set(r["column"] for r in self._search_results))
        types = sorted(set(r["type"] for r in self._search_results))
        self._sr_table_filter.configure(values=["All"] + tbl_labels)
        self._sr_col_filter.configure(values=["All"] + columns)
        self._sr_type_filter.configure(values=["All"] + types)
        # Dynamically size combobox width to fit longest entry
        max_tbl = max((len(l) for l in tbl_labels), default=5)
        self._sr_table_filter.configure(width=max(32, min(max_tbl + 2, 55)))
        max_col = max((len(c) for c in columns), default=5)
        self._sr_col_filter.configure(width=max(24, min(max_col + 2, 45)))
        self._sr_table_filter.set("All")
        self._sr_col_filter.set("All")
        self._sr_type_filter.set("All")
        # Auto-resize treeview Table/Column columns to fit longest name
        # Use tkfont to measure actual pixel width for accuracy
        if not self._measure_font:
            self._measure_font = tkfont.nametofont("TkDefaultFont")
        mf = self._measure_font
        tbl_names = sorted(tc.keys()) if tc else []
        if tbl_names:
            longest_tbl_px = max(mf.measure(t) for t in tbl_names)
            tbl_px = max(220, min(longest_tbl_px + 30, 500))
            self._search_tree.column("Table", width=tbl_px)
        if columns:
            longest_col_px = max(mf.measure(c) for c in columns)
            col_px = max(170, min(longest_col_px + 30, 400))
            self._search_tree.column("Column", width=col_px)
        # Display first page
        self._sr_filtered = list(self._search_results)
        self._sr_page = 0
        self._display_search_page()

    @staticmethod
    def _auto_dropdown_width(combo):
        """Widen combobox dropdown popup to fit the longest entry."""
        try:
            pd = combo.tk.call('ttk::combobox::PopdownWindow', str(combo))
            combo.tk.call(pd + '.f.l', 'configure', '-width', 0)
        except Exception:
            pass

    def _filter_search_results(self):
        tbl_f = self._sr_table_filter.get()
        col_f = self._sr_col_filter.get()
        type_f = self._sr_type_filter.get()
        filtered = self._search_results
        if tbl_f != "All":
            # Strip count suffix: "table_name (42)" -> "table_name"
            tbl_name = tbl_f.rsplit(" (", 1)[0] if " (" in tbl_f else tbl_f
            filtered = [r for r in filtered if r["table"] == tbl_name]
        if col_f != "All":
            filtered = [r for r in filtered if r["column"] == col_f]
        if type_f != "All":
            filtered = [r for r in filtered if r["type"] == type_f]
        self._sr_filtered = filtered
        self._sr_page = 0
        self._display_search_page()

    def _display_search_page(self):
        tree = self._search_tree
        tree.delete(*tree.get_children())
        start = self._sr_page * self._sr_page_size
        end = start + self._sr_page_size
        page_data = self._sr_filtered[start:end]
        term = getattr(self, '_search_term', '')
        mkey = getattr(self, '_search_mode_key', 'ci')
        for i, r in enumerate(page_data):
            idx = start + i + 1
            tag = "odd" if i % 2 else "even"
            val = _snippet(r["value"], term, mkey) if term else r["value"]
            tree.insert("", "end", values=(idx, r["table"], r["column"],
                        r["rowid"], val, r["type"]), tags=(tag,))
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])
        total = len(self._sr_filtered)
        total_pages = max(1, (total + self._sr_page_size - 1) // self._sr_page_size)
        self._sr_page_label.configure(
            text=f"Page {self._sr_page + 1} of {total_pages}  |  Showing {len(page_data)} of {total} results")

    def _sr_prev_page(self):
        if self._sr_page > 0:
            self._sr_page -= 1
            self._display_search_page()

    def _sr_next_page(self):
        total_pages = max(1, (len(self._sr_filtered) + self._sr_page_size - 1) // self._sr_page_size)
        if self._sr_page < total_pages - 1:
            self._sr_page += 1
            self._display_search_page()

    def _stop_search(self):
        self._search_cancel = True

    def _show_scope(self):
        if not self.db.ok:
            return
        tables = self.db.tables()
        dlg = ScopeDlg(self, tables, self._count_cache, self._scope_tables)
        self.wait_window(dlg)
        if dlg.result is not None:
            self._scope_tables = dlg.result

    def _reset_scope(self):
        if self.db.ok:
            self._scope_tables = list(self.db.tables())
            self._search_status.configure(text=f"Scope reset: {len(self._scope_tables)} tables selected")

    def _on_search_dblclick(self, event):
        sel = self._search_tree.selection()
        if not sel:
            return
        vals = self._search_tree.item(sel[0], "values")
        if not vals:
            return
        tbl = vals[1]
        match_col = vals[2]  # The column where the match was found
        rid = vals[3]
        search_term = getattr(self, '_search_term', '')
        if rid and rid != "-":
            try:
                rid = int(rid)
                RowWin.show(self, self.db, tbl, rid,
                            search_term=search_term, match_col=match_col)
            except (ValueError, TypeError):
                pass

    def _show_search_errors(self):
        if not self._search_errors:
            messagebox.showinfo("Errors", "No errors")
            return
        messagebox.showwarning("Search Errors", "\n".join(self._search_errors[:50]))

    def _get_export_scope(self):
        """Ask user which results to export: all, filtered, or current page."""
        if not self._search_results:
            return None
        all_n = len(self._search_results)
        filt_n = len(self._sr_filtered)
        page_start = self._sr_page * self._sr_page_size
        page_data = self._sr_filtered[page_start:page_start + self._sr_page_size]
        page_n = len(page_data)

        dlg = tk.Toplevel(self)
        dlg.title("Export Scope")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()
        dlg.update_idletasks()
        _dw, _dh = 340, 170
        dlg.geometry(f"{_dw}x{_dh}+{self.winfo_rootx() + (self.winfo_width() - _dw) // 2}+{self.winfo_rooty() + (self.winfo_height() - _dh) // 2}")
        result = [None]

        tk.Label(dlg, text="What to export?", font=("Segoe UI", 10, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(pady=(12, 8))
        bf = tk.Frame(dlg, bg=C["bg"])
        bf.pack(fill="x", padx=20)
        btn_cfg = dict(font=("Segoe UI", 9), relief="flat", bd=0, padx=12, pady=5, cursor="hand2")
        def pick(which):
            result[0] = which
            dlg.destroy()
        tk.Button(bf, text=f"All Results ({all_n})", bg=C["acl"], fg=C["accent"],
                  command=lambda: pick("all"), **btn_cfg).pack(fill="x", pady=2)
        if filt_n != all_n:
            tk.Button(bf, text=f"Filtered ({filt_n})", bg="#e3fcef", fg=C["green"],
                      command=lambda: pick("filtered"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text=f"Current Page ({page_n})", bg="#f0f0ff", fg=C["purple"],
                  command=lambda: pick("page"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text="Cancel", bg=C["bg3"], fg=C["text2"],
                  command=dlg.destroy, **btn_cfg).pack(fill="x", pady=2)
        self.wait_window(dlg)
        if result[0] is None:
            return None
        if result[0] == "all":
            return self._search_results
        elif result[0] == "filtered":
            return list(self._sr_filtered)
        else:
            return page_data

    def _search_export_csv(self):
        data = self._get_export_scope()
        if not data:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                             filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["#", "Table", "Column", "RowID", "Value", "Type"])
                for i, r in enumerate(data):
                    w.writerow([i + 1, r["table"], r["column"], r["rowid"], r["value"], r["type"]])
            messagebox.showinfo("Exported", f"Exported {len(data)} results to:\n{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _search_export_json(self):
        data = self._get_export_scope()
        if not data:
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                             filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            out = [{"table": r["table"], "column": r["column"],
                    "rowid": r["rowid"], "value": r["value"], "type": r["type"]} for r in data]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2, default=str)
            messagebox.showinfo("Exported", f"Exported {len(out)} results to:\n{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _search_copy(self):
        data = self._get_export_scope()
        if not data:
            return
        lines = ["#\tTable\tColumn\tRowID\tValue\tType"]
        for i, r in enumerate(data):
            lines.append(f"{i+1}\t{r['table']}\t{r['column']}\t{r['rowid']}\t{r['value']}\t{r['type']}")
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))

    # ── Browse Tab ───────────────────────────────────────────────────
    def _build_browse_tab(self):
        bf = self._browse_frame

        # Top controls
        top = ttk.Frame(bf)
        top.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(top, text="Table:").pack(side="left")
        self._browse_table_var = tk.StringVar()
        self._browse_table_combo = ttk.Combobox(top, textvariable=self._browse_table_var,
                                                  state="readonly", width=28)
        self._browse_table_combo.pack(side="left", padx=4)
        self._browse_table_combo.bind("<<ComboboxSelected>>", lambda e: self._load_browse_table())

        ttk.Label(top, text="Rows/page:").pack(side="left", padx=(12, 2))
        self._browse_limit_var = tk.StringVar(value="200")
        ttk.Combobox(top, textvariable=self._browse_limit_var,
                     values=["50", "100", "200", "500", "1000"], state="readonly",
                     width=6).pack(side="left")

        ttk.Button(top, text="Prev", command=self._browse_prev).pack(side="left", padx=(12, 2))
        ttk.Button(top, text="Next", command=self._browse_next).pack(side="left", padx=2)

        self._browse_page_lbl = ttk.Label(top, text="", style="M.TLabel")
        self._browse_page_lbl.pack(side="left", padx=8)

        ttk.Button(top, text="Export CSV", command=self._browse_export_csv).pack(side="right", padx=3)
        ttk.Button(top, text="Export BLOBs", command=self._browse_export_blobs).pack(side="right", padx=3)

        # Filter row: Column dropdown + filter entry
        filt_f = ttk.Frame(bf)
        filt_f.pack(fill="x", padx=8, pady=2)
        ttk.Label(filt_f, text="Filter:").pack(side="left")
        self._browse_filter_var = tk.StringVar()
        self._browse_filter_var.trace_add("write", self._on_browse_filter)
        self._browse_filter_entry = ttk.Entry(filt_f, textvariable=self._browse_filter_var, width=30)
        self._browse_filter_entry.pack(side="left", padx=4, fill="x", expand=True)
        ttk.Label(filt_f, text="in", style="M.TLabel").pack(side="left", padx=2)
        self._browse_col_filter_var = tk.StringVar(value="All Columns")
        self._browse_col_combo = ttk.Combobox(filt_f, textvariable=self._browse_col_filter_var,
                                               values=["All Columns"], state="readonly", width=20)
        self._browse_col_combo.pack(side="left", padx=4)
        self._browse_col_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_browse_filter())
        self._browse_display_rows = []

        # PanedWindow
        pw = ttk.PanedWindow(bf, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=4)

        # TOP pane: bordered treeview container
        tree_outer = tk.Frame(pw, bg=C["border"], bd=1, relief="solid")
        pw.add(tree_outer, weight=3)

        tree_frame = ttk.Frame(tree_outer)
        tree_frame.pack(fill="both", expand=True)

        self._browse_tree = ttk.Treeview(tree_frame, show="headings", selectmode="browse")
        bxsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._browse_tree.xview)
        bysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._browse_tree.yview)
        self._browse_tree.configure(xscrollcommand=bxsb.set, yscrollcommand=bysb.set)
        bysb.pack(side="right", fill="y")
        bxsb.pack(side="bottom", fill="x")
        self._browse_tree.pack(fill="both", expand=True)
        self._browse_tree.bind("<<TreeviewSelect>>", self._on_browse_select)
        self._browse_tree.bind("<Double-1>", self._on_browse_dblclick)
        self._browse_tree.bind("<Return>", self._on_browse_dblclick)
        TreeviewTooltip(self._browse_tree)

        # BOTTOM pane: preview with border
        preview_outer = tk.Frame(pw, bg=C["border"], bd=1, relief="solid")
        pw.add(preview_outer, weight=1)

        preview_frame = ttk.Frame(preview_outer)
        preview_frame.pack(fill="both", expand=True)
        ttk.Label(preview_frame, text="Row Preview", style="B.TLabel").pack(anchor="w", padx=4, pady=(4, 0))

        prev_canvas = tk.Canvas(preview_frame, bg=C["bg"], highlightthickness=0)
        prev_sb = ttk.Scrollbar(preview_frame, orient="vertical", command=prev_canvas.yview)
        self._preview_inner = ttk.Frame(prev_canvas)
        self._preview_inner.bind("<Configure>",
            lambda e: prev_canvas.configure(scrollregion=prev_canvas.bbox("all")))
        prev_canvas.create_window((0, 0), window=self._preview_inner, anchor="nw")
        prev_canvas.configure(yscrollcommand=prev_sb.set)
        prev_sb.pack(side="right", fill="y")
        prev_canvas.pack(fill="both", expand=True)
        self._preview_canvas = prev_canvas
        def _prev_scroll(e):
            try:
                prev_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        prev_canvas.bind("<MouseWheel>", _prev_scroll)
        self._preview_inner.bind("<MouseWheel>", _prev_scroll)
        self._preview_tk_imgs = []

    def _load_browse_table(self, reset_offset=True):
        tbl = self._browse_table_var.get()
        if not tbl or not self.db.ok:
            return
        if reset_offset:
            self._browse_offset = 0
            self._browse_sort_col = None
            self._browse_sort_dir = "ASC"
        lim = int(self._browse_limit_var.get())
        cols, rows = self.db.browse(tbl, lim, self._browse_offset,
                                     self._browse_sort_col, self._browse_sort_dir)
        self._browse_cache_cols = cols
        self._browse_cache_data = rows
        self._browse_filter_var.set("")
        # Update column filter dropdown
        self._browse_col_combo.configure(values=["All Columns"] + cols)
        self._browse_col_filter_var.set("All Columns")
        self._display_browse_data(cols, rows)

        cnt = self._count_cache.get(tbl, "?")
        page = self._browse_offset // lim + 1
        self._browse_page_lbl.configure(text=f"Page {page}  |  {fmt_count(cnt)} rows total")


    def _display_browse_data(self, cols, rows):
        tree = self._browse_tree
        tree.delete(*tree.get_children())

        if not cols:
            tree.configure(columns=())
            return

        tree.configure(columns=cols)
        ncols = len(cols)
        for c in cols:
            tree.heading(c, text=c, command=lambda col=c: self._browse_sort(col))

        # Auto-size columns
        if not self._measure_font:
            self._measure_font = tkfont.Font(family="Segoe UI", size=9)
            self._measure_bold_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        mf = self._measure_font
        bf = self._measure_bold_font

        if ncols <= 8:
            max_w = 400
        elif ncols <= 15:
            max_w = 250
        else:
            max_w = 150

        for ci, c in enumerate(cols):
            hw = bf.measure(c) + 30
            data_w = 60
            for ri in range(min(50, len(rows))):
                if ri < len(rows) and ci < len(rows[ri]):
                    val = vb(rows[ri][ci])
                    cw = mf.measure(str(val)[:60]) + 20
                    data_w = max(data_w, cw)
            # Header width is minimum - column must at least show full header name
            w = max(hw, min(data_w, max_w))
            if c == "_rid":
                w = 72
            tree.column(c, width=w, minwidth=hw, anchor="w")

        # Insert rows with alternating colors
        for ri, row in enumerate(rows):
            vals = [vb(v) for v in row]
            tag = "odd" if ri % 2 else "even"
            tree.insert("", "end", values=vals, tags=(tag,))
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])
        self._browse_display_rows = rows

    def _browse_sort(self, col):
        if col == self._browse_sort_col:
            self._browse_sort_dir = "DESC" if self._browse_sort_dir == "ASC" else "ASC"
        else:
            self._browse_sort_col = col
            self._browse_sort_dir = "ASC"
        self._load_browse_table(reset_offset=False)

    def _browse_prev(self):
        lim = int(self._browse_limit_var.get())
        self._browse_offset = max(0, self._browse_offset - lim)
        self._load_browse_table(reset_offset=False)

    def _browse_next(self):
        lim = int(self._browse_limit_var.get())
        tbl = self._browse_table_var.get()
        cnt = _int_count(self._count_cache.get(tbl, 0))
        if self._browse_offset + lim >= cnt:
            return
        self._browse_offset += lim
        self._load_browse_table(reset_offset=False)

    def _on_browse_filter(self, *args):
        if self._browse_filter_after:
            self.after_cancel(self._browse_filter_after)
        self._browse_filter_after = self.after(200, self._apply_browse_filter)

    def _apply_browse_filter(self):
        """Filter browse rows by text, optionally restricted to a specific column."""
        filt = self._browse_filter_var.get().lower().strip()
        cols = self._browse_cache_cols
        rows = self._browse_cache_data
        col_sel = self._browse_col_filter_var.get()
        if not filt:
            self._display_browse_data(cols, rows)
            return
        # Determine column index if filtering specific column
        col_idx = None
        if col_sel != "All Columns":
            try:
                col_idx = cols.index(col_sel)
            except ValueError:
                pass
        filtered = []
        for row in rows:
            if col_idx is not None:
                # Filter specific column
                if col_idx < len(row) and filt in str(vb(row[col_idx])).lower():
                    filtered.append(row)
            else:
                # Filter all columns
                for v in row:
                    if filt in str(vb(v)).lower():
                        filtered.append(row)
                        break
        self._display_browse_data(cols, filtered)

    def _on_browse_select(self, event):
        sel = self._browse_tree.selection()
        if not sel:
            return
        vals = self._browse_tree.item(sel[0], "values")
        cols = self._browse_cache_cols
        if not vals or not cols:
            return

        # Clear preview
        for w in self._preview_inner.winfo_children():
            w.destroy()
        self._preview_tk_imgs = []

        # Get actual data from display rows
        idx = self._browse_tree.index(sel[0])
        display_rows = getattr(self, '_browse_display_rows', self._browse_cache_data)
        if idx >= len(display_rows):
            return
        actual_row = display_rows[idx]

        for i, c in enumerate(cols):
            v = actual_row[i] if i < len(actual_row) else None
            bg = C["bg"] if i % 2 == 0 else C["alt"]
            row_f = tk.Frame(self._preview_inner, bg=bg)
            row_f.pack(fill="x", padx=2, pady=1)

            tk.Label(row_f, text=c, font=("Segoe UI", 9, "bold"),
                     fg=C["accent"], bg=bg, width=18, anchor="nw").pack(side="left", padx=4, pady=2)

            if v is None:
                tk.Label(row_f, text="NULL", fg=C["text2"], bg=bg,
                         font=("Segoe UI", 9, "italic")).pack(side="left", padx=4)
            elif isinstance(v, bytes):
                bt = blob_type(v)
                tk.Label(row_f, text=f"{bt} ({fmtb(len(v))})",
                         fg=C["orange"], bg=bg, font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)
                tk.Button(row_f, text="View BLOB", font=("Segoe UI", 7),
                          command=lambda bv=v, cn=c: BlobViewer(self, bv, cn)).pack(side="left", padx=4)
            else:
                sv = str(v)
                display = sv[:500] if len(sv) > 500 else sv
                tk.Label(row_f, text=display, fg=C["text"], bg=bg,
                         wraplength=500, justify="left", anchor="nw").pack(side="left", padx=4)
                # Timestamp
                if isinstance(v, (int, float)):
                    ts = try_decode_timestamp(v)
                    if ts:
                        for fmt_name, decoded in ts:
                            tk.Label(row_f, text=f"{fmt_name}: {decoded}",
                                     fg=C["green"], bg=bg, font=("Segoe UI", 8)).pack(side="left", padx=8)

        # Bind scroll to all preview children
        def _bind_prev_scroll(w):
            w.bind("<MouseWheel>", lambda e: self._preview_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
            for child in w.winfo_children():
                _bind_prev_scroll(child)
        _bind_prev_scroll(self._preview_inner)

    def _on_browse_dblclick(self, event):
        sel = self._browse_tree.selection()
        if not sel:
            return
        tbl = self._browse_table_var.get()
        vals = self._browse_tree.item(sel[0], "values")
        cols = self._browse_cache_cols
        if not vals or not cols or "_rid" not in cols:
            return
        rid_idx = cols.index("_rid")
        try:
            rid = int(vals[rid_idx])
            RowWin.show(self, self.db, tbl, rid)
        except (ValueError, TypeError, IndexError):
            pass

    def _browse_export_csv(self):
        tbl = self._browse_table_var.get()
        if not tbl or not self.db.ok:
            return
        # Ask scope: displayed page or full table
        display_rows = getattr(self, '_browse_display_rows', self._browse_cache_data)
        all_rows = self._browse_cache_data
        cnt = _int_count(self._count_cache.get(tbl, 0), len(all_rows))
        dlg = tk.Toplevel(self)
        dlg.title("Export CSV")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()
        dlg.update_idletasks()
        _dw, _dh = 320, 150
        dlg.geometry(f"{_dw}x{_dh}+{self.winfo_rootx() + (self.winfo_width() - _dw) // 2}+{self.winfo_rooty() + (self.winfo_height() - _dh) // 2}")
        result = [None]
        tk.Label(dlg, text="What to export?", font=("Segoe UI", 10, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(pady=(12, 8))
        bf = tk.Frame(dlg, bg=C["bg"])
        bf.pack(fill="x", padx=20)
        btn_cfg = dict(font=("Segoe UI", 9), relief="flat", bd=0, padx=12, pady=5, cursor="hand2")
        def pick(which):
            result[0] = which
            dlg.destroy()
        tk.Button(bf, text=f"Loaded Rows ({len(all_rows)})", bg=C["acl"], fg=C["accent"],
                  command=lambda: pick("loaded"), **btn_cfg).pack(fill="x", pady=2)
        if len(display_rows) != len(all_rows):
            tk.Button(bf, text=f"Filtered Rows ({len(display_rows)})", bg="#e3fcef", fg=C["green"],
                      command=lambda: pick("filtered"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text="Cancel", bg=C["bg3"], fg=C["text2"],
                  command=dlg.destroy, **btn_cfg).pack(fill="x", pady=2)
        self.wait_window(dlg)
        if result[0] is None:
            return
        rows = all_rows if result[0] == "loaded" else display_rows
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                             initialfile=f"{tbl}.csv",
                                             filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            cols = self._browse_cache_cols
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for row in rows:
                    w.writerow([vb(v) for v in row])
            messagebox.showinfo("Exported", f"Exported {len(rows)} rows to:\n{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _browse_export_blobs(self):
        tbl = self._browse_table_var.get()
        if not tbl or not self.db.ok:
            return
        # Scope selection dialog
        total_rows = _int_count(self._count_cache.get(tbl, 0), len(self._browse_cache_data))
        loaded = len(self._browse_cache_data)
        dlg = tk.Toplevel(self)
        dlg.title("Export BLOBs")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()
        # Center on parent window
        dlg.update_idletasks()
        pw, ph = self.winfo_width(), self.winfo_height()
        px, py = self.winfo_rootx(), self.winfo_rooty()
        dw, dh = 340, 200
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        dlg.geometry(f"{dw}x{dh}+{x}+{y}")
        result = [None]
        tk.Label(dlg, text="Export BLOBs from:", font=("Segoe UI", 10, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(pady=(12, 8))
        bf = tk.Frame(dlg, bg=C["bg"])
        bf.pack(fill="x", padx=20)
        btn_cfg = dict(font=("Segoe UI", 9), relief="flat", bd=0, padx=12, pady=5, cursor="hand2")
        def pick(w):
            result[0] = w
            dlg.destroy()
        tk.Button(bf, text=f"All Rows in Table ({fmt_count(total_rows)})", bg=C["acl"], fg=C["accent"],
                  command=lambda: pick("all"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text=f"Loaded Page ({loaded})", bg="#e3fcef", fg=C["green"],
                  command=lambda: pick("loaded"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text="Cancel", bg=C["bg3"], fg=C["text2"],
                  command=dlg.destroy, **btn_cfg).pack(fill="x", pady=2)
        self.wait_window(dlg)
        if result[0] is None:
            return
        folder = filedialog.askdirectory(title="Select folder for BLOBs")
        if not folder:
            return
        # Find BLOB-type columns
        col_info = self.db.columns(tbl)
        blob_col_names = [c[0] for c in col_info if c[1] and "BLOB" in c[1].upper()]
        if not blob_col_names and result[0] == "all":
            # Fallback: check loaded data for bytes
            blob_col_names = []
            cols = self._browse_cache_cols
            for ri, row in enumerate(self._browse_cache_data[:5]):
                for ci, v in enumerate(row):
                    if isinstance(v, bytes) and ci < len(cols) and cols[ci] not in blob_col_names:
                        blob_col_names.append(cols[ci])
        # Progress dialog — centered on parent
        prog_dlg = tk.Toplevel(self)
        prog_dlg.title("Exporting BLOBs...")
        prog_dlg.configure(bg=C["bg"])
        prog_dlg.update_idletasks()
        pdw, pdh = 350, 100
        px2 = self.winfo_rootx() + (self.winfo_width() - pdw) // 2
        py2 = self.winfo_rooty() + (self.winfo_height() - pdh) // 2
        prog_dlg.geometry(f"{pdw}x{pdh}+{px2}+{py2}")
        prog_lbl = tk.Label(prog_dlg, text="Starting export...", bg=C["bg"], font=("Segoe UI", 9))
        prog_lbl.pack(pady=(10, 5))
        prog_bar = ttk.Progressbar(prog_dlg, mode="determinate")
        prog_bar.pack(fill="x", padx=20, pady=5)
        self.update_idletasks()
        count = 0
        errors = 0
        if result[0] == "loaded":
            # Export from loaded page data
            cols = self._browse_cache_cols
            rows = self._browse_cache_data
            prog_bar.configure(maximum=max(len(rows), 1))
            for ri, row in enumerate(rows):
                for ci, v in enumerate(row):
                    if isinstance(v, bytes) and len(v) > 0:
                        bt = blob_type(v)
                        ext = _EXT_MAP.get(bt, ".bin")
                        cn = cols[ci] if ci < len(cols) else f"col{ci}"
                        fname = f"{tbl}_r{ri}_{cn}{ext}"
                        try:
                            with open(os.path.join(folder, fname), "wb") as f:
                                f.write(v)
                            count += 1
                        except Exception:
                            errors += 1
                if ri % 50 == 0:
                    prog_bar.configure(value=ri + 1)
                    prog_lbl.configure(text=f"Exported {count} BLOBs ({ri+1}/{len(rows)} rows)")
                    self.update_idletasks()
        else:
            # Export ALL rows from table — query in batches
            batch_size = 500
            offset = 0
            total = total_rows if isinstance(total_rows, int) else 10000
            prog_bar.configure(maximum=max(total, 1))
            while True:
                try:
                    sql = f"SELECT rowid, * FROM {_q(tbl)} LIMIT {batch_size} OFFSET {offset}"
                    cur = self.db._conn.execute(sql)
                    col_descs = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                except Exception:
                    break
                if not rows:
                    break
                for row in rows:
                    rid = row[0]
                    for ci in range(1, len(row)):
                        v = row[ci]
                        if isinstance(v, bytes) and len(v) > 0:
                            bt = blob_type(v)
                            ext = _EXT_MAP.get(bt, ".bin")
                            cn = col_descs[ci] if ci < len(col_descs) else f"col{ci}"
                            fname = f"{tbl}_r{rid}_{cn}{ext}"
                            try:
                                with open(os.path.join(folder, fname), "wb") as f:
                                    f.write(v)
                                count += 1
                            except Exception:
                                errors += 1
                offset += batch_size
                prog_bar.configure(value=min(offset, total))
                prog_lbl.configure(text=f"Exported {count} BLOBs ({offset}/{fmt_count(total)} rows)")
                self.update_idletasks()
        prog_dlg.destroy()
        msg = f"Exported {count} BLOB(s) to:\n{folder}"
        if errors:
            msg += f"\n({errors} error(s))"
        messagebox.showinfo("Export Complete", msg)

    # ── Key bindings ─────────────────────────────────────────────────
    def _toggle_sidebar(self):
        if self._sidebar_visible:
            self._sidebar.pack_forget()
            self._sb_toggle.config(text="\u25B6")
            self._sidebar_visible = False
        else:
            self._sidebar.pack(side="left", fill="y", before=self._sb_toggle)
            self._sb_toggle.config(text="\u25C0")
            self._sidebar_visible = True

    def _bind_keys(self):
        self.bind("<Control-o>", lambda e: self._open_file())
        self.bind("<Control-f>", lambda e: self._focus_search())
        self.bind("<Escape>", lambda e: self._stop_search())
        self._search_entry.bind("<Return>", lambda e: self._do_search())

    def _setup_tooltips(self):
        """Add tooltips to all interactive widgets."""
        # Search tab
        ToolTip(self._search_entry, "Type your search term and press Enter")
        ToolTip(self._mode_combo, "Choose how to match: case-insensitive, exact, regex, etc.")
        ToolTip(self._deep_blob_cb, "Include BLOB columns in text search (slower)")
        ToolTip(self._search_btn, "Start searching across all tables (Enter)")
        ToolTip(self._stop_btn, "Cancel the running search (Escape)")
        ToolTip(self._scope_btn, "Select which tables to include in the search")
        ToolTip(self._reset_scope_btn, "Reset scope to include all tables")
        ToolTip(self._sr_table_filter, "Filter results by table (shows result count per table)")
        ToolTip(self._sr_col_filter, "Filter results by column name")
        ToolTip(self._search_tree, "Double-click or press Enter to open row detail")
        # Browse tab
        ToolTip(self._browse_table_combo, "Select a table to browse")
        ToolTip(self._browse_filter_entry, "Type to filter rows in real-time")
        ToolTip(self._browse_col_combo, "Filter by a specific column or all columns")
        ToolTip(self._browse_tree, "Double-click or press Enter to open row detail")
        # Sidebar
        ToolTip(self._schema_tree, "Expand tables to see columns, types, and indexes.\nRight-click for more options.")
        ToolTip(self._sb_toggle, "Toggle schema sidebar")

    def _focus_search(self):
        self._nb.select(self._search_frame)
        self._search_entry.focus_set()
        self._search_entry.select_range(0, "end")

    # ── DB operations ────────────────────────────────────────────────
    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Open SQLite Database",
            filetypes=[("SQLite files", "*.db *.sqlite *.sqlite3 *.db3"),
                       ("All files", "*.*")]
        )
        if path:
            self._open_db(path)

    def _open_db(self, path):
        t0 = time.time()
        try:
            self.db.open(path)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot open database:\n{e}")
            return
        self._load_time = time.time() - t0

        tables = self.db.tables()
        self._scope_tables = list(tables)
        self._count_cache = {t: "?" for t in tables}

        # Update UI
        fname = os.path.basename(path)
        fsize = fmtb(os.path.getsize(path)) if os.path.exists(path) else "?"
        self._db_info.configure(
            text=f"  {fname}  |  {fsize}  |  {len(tables)} tables  |  loaded in {self._load_time:.2f}s")

        self._browse_table_combo.configure(values=tables)
        if tables:
            self._browse_table_var.set(tables[0])
            self._load_browse_table()

        self._populate_schema()

        # Background count using SEPARATE connection (non-blocking)
        self._count_cancel = False
        self._bg_count_thread = threading.Thread(
            target=self._bg_count, args=(list(tables), self.db._path), daemon=True)
        self._bg_count_thread.start()

    def _bg_count(self, tables, db_path):
        """Count rows using a separate connection so search is never blocked.

        Two-pass approach for speed:
          Pass 1: max(rowid) — O(1), instant approximate counts (~0.01s total)
          Pass 2: SELECT COUNT(*) — exact counts (can take 10-15s on large DBs)
        Approximate counts show with '~' prefix until exact count arrives.
        """
        try:
            uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("PRAGMA cache_size = -2000")
            conn.execute("PRAGMA mmap_size = 67108864")
            conn.execute("PRAGMA query_only = ON")
        except Exception:
            return
        # Pass 1: fast approximate counts via max(rowid) — nearly instant
        for t in tables:
            if self._count_cancel:
                break
            try:
                r = conn.execute(f"SELECT max(rowid) FROM {_q(t)}").fetchone()
                approx = r[0] if r and r[0] is not None else 0
                self._count_cache[t] = f"~{approx}"  # ~ prefix = approximate
            except Exception:
                pass  # stays as "?", will be resolved in pass 2
        if not self._count_cancel:
            self.after(0, self._update_schema_counts)
        # Pass 2: exact counts — replaces approximations
        last_update = 0
        for t in tables:
            if self._count_cancel:
                break
            try:
                r = conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()
                self._count_cache[t] = r[0] if r else 0
            except Exception:
                if not isinstance(self._count_cache.get(t), int):
                    self._count_cache[t] = "?"
            now = time.time()
            if now - last_update >= 0.3:
                last_update = now
                self.after(0, self._update_schema_counts)
        try:
            conn.close()
        except Exception:
            pass
        if not self._count_cancel:
            self.after(0, self._update_schema_counts)

    def _update_schema_counts(self):
        tree = self._schema_tree
        for iid in tree.get_children():
            text = tree.item(iid, "text")
            if text == "Tables":
                for child in tree.get_children(iid):
                    vals = tree.item(child, "values")
                    if vals and len(vals) >= 1:
                        tbl = vals[0]
                        cnt = self._count_cache.get(tbl, "?")
                        tree.item(child, text=f"{tbl}  ({fmt_count(cnt)})")

    def _close_db(self):
        self._count_cancel = True
        self.db.close()
        self._count_cache = {}
        self._scope_tables = []
        self._search_results = []
        self._search_errors = []
        self._browse_cache_data = []
        self._browse_cache_cols = []
        self._browse_display_rows = []
        self._search_tree.delete(*self._search_tree.get_children())
        self._browse_tree.delete(*self._browse_tree.get_children())
        self._schema_tree.delete(*self._schema_tree.get_children())
        self._browse_table_combo.configure(values=[])
        self._browse_table_var.set("")
        self._db_info.configure(text="  No database loaded")
        self._sql_preview.configure(state="normal")
        self._sql_preview.delete("1.0", "end")
        self._sql_preview.configure(state="disabled")
        for w in self._preview_inner.winfo_children():
            w.destroy()

    def _show_info(self):
        if not self.db.ok:
            messagebox.showinfo("Info", "No database loaded")
            return
        m = self.db.meta()
        total_rows = sum(_int_count(v) for v in self._count_cache.values())
        info = (
            f"Path: {m.get('path', '')}\n"
            f"Size: {fmtb(m.get('size', 0))}\n"
            f"Page size: {m.get('page_size', '')}\n"
            f"Page count: {m.get('page_count', '')}\n"
            f"Journal mode: {m.get('journal_mode', '')}\n"
            f"Encoding: {m.get('encoding', '')}\n"
            f"Auto vacuum: {m.get('auto_vacuum', '')}\n"
            f"User version: {m.get('user_version', '')}\n"
            f"Freelist count: {m.get('freelist_count', '')}\n"
            f"Total rows: {total_rows:,}\n"
            f"Tables: {len(self.db.tables())}\n"
            f"Views: {len(self.db.views())}\n"
            f"Indexes: {len(self.db.all_indexes())}\n"
            f"Triggers: {len(self.db.triggers())}"
        )
        messagebox.showinfo("Database Info", info)


# ── Entry point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App(sys.argv[1] if len(sys.argv) > 1 else None)
    app.mainloop()
