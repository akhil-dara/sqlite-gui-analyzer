"""One design for every HTML file the tool writes: exports (Browse, Search, SQL, Timeline,
Tagged rows), the tagged rows report, the forensic report, the Database Map and the schema report.

    rep = Report("Browse table 'urls'", provenance=info, case_name="Case 12")
    rep.add_section("Rows")
    rep.add_table("urls", columns, rows_generator, badges=..., detail=..., tags=...)
    result = rep.write(path, protected)          # streamed to disk, bounded memory

A page is fully self-contained: one inline stylesheet and one inline script (both ours, in
engine.html_report_assets), no font, image, script or style loaded from anywhere; a
Content-Security-Policy forbids loading anything. Every value is escaped: html.escape for
markup (control characters shown as \\xNN), and rows embedded as JSON inside
<script type="application/json"> with every '<' written as \\u003c (so no </script> or <!--
can end or confuse it).

Layout: a top bar (theme, density, UTC / local dates, print, help), a table of
contents with a search over the whole report, a cover (title, case, the evidence files with
size and SHA-256, the tool and its version, the export time in UTC, scope and filters), a
summary (key numbers, an activity chart drawn from the date columns, top values, tags,
confidence mix), then the sections as cards.

Tables of rows never become one giant DOM table: the rows are written as JSON chunks of
limit html_chunk_rows rows while the export streams (generator in, chunk out), and the page
renders only the visible rows (a virtualized table with sort, filters, search, row details).
Without JavaScript each table shows its first rows (limit html_print_rows) as a plain table,
also printed. Above limit html_rows_per_part rows a table continues in part files next to the
report (<name>_part002.html ...), which the report lists (with size and SHA-256): nothing is
cut off silently.

Nothing is written near the evidence: write() refuses (ValueError) any path protected() is
true for, the part files included, before anything is created.
"""

import base64
import hashlib
import html
import io
import json
import math
import os
import platform
import re
import sqlite3
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

from . import limits as lim
from .decode import summary as blob_summary
from .fileformat.record import InvalidText
from .html_report_assets import CSS, JS
from .schema import Locator
from .tags import PartialBlob, valid_color

TOOL_NAME = "SQLite GUI Analyzer"
SAFE_INT = (1 << 53) - 1

# Only the report's own script runs: script-src allows exactly the SHA-256 of JS (computed
# here, so it always matches the script written), so an inline event handler or any other
# script that an escaping mistake let in is not run. Nothing loads from anywhere, no form
# posts, no <base> redirects links. (frame-ancestors is left out: browsers ignore it in a
# <meta> policy and log an error.)
SCRIPT_SHA256 = base64.b64encode(hashlib.sha256(JS.encode("utf-8")).digest()).decode("ascii")
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-%s'; "
       "img-src data: blob:; base-uri 'none'; form-action 'none'" % SCRIPT_SHA256)
_EPOCH = datetime(1970, 1, 1)
_DAY_MS = 86400000
DAYS_KEPT = 100000              # distinct days one activity chart counts
CHART_BINS = 60
KEY_COLUMNS_AUTO = 3
TOP_SHOWN = 5
BADGE_VALUES = 50               # distinct values a badge column counts
AUTO_BADGES = ("confidence", "level", "source", "state", "frame_state", "status")
_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_IMAGE_SIGS = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
               (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))
_TONES = OrderedDict([
    ("high", "ok"), ("live", "ok"), ("current", "ok"), ("ok", "ok"), ("complete", "ok"),
    ("committed", "ok"), ("medium", "warn"), ("warning", "warn"), ("superseded", "warn"),
    ("uncommitted", "warn"), ("older", "warn"), ("low", "bad"), ("error", "bad"),
    ("deleted", "bad"), ("carved", "bad"), ("freelist", "bad"), ("dropped", "bad"),
    ("unallocated", "bad"), ("info", "info"), ("db", "info"), ("wal", "info"),
    ("journal", "warn")])


class ReportError(ValueError):
    """The report cannot be written where asked (inside the evidence folder, no path)."""


class Markup(str):
    """Trusted markup: written as it is (everything else is escaped)."""
    __slots__ = ()


class _Missing(object):
    """A column a row does not have (e.g. a tag snapshot without it): shown empty."""
    __slots__ = ()

    def __repr__(self):
        return "MISSING"


MISSING = _Missing()


# -- escaping ------------------------------------------------------------------------------------
def esc(v):
    """Text safe in HTML content and attribute values; control characters as \\xNN."""
    if isinstance(v, Markup):
        return v
    s = html.escape("" if v is None else str(v), quote=True)
    if _CTRL_RE.search(s):
        s = _CTRL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), s)
    return s


def script_json(obj):
    """JSON safe inside <script type="application/json">: every '<' as \\u003c."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=str).replace("<", "\\u003c")


def _count(n, word, words=None):
    return "%s %s" % (format(n, ","), word if n == 1 else (words or word + "s"))


def utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _mtime_text(ns):
    try:
        return datetime.fromtimestamp(ns / 1e9, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _day_text(day):
    try:
        d = _EPOCH + timedelta(days=day)
        return "%04d-%02d-%02d" % (d.year, d.month, d.day)
    except (OverflowError, ValueError):
        return "?"


def _ms_text(ms):
    try:
        d = _EPOCH + timedelta(milliseconds=ms)
        return "%04d-%02d-%02d %02d:%02d:%02d" % (d.year, d.month, d.day, d.hour, d.minute,
                                                 d.second)
    except (OverflowError, ValueError):
        return "?"


def slug(text, prefix="s"):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(text)).strip("-")[:48]
    if not s or not s[0].isalpha():
        s = prefix + "-" + s if s else prefix
    return s


# -- colours -------------------------------------------------------------------------------------
def _lum(color):
    c = valid_color(color, "#000000")
    out = []
    for i in (1, 3, 5):
        x = int(c[i:i + 2], 16) / 255.0
        out.append(x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a, b):
    """WCAG contrast ratio of two '#rrggbb' colours."""
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def text_on(bg):
    """The text colour (near-black or white) that reads best on bg."""
    bg = valid_color(bg)
    return "#0F172A" if contrast("#0F172A", bg) >= contrast("#FFFFFF", bg) else "#FFFFFF"


def chip(name, color):
    c = valid_color(color)
    return Markup('<span class="chip" style="background:%s;color:%s">%s</span>'
                  % (c, text_on(c), esc(name)))


def badge(text, tone="muted"):
    return Markup('<span class="badge %s">%s</span>' % (
        tone if tone in ("ok", "warn", "bad", "info", "muted") else "muted", esc(text)))


def tone_of(value):
    """The badge tone of a value: confidence, state, source ... words ('muted' otherwise)."""
    s = str(value).strip().lower()
    if s in _TONES:
        return _TONES[s]
    words = set(re.findall(r"[a-z]+", s))
    for k, t in _TONES.items():
        if k in words:
            return t
    return "muted"


# -- values --------------------------------------------------------------------------------------
def image_mime(data):
    for sig, mime in _IMAGE_SIGS:
        if data[:len(sig)] == sig:
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _summary(raw):
    try:
        return blob_summary(raw)
    except Exception:                   # noqa: BLE001 - a summary is a courtesy, never fatal
        return ""


def _decoded(d, raw):
    """The decoded value of a BLOB for the row details ('dk' its form, 'dv' the text, 'dc'
    the full length when cut at the limit html_decoded_chars)."""
    from . import limits
    from .decode.render import decoded_view
    lim = limits.current()
    if not raw or len(raw) > lim["html_decoded_bytes"]:
        return
    try:
        view = decoded_view(raw)
    except Exception:                   # noqa: BLE001 - a decoded view is a courtesy
        return
    if view is None:
        return
    title, text = view
    cap = lim["html_decoded_chars"]
    d["dk"] = title
    d["dv"] = text[:cap]
    if len(text) > cap:
        d["dc"] = len(text)


def _blob(v, blob_mode, thumb_cap):
    raw = bytes(v)
    partial = isinstance(v, PartialBlob) and v.size != len(raw)
    size = v.size if partial else len(raw)
    d = OrderedDict([("n", size), ("s", _summary(raw))])
    if blob_mode == "base64":
        d["b64"] = base64.b64encode(raw).decode("ascii")
    elif blob_mode == "summary":
        d["h"] = v.sha256 if partial else hashlib.sha256(raw).hexdigest()
    else:
        d["hex"] = raw.hex()
    if partial:
        d["p"] = OrderedDict([("kept", len(raw)), ("h", v.sha256)])
    if not partial:
        _decoded(d, raw)
    mime = None if partial else image_mime(raw)
    if mime:
        d["m"] = mime
        if thumb_cap is not None and size > thumb_cap:
            d["big"] = 1
        elif blob_mode == "summary":
            d["img"] = base64.b64encode(raw).decode("ascii")
    return d


def encode_cell(v, blob_mode="hex", thumb_cap=None):
    """One value as embedded in a report (the script reads it back; decode_cell inverts it):
    NULL null; INTEGER a number ({"i": digits} beyond 2**53, where JavaScript numbers lose
    digits); REAL a number ({"f": repr} for a whole number, so 1.0 stays 1.0; {"r": repr} for
    inf / nan); TEXT a string; invalid text {"x": hex, "t": readable}; BLOB {"b": {n: size,
    s: summary, hex | b64 | h (SHA-256, summary mode), p: part kept, m: image type}}; several
    values {"j": [...] or {...}} of these; a missing column {"m": 1}."""
    t = type(v)
    if t is str:
        return v
    if v is None:
        return None
    if t is int:
        return v if -SAFE_INT <= v <= SAFE_INT else {"i": str(v)}
    if t is float:
        if not math.isfinite(v):
            return {"r": repr(v)}
        return {"f": repr(v)} if v.is_integer() else v
    if t is bool:
        return int(v)
    if isinstance(v, InvalidText):
        raw = bytes(v)
        return {"x": raw.hex(), "t": raw.decode("utf-8", "backslashreplace")}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"b": _blob(v, blob_mode, thumb_cap)}
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        v = int(v)
        return v if -SAFE_INT <= v <= SAFE_INT else {"i": str(v)}
    if isinstance(v, float):
        return encode_cell(float(v))
    if isinstance(v, str):
        return str(v)
    if isinstance(v, Locator):
        return v.display()
    if isinstance(v, (list, tuple)):
        return {"j": [encode_cell(x, blob_mode, thumb_cap) for x in v]}
    if isinstance(v, dict):
        return {"j": OrderedDict((str(k), encode_cell(x, blob_mode, thumb_cap))
                                 for k, x in v.items())}
    if v is MISSING:
        return {"m": 1}
    return str(v)


def decode_cell(c):
    """The value encode_cell() wrote (a BLOB written as a summary comes back as a dict)."""
    if not isinstance(c, dict):
        return c
    if "i" in c:
        return int(c["i"])
    if "f" in c:
        return float(c["f"])
    if "r" in c:
        return float(c["r"])
    if "x" in c:
        return InvalidText(bytes.fromhex(c["x"]))
    if "m" in c:
        return MISSING
    if "b" in c:
        b = c["b"]
        if "hex" in b:
            data = bytes.fromhex(b["hex"])
        elif "b64" in b:
            data = base64.b64decode(b["b64"])
        else:
            return OrderedDict([("blob_summary", b.get("s")), ("size", b.get("n")),
                                ("sha256", b.get("h"))])
        if "p" in b:
            pb = PartialBlob(data)
            pb.size, pb.sha256 = b["n"], b["p"]["h"]
            return pb
        return data
    if "j" in c:
        x = c["j"]
        if isinstance(x, list):
            return [decode_cell(i) for i in x]
        return OrderedDict((k, decode_cell(i)) for k, i in x.items())
    return c


def plain_text(v):
    """A value as short readable text (static tables, top values)."""
    if v is None:
        return "NULL"
    if isinstance(v, InvalidText):
        return bytes(v).decode("utf-8", "backslashreplace")
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "BLOB %s bytes" % format(getattr(v, "size", 0) or len(v), ",")
    if isinstance(v, Locator):
        return v.display()
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(decode_plain(v), ensure_ascii=False, default=str)
    if v is MISSING:
        return ""
    return str(v)


def decode_plain(v):
    if isinstance(v, (list, tuple)):
        return [decode_plain(x) for x in v]
    if isinstance(v, dict):
        return OrderedDict((str(k), decode_plain(x)) for k, x in v.items())
    if v is None or isinstance(v, (int, float, str)) and not isinstance(v, InvalidText):
        return v
    return plain_text(v)


def _cut(s, cap):
    if len(s) > cap:
        return s[:cap] + "… (%s characters)" % format(len(s), ",")
    return s


def static_cell(v, blob_mode="hex", cap=2000, thumb_cap=None):
    """One value as HTML for the plain (no-script / printed) tables."""
    if v is None:
        return '<span class="nul">NULL</span>'
    if v is MISSING:
        return ""
    if isinstance(v, InvalidText):
        raw = bytes(v)
        return '<span class="bad">invalid text (%s bytes):</span> %s' % (
            format(len(raw), ","), esc(_cut(raw.decode("utf-8", "backslashreplace"), cap)))
    if isinstance(v, (bytes, bytearray, memoryview)):
        raw = bytes(v)
        partial = isinstance(v, PartialBlob) and v.size != len(raw)
        size = v.size if partial else len(raw)
        out = '<span class="blob">BLOB %s bytes</span> %s' % (format(size, ","),
                                                             esc(_summary(raw)))
        if partial:
            out += ' <span class="muted">(first %s bytes kept; SHA-256 %s)</span>' % (
                format(len(raw), ","), esc(v.sha256))
        mime = None if partial else image_mime(raw)
        if mime and (thumb_cap is None or size <= thumb_cap):
            out += '<img class="thumb" alt="image, %s bytes" src="data:%s;base64,%s">' % (
                format(size, ","), mime, base64.b64encode(raw).decode("ascii"))
        return out
    return esc(_cut(plain_text(v), cap))


# -- the view-state link format (mirrors STATE_JS: stEnc / stDec) -------------------------------------
_URI_SAFE = "!~*'()"


def _uri(s):
    return quote(str(s), safe=_URI_SAFE)


def state_encode(st):
    """'v=1&sec=..&tbl=..&q=..&m=0&sort=3a,1d&f=<JSON>&row=5&hide=1,2&ord=..&pin=1': the part
    of a report link after '#', as the report's script writes it (stEnc)."""
    st = st or {}
    p = ["v=1"]
    for k in ("sec", "tbl", "q"):
        if st.get(k):
            p.append("%s=%s" % (k, _uri(st[k])))
    if st.get("m") == 0:
        p.append("m=0")
    if st.get("sort"):
        p.append("sort=" + ",".join("%d%s" % (int(c), "d" if d < 0 else "a")
                                    for c, d in st["sort"]))
    f = st.get("f") or {}
    o = OrderedDict()
    for k in sorted((k for k in f if str(k).isdigit()), key=int):
        x, y = f[k] or {}, OrderedDict()
        if x.get("e"):
            y["e"] = str(x["e"])
        if x.get("v") is not None:
            y["v"] = [str(i) for i in x["v"]]
        if y:
            o[str(int(k))] = y
    if o:
        p.append("f=" + _uri(json.dumps(o, ensure_ascii=False, separators=(",", ":"))))
    row = st.get("row")
    if isinstance(row, int) and not isinstance(row, bool) and row >= 0:
        p.append("row=%d" % row)
    for k in ("hide", "ord"):
        if st.get(k):
            p.append("%s=%s" % (k, ",".join(str(int(i)) for i in st[k])))
    if st.get("pin"):
        p.append("pin=1")
    return "&".join(p)


def state_decode(h):
    """The state of a report link's '#...' (stDec): a plain '#id' gives just the section."""
    st = {"sec": "", "tbl": "", "q": "", "m": 1, "sort": [], "f": OrderedDict(), "row": -1,
          "hide": [], "ord": [], "pin": 0}
    h = str(h or "")
    if h.startswith("#"):
        h = h[1:]
    if not h:
        return st

    def dec(s):
        try:
            return unquote(s, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return ""
    if "=" not in h:
        st["sec"] = dec(h)
        return st

    def ints(s):
        return [int(x) for x in s.split(",") if x.isdigit()]
    for kv in h.split("&"):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        if k in ("sec", "tbl", "q"):
            st[k] = dec(v)
        elif k == "m":
            st["m"] = 0 if v == "0" else 1
        elif k == "sort":
            for s in v.split(","):
                m = re.match(r"^(\d+)([ad])$", s)
                if m:
                    st["sort"].append([int(m.group(1)), -1 if m.group(2) == "d" else 1])
        elif k == "f":
            try:
                o = json.loads(dec(v), object_pairs_hook=OrderedDict)
            except ValueError:
                continue
            if isinstance(o, dict):
                for c, x in o.items():
                    if not c.isdigit() or not isinstance(x, dict):
                        continue
                    y = OrderedDict()
                    if isinstance(x.get("e"), str) and x["e"]:
                        y["e"] = x["e"]
                    if isinstance(x.get("v"), list):
                        y["v"] = [str(i) if not isinstance(i, str) else i for i in x["v"]]
                    if y:
                        st["f"][c] = y
        elif k == "row":
            if v.isdigit():
                st["row"] = int(v)
        elif k in ("hide", "ord"):
            st[k] = ints(v)
        elif k == "pin":
            st["pin"] = 1 if v == "1" else 0
    return st


# -- small pieces of markup ----------------------------------------------------------------------
def _cellmark(v):
    return v if isinstance(v, Markup) else esc(v)


def simple_table_html(head, rows, classes=None, num=(), mono=(), caption=None, sortable=True):
    """A small table rendered in full (it prints whole): head [names], rows [[cell]] where a
    cell is text (escaped) or Markup; num / mono: column indexes aligned right / monospaced."""
    num, mono = set(num or ()), set(mono or ())
    out = ['<div class="stw"><table class="st">']
    if caption:
        out.append('<caption class="vh">%s</caption>' % esc(caption))
    out.append("<thead><tr>")
    for i, h in enumerate(head):
        label = _cellmark(h)
        out.append('<th scope="col"%s%s>%s</th>' % (
            ' class="num"' if i in num else "", ' aria-sort="none"' if sortable else "",
            '<button type="button" class="ss">%s</button>' % label if sortable else label))
    out.append("</tr></thead><tbody>")
    for ri, r in enumerate(rows):
        cls = classes[ri] if classes and ri < len(classes) and classes[ri] else ""
        out.append('<tr%s>' % (' class="%s"' % esc(cls) if cls else ""))
        for i, c in enumerate(r):
            k = []
            if i in num:
                k.append("num")
            if i in mono:
                k.append("mono")
            out.append('<td%s>%s</td>' % (' class="%s"' % " ".join(k) if k else "",
                                          _cellmark(c)))
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return Markup("".join(out))


def code_html(sql, title=None, cls="sql"):
    return Markup('<div class="codeblock"><div class="codehead"><span>%s</span>'
                  '<button type="button" class="btn sm js-only" data-act="copy-code">Copy'
                  '</button></div><pre class="code %s"><code>%s</code></pre></div>'
                  % (esc(title or "SQL"), esc(cls), esc(sql)))


def card_html(label, value, sub=None, tone=None, wide=False, body=None):
    return Markup('<div class="card%s%s"><div class="l">%s</div><div class="v">%s</div>%s%s'
                  '</div>' % (" " + tone if tone else "", " wide" if wide else "", esc(label),
                              _cellmark(value), '<div class="s">%s</div>' % _cellmark(sub)
                              if sub else "", _cellmark(body) if body else ""))


def cards_html(cards):
    return Markup('<div class="cards">%s</div>' % "".join(
        c if isinstance(c, Markup) else card_html(*c) for c in cards))


def details_html(summary, inner, id=None, open=False):
    return Markup('<details%s%s><summary>%s</summary>%s</details>' % (
        ' id="%s"' % esc(id) if id else "", " open" if open else "", _cellmark(summary),
        inner))


def paragraphs(text, cls=None):
    return Markup("".join('<p%s>%s</p>' % (' class="%s"' % cls if cls else "", esc(p))
                          for p in str(text).split("\n\n") if p.strip()))


# -- tables of rows ------------------------------------------------------------------------------
class _Stats(object):
    """What the summary says about one table, gathered while its rows stream (bounded)."""

    def __init__(self):
        self.dmin, self.dmax = {}, {}
        self.days = {}
        self.days_over = False
        self.keys = OrderedDict()
        self.key_over = set()
        self.tags = OrderedDict()
        self.badges = OrderedDict()


class _Table(object):
    def __init__(self, rep, name, fields, rows, title, total, badges, detail, dates, tags,
                 notes, key_columns, blob_mode, note, tid, chart):
        self.rep = rep
        self.name = str(name)
        self.fields = [str(f) for f in fields]
        self.nf = len(self.fields)
        self.rows = rows
        self.title = title
        self.total = total
        self.note = note
        self.tid = tid
        self.blob_mode = blob_mode if blob_mode in ("hex", "base64", "summary") else "hex"
        self.chart = chart
        idx = dict((f, i) for i, f in reversed(list(enumerate(self.fields))))
        self._idx = idx
        self.given_dates = dates
        self.tag_col = idx.get(tags[0]) if tags else None
        self.tag_colors = OrderedDict()
        if tags:
            for k, c in (tags[1] or {}).items():
                c = valid_color(c)
                self.tag_colors[str(k)] = [c, text_on(c)]
        self.notes_col = idx.get(notes) if notes else None
        self.badge_cols = OrderedDict()
        if badges is None:
            for i, f in enumerate(self.fields):
                if f.lower() in AUTO_BADGES:
                    self.badge_cols[i] = OrderedDict()
        else:
            for k, m in (badges or {}).items():
                if k in idx:
                    self.badge_cols[idx[k]] = OrderedDict(
                        (str(a), b) for a, b in (m or {}).items())
        self.badge_auto = dict((c, not m) for c, m in self.badge_cols.items())
        detail = detail or {}
        self.prov = [idx[f] for f in detail.get("prov", ()) if f in idx]
        self.path = []
        for x in detail.get("path", ()):
            if isinstance(x, dict) and x.get("col") in idx:
                self.path.append(idx[x["col"]])
            elif isinstance(x, int) and not isinstance(x, bool) and 0 <= x < self.nf:
                self.path.append(x)
            elif x is not None:
                self.path.append(str(x))
        self.given_keys = key_columns
        self.prepared = False
        self.dates = []                     # [(col, kind, label)]
        self.date_extra = {}                # col -> index of its decoded ms in an encoded row
        self.key_cols = []
        self.widths = [140] * self.nf
        self.num_cols = []
        self.stats = _Stats()
        self.written = 0
        self.complete = False
        self.stopped = ""
        self.pieces = []                    # [{"no", "path", "first", "count"}] (1: this file)

    # first rows: date columns, key columns, widths
    def prepare(self, sample):
        if self.prepared:
            return
        self.prepared = True
        from . import timeline as tl
        given = self.given_dates
        forced = set()
        if isinstance(given, dict):
            for name, kind in given.items():
                if name in self._idx and kind in tl.KINDS:
                    self.dates.append((self._idx[name], kind, tl.LABELS.get(kind, kind)))
                    forced.add(self._idx[name])
        if given is not False:
            for i, name in enumerate(self.fields):
                if i == self.tag_col or i in self.badge_cols or i in forced:
                    continue
                vals = []
                for r in sample:
                    if i < len(r):
                        v = r[i]
                        if v is None or isinstance(v, (bytes, bytearray, memoryview, list,
                                                       tuple, dict, bool)) or v is MISSING:
                            continue
                        if isinstance(v, (int, float, str)):
                            vals.append(v)
                            if len(vals) >= 300:
                                break
                if not vals:
                    continue
                try:
                    kind = tl.suggest_kind(name, vals)
                except Exception:           # noqa: BLE001 - a guess, never fatal
                    kind = None
                if kind:
                    self.dates.append((i, kind, tl.LABELS.get(kind, kind)))
        self.dates.sort()
        for k, (c, _kind, _label) in enumerate(self.dates):
            self.date_extra[c] = self.nf + k
        self._to_utc = tl.to_utc
        # key columns (top values in the summary)
        skip = set(self.date_extra) | set([self.tag_col, self.notes_col]) | set(self.badge_cols)
        if self.given_keys:
            self.key_cols = [self._idx[k] for k in self.given_keys if k in self._idx]
        elif len(sample) >= 20:
            for i in range(self.nf):
                if i in skip:
                    continue
                vals = [r[i] for r in sample if i < len(r) and r[i] is not None]
                if not vals or not all(isinstance(v, (int, str)) and not isinstance(
                        v, (bool, InvalidText)) for v in vals):
                    continue
                if any(isinstance(v, str) and len(v) > 64 for v in vals):
                    continue
                distinct = len(set(vals))
                if 2 <= distinct <= min(50, max(2, len(sample) // 5)):
                    self.key_cols.append(i)
                    if len(self.key_cols) >= KEY_COLUMNS_AUTO:
                        break
        for c in self.key_cols:
            self.stats.keys[c] = {}
        for c in self.badge_cols:
            self.stats.badges[c] = OrderedDict()
        # widths and number columns from the first rows
        for i, name in enumerate(self.fields):
            lens, nums, seen = [], True, False
            for r in sample[:300]:
                if i >= len(r):
                    continue
                v = r[i]
                if v is None:
                    continue
                seen = True
                if not (isinstance(v, (int, float)) and not isinstance(v, bool)):
                    nums = False
                lens.append(min(len(plain_text(v)), 60))
            avg = (sum(lens) / float(len(lens))) if lens else 6
            if i in self.date_extra:
                # the date (23 characters) and the raw value in a smaller font beside it
                self.widths[i] = int(max(240, min(340, 23 * 7.4 + min(avg, 30) * 5.6 + 40),
                                         (len(name) + 8) * 7.4 + 34))
                continue
            self.widths[i] = int(max(72, min(460, max(avg, len(name) + 3) * 7.4 + 34)))
            if seen and nums and i not in self.date_extra:
                self.num_cols.append(i)

    def encode(self, row, thumb_cap):
        """The encoded row (plus the decoded date of each date column, in ms) and stats."""
        st = self.stats
        nf = self.nf
        bm = self.blob_mode
        if len(row) != nf:
            row = list(row)[:nf] + [MISSING] * (nf - len(row))
        out = [v if (v is None or type(v) is str or (type(v) is int and -SAFE_INT <= v <= SAFE_INT))
               else encode_cell(v, bm, thumb_cap) for v in row]
        if self.dates:
            to_utc = self._to_utc
            first = True
            for c, kind, _label in self.dates:
                v = row[c]
                ms = None
                if v is not None and v is not MISSING and not isinstance(
                        v, (bytes, bytearray, memoryview)):
                    try:
                        dt = to_utc(v, kind)
                    except Exception:       # noqa: BLE001 - not a date then
                        dt = None
                    if dt is not None:
                        d = dt - _EPOCH
                        ms = d.days * _DAY_MS + d.seconds * 1000 + d.microseconds // 1000
                out.append(ms)
                if ms is not None:
                    if c not in st.dmin or ms < st.dmin[c]:
                        st.dmin[c] = ms
                    if c not in st.dmax or ms > st.dmax[c]:
                        st.dmax[c] = ms
                    if first:
                        day = ms // _DAY_MS
                        if day in st.days:
                            st.days[day] += 1
                        elif len(st.days) < DAYS_KEPT:
                            st.days[day] = 1
                        else:
                            st.days_over = True
                first = False
        if st.keys:
            cap = self.rep.L["html_top_distinct"]
            for c, counter in st.keys.items():
                v = row[c]
                if v is None or v is MISSING or isinstance(v, (bytes, bytearray, memoryview,
                                                               list, tuple, dict)):
                    continue
                k = v if type(v) is str else plain_text(v)
                if len(k) > 200:
                    continue
                if k in counter:
                    counter[k] += 1
                elif len(counter) < cap:
                    counter[k] = 1
                else:
                    st.key_over.add(c)
        if self.tag_col is not None:
            tv = row[self.tag_col]
            names = tv if isinstance(tv, (list, tuple)) else (
                [x for x in re.split(r";\s*", tv) if x] if isinstance(tv, str) else [])
            for t in names:
                t = str(t)
                st.tags[t] = st.tags.get(t, 0) + 1
        for c, counter in st.badges.items():
            v = row[c]
            k = v if type(v) is str else (plain_text(v) if v is not None else "NULL")
            if k in counter:
                counter[k] += 1
            elif len(counter) < BADGE_VALUES:
                counter[k] = 1
                if self.badge_auto.get(c):
                    self.badge_cols[c][k] = tone_of(k)
        return out

    def meta(self, sink):
        L = self.rep.L
        m = OrderedDict([("id", self.tid), ("name", self.name), ("fields", self.fields),
                         ("n", sink.count), ("first", sink.first), ("chunks", sink.chunks),
                         ("complete", self.complete), ("stopped", self.stopped),
                         ("dates", [[c, kind, label, self.date_extra[c]]
                                    for c, kind, label in self.dates]),
                         ("badges", OrderedDict((str(c), dict(m)) for c, m in
                                                self.badge_cols.items())),
                         ("tags", OrderedDict([("col", self.tag_col),
                                               ("colors", self.tag_colors)])
                          if self.tag_col is not None else None),
                         ("notes", self.notes_col),
                         ("detail", OrderedDict([("prov", self.prov), ("path", self.path)])),
                         ("blob_mode", self.blob_mode), ("thumb", L["html_thumb_bytes"]),
                         ("print", L["html_print_rows"]), ("cell", L["html_cell_chars"]),
                         ("distinct", L["filter_distinct_values"]),
                         ("widths", self.widths), ("num", self.num_cols)])
        if sink.part_no > 1:
            m["part"] = OrderedDict([("no", sink.part_no)])
        return m


class _Sink(object):
    """Writes one table's rows (or one part of them) into a page body as they come."""

    def __init__(self, rep, spec, out, part_no, first, head_level):
        self.rep, self.spec, self.out = rep, spec, out
        self.part_no, self.first, self.count, self.chunks = part_no, first, 0, 0
        self.buf = []
        self.static_n = 0
        self.static = tempfile.SpooledTemporaryFile(max_size=1 << 20, mode="w+",
                                                    encoding="utf-8", newline="\n",
                                                    errors="backslashreplace")
        tid = spec.tid
        out.write('<div class="tblock" id="%s" data-toc="%s">\n\x00SLOT %s %d\n'
                  '<div class="grid js-only" id="g-%s" data-grid="%s"></div>\n'
                  % (tid, tid, tid, head_level, tid, tid))

    def add(self, row):
        self.buf.append(row)
        self.count += 1
        if len(self.buf) >= self.rep.L["html_chunk_rows"]:
            self.flush()

    def flush(self):
        if not self.buf:
            return
        spec, L = self.spec, self.rep.L
        spec.prepare(self.buf)
        thumb, keep, cap = L["html_thumb_bytes"], L["html_print_rows"], L["html_cell_chars"]
        enc = []
        n0 = self.count - len(self.buf)
        for k, row in enumerate(self.buf):
            enc.append(spec.encode(row, thumb))
            if self.static_n < keep:
                self.static_n += 1
                cells = "".join("<td>%s</td>" % (
                    self._tags_html(row[i] if i < len(row) else None)
                    if i == spec.tag_col else static_cell(
                        row[i] if i < len(row) else MISSING, spec.blob_mode, cap, thumb))
                    for i in range(spec.nf))
                self.static.write('<tr><td class="num">%s</td>%s</tr>\n'
                                  % (format(self.first + n0 + k, ","), cells))
        self.out.write('<script type="application/json" class="gdata" data-grid="%s">'
                       % spec.tid)
        self.out.write(script_json(enc))
        self.out.write("</script>\n")
        self.chunks += 1
        self.buf = []

    def _tags_html(self, v):
        names = v if isinstance(v, (list, tuple)) else (
            [x for x in re.split(r";\s*", v) if x] if isinstance(v, str) else [])
        colors = self.spec.tag_colors
        return "".join(chip(t, colors.get(str(t), ["#94A3B8"])[0]) for t in names)

    def close(self):
        self.flush()
        spec = self.spec
        if not spec.prepared:
            spec.prepare([])
        out = self.out
        tid = spec.tid
        out.write('<noscript><p class="note warn">The interactive table (search, sort, '
                  'filters, row details) needs JavaScript; the first rows are shown below as '
                  'a plain table.</p></noscript>\n')
        out.write('<div class="static-wrap" id="s-%s"><div class="stw"><table class="st">'
                  '<caption class="vh">%s</caption><thead><tr><th scope="col" class="num">#'
                  '</th>%s</tr></thead><tbody>\n' % (
                      tid, esc(spec.name),
                      "".join('<th scope="col">%s</th>' % esc(f) for f in spec.fields)))
        self.static.seek(0)
        while True:
            block = self.static.read(1 << 16)
            if not block:
                break
            out.write(block)
        self.static.close()
        keep = self.rep.L["html_print_rows"]
        if self.count <= keep:
            note = ("All %s of this table%s." % (_count(self.count, "row"),
                                                  " part" if self.part_no > 1 else "")
                    if self.count else "No rows.")
        else:
            note = ("The first %s of the %s in this file are shown here and printed (limit "
                    "html_print_rows); the interactive view above holds all of them."
                    % (format(keep, ","), _count(self.count, "row")))
        out.write('</tbody></table></div><p class="muted small" id="sn-%s">%s</p></div>\n'
                  % (tid, esc(note)))
        out.write('<script type="application/json" class="gmeta" data-grid="%s">%s</script>\n'
                  '</div>\n' % (tid, script_json(spec.meta(self))))
        if self.part_no > 1:
            out.close()


class _Section(object):
    __slots__ = ("id", "title", "title_html", "level", "blocks")

    def __init__(self, sid, title, title_html, level):
        self.id, self.title, self.title_html, self.level = sid, title, title_html, level
        self.blocks = []


class ReportResult(object):
    """write()'s result: path, rows (written, all tables), complete, stopped (why not), files
    [{path, size, sha256, role, ...}] (the report first, then its part files), parts (the part
    files only), size and sha256 of the report file, seconds."""

    def __init__(self, path):
        self.path = path
        self.rows = 0
        self.complete = True
        self.stopped = ""
        self.files = []
        self.parts = []
        self.size = None
        self.sha256 = None
        self.seconds = 0.0

    def __repr__(self):
        return "ReportResult(%r, rows=%d, complete=%r, parts=%d)" % (
            self.path, self.rows, self.complete, len(self.parts))


def _hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _prov_evidence(p):
    out = []
    for d in p.get("databases") or ():
        for f in d.get("files") or ():
            out.append((d.get("label") or "", f))
    for f in p.get("evidence") or ():
        out.append(("", f))
    return out


class Report(object):
    """One report page (see the module docstring). provenance: the dict engine.export's
    provenance() builds (tool, exported_utc, python, sqlite, databases with their evidence
    files, source, scope, filters, extra, blob_mode); any of it can be given instead as
    keyword arguments: tool=(name, version), exported_utc, evidence=[(database label,
    {role, path, size, mtime_utc | mtime_ns, sha256, note, error})], details=[(label, text)].
    limits: overrides of engine.limits for this report (e.g. {"html_rows_per_part": 1000}).
    auto_summary: False leaves out the numbers gathered from the tables (the caller's own
    summary cards stay)."""

    def __init__(self, title, provenance=None, case_name="", subtitle="", kind="Report",
                 tool=None, exported_utc=None, evidence=None, details=None, limits=None,
                 auto_summary=True, python=None, sqlite=None, database=None):
        p = provenance or {}
        self.title = str(title or "Report")
        self.case_name = str(case_name or p.get("case") or "")
        self.subtitle = str(subtitle or "")
        self.kind = str(kind or "Report")
        t = p.get("tool") if isinstance(p.get("tool"), dict) else {}
        self.tool_name = str(t.get("name") or TOOL_NAME)
        self.tool_version = str(t.get("version") or "")
        if tool:
            self.tool_name, self.tool_version = str(tool[0]), str(tool[1])
        self.exported = str(exported_utc or p.get("exported_utc") or utc_now_text())
        self.python = str(python or p.get("python") or platform.python_version())
        self.sqlite = str(sqlite or p.get("sqlite") or sqlite3.sqlite_version)
        self.evidence = list(evidence) if evidence is not None else _prov_evidence(p)
        self.database = database
        self.facts = []
        for key, label in (("source", "Source"), ("scope", "Scope"), ("filters", "Filters")):
            if p.get(key):
                self.facts.append((label, p[key]))
        self.facts.extend(details or ())
        self.extra = []
        for k, v in (p.get("extra") or {}).items():
            if isinstance(v, (list, tuple)):
                v = "\n".join(str(x) for x in v)
            elif isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False, default=str)
            self.extra.append((str(k), v))
        self.blob_mode = p.get("blob_mode")
        self.L = lim.checked(limits)
        self.auto_summary = auto_summary
        self._sections = []
        self._ids = set(["main", "toc", "cover", "cover-h", "summary", "summary-h", "drawer",
                         "drawer-title", "drawer-pos", "drawer-body", "help", "help-title",
                         "pop", "toast", "totop", "rs", "rs-stat", "print-toc", "act-theme", "act-toc",
                         "act-density", "act-tz", "pc-op", "pc-a", "pc-b", "pv-q"])
        self._tables = []
        self._reserved = set()
        self._cards = []
        self._summary_extra = []
        self._stopped_all = False

    # -- building -----------------------------------------------------------------------------
    def uid(self, base, prefix="s"):
        """A unique element id made from base."""
        s = slug(base, prefix)
        cand, n = s, 2
        while cand in self._ids or cand + "-h" in self._ids or "g-" + cand in self._ids:
            cand = "%s-%d" % (s, n)
            n += 1
        for x in (cand, cand + "-h", "g-" + cand, "s-" + cand, "sn-" + cand):
            self._ids.add(x)
        self._reserved.add(cand)
        return cand

    def _cur(self):
        if not self._sections:
            self.add_section(self.title)
        return self._sections[-1]

    def add_section(self, title, text=None, cards=None, id=None, level=2, title_html=None,
                    note=None):
        """Start a section (level 2) or a subsection (level 3); later add_* calls go into it.
        Returns its id (for links: '#id')."""
        if id is not None and id in self._reserved:
            sid = id                    # an id this report handed out earlier (uid())
        else:
            sid = self.uid(id or title, "s")
        self._reserved.discard(sid)
        sec = _Section(sid, str(title), title_html, 3 if level >= 3 else 2)
        self._sections.append(sec)
        if text:
            self.add_text(text)
        if cards:
            self.add_cards(cards)
        if note:
            self.add_note(note)
        return sid

    def add_html(self, markup):
        """Trusted markup (build it with esc(), simple_table_html(), code_html() ...)."""
        self._cur().blocks.append(Markup(markup))

    def add_text(self, text, cls=None):
        self._cur().blocks.append(paragraphs(text, cls))

    def add_note(self, text, tone=""):
        self._cur().blocks.append(Markup('<p class="note%s">%s</p>' % (
            " " + tone if tone in ("warn", "bad") else "", _cellmark(text))))

    def add_cards(self, cards):
        """cards: [(label, value, sub, tone)] (tone: ok, warn, bad or None) or card_html()."""
        self._cur().blocks.append(cards_html(cards))

    def add_kv(self, pairs, title=None):
        out = []
        if title:
            out.append("<h3>%s</h3>" % esc(title))
        out.append('<dl class="facts">%s</dl>' % "".join(
            "<dt>%s</dt><dd>%s</dd>" % (esc(k), _cellmark(v)) for k, v in pairs))
        self._cur().blocks.append(Markup("".join(out)))

    def add_simple_table(self, head, rows, classes=None, num=(), mono=(), title=None,
                         sortable=True):
        """A small table printed in full (evidence, findings, links...)."""
        if title:
            self._cur().blocks.append(Markup("<h3>%s</h3>" % esc(title)))
        self._cur().blocks.append(simple_table_html(head, rows, classes, num, mono, title,
                                                    sortable))

    def add_svg(self, svg_text, title=None, note=None):
        """An SVG made by the tool (the relationship diagram): kept as it is."""
        out = []
        if title:
            out.append("<h3>%s</h3>" % esc(title))
        if note:
            out.append('<p class="muted">%s</p>' % esc(note))
        out.append('<div class="diagram" role="img" aria-label="%s">%s</div>' % (
            esc(title or "Diagram"), svg_text))
        self._cur().blocks.append(Markup("".join(out)))

    def add_code(self, sql, title=None):
        self._cur().blocks.append(code_html(sql, title))

    def add_summary_cards(self, cards):
        """Cards shown first in the Summary."""
        self._cards.extend(cards)

    def add_summary_html(self, markup):
        self._summary_extra.append(Markup(markup))

    def add_table(self, name, fields, rows, total=None, badges=None, detail=None, dates=None,
                  tags=None, notes=None, key_columns=None, blob_mode=None, title=None,
                  note=None, id=None, chart=True):
        """A table of rows, streamed when the report is written: rows is any iterable of
        sequences (a generator over millions of rows is fine).
        badges: {column: {value: tone}} (tones ok, warn, bad, info, muted; an empty mapping:
          tones from the words), default: columns named confidence, level, source, state,
          frame_state, status get them;
        detail: {"prov": [columns shown as the row's provenance], "path": [text, or
          {"col": column}, e.g. database > table > row]};
        dates: {column: kind (engine.timeline KINDS)} for those columns, the others are
          detected from the first rows (engine.timeline.suggest_kind); False: no date column;
        tags: (column, {tag: '#rrggbb'}) the column holding each row's tag names;
        notes: the column holding a note; key_columns: columns whose top values the summary
        lists (default: a few low-cardinality columns); blob_mode: hex | base64 | summary
        (default: the provenance's, else hex). Returns the table's id."""
        tid = self.uid(id or ("t-" + str(name)), "t")
        spec = _Table(self, name, fields, rows, title or name, total, badges, detail, dates,
                      tags, notes, key_columns, blob_mode or self.blob_mode or "hex", note,
                      tid, chart)
        self._cur().blocks.append(spec)
        self._tables.append(spec)
        return tid

    # -- writing ------------------------------------------------------------------------------
    def write(self, path, protected=None, cancel=None, progress=None, every=500):
        """Write the report to path (and its part files next to it when a table is larger
        than limit html_rows_per_part). protected(path) true: ReportError before anything is
        written. cancel() true stops after the current row (the page is still complete HTML,
        marked incomplete); progress(n) every `every` rows. Returns a ReportResult."""
        if not path:
            raise ReportError("no report location given")
        path = os.path.abspath(path)
        self._guard(protected, path)
        self._protected, self._cancel, self._progress, self._every = (protected, cancel,
                                                                      progress, every)
        self._path = path
        self._part_files = []
        self._n = 0
        res = ReportResult(path)
        start = time.time()
        body = tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n",
                                      errors="backslashreplace")
        try:
            self._write_body(body, split=True)
            for part in self._part_files:
                self._finish_part(part)
            body.seek(0)
            with open(path, "w", encoding="utf-8", newline="\n",
                      errors="backslashreplace") as out:
                self._emit(out, body, None)
        finally:
            body.close()
            for part in self._part_files:
                try:
                    os.remove(part["tmp"])
                except OSError:
                    pass
        res.seconds = time.time() - start
        res.size = os.path.getsize(path)
        res.sha256 = _hash(path)
        res.rows = sum(t.written for t in self._tables)
        bad = [t for t in self._tables if not t.complete]
        res.complete = not bad
        res.stopped = bad[0].stopped if bad else ""
        res.files.append({"path": path, "size": res.size, "sha256": res.sha256,
                          "role": "report"})
        for part in self._part_files:
            d = OrderedDict([("path", part["path"]), ("size", part["size"]),
                             ("sha256", part["sha256"]), ("role", "part"),
                             ("table", part["spec"].name), ("first_row", part["first"]),
                             ("rows", part["count"])])
            res.files.append(d)
            res.parts.append(d)
        return res

    def render(self):
        """The whole page as text (no part files: every row stays in this one page); for
        small reports and tests."""
        self._protected, self._cancel, self._progress, self._every = None, None, None, 0
        self._path = None
        self._part_files = []
        self._n = 0
        body = tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n",
                                      errors="backslashreplace")
        try:
            self._write_body(body, split=False)
            body.seek(0)
            out = io.StringIO()
            self._emit(out, body, None)
            return out.getvalue()
        finally:
            body.close()

    @staticmethod
    def _guard(protected, path):
        if protected is not None and protected(path):
            raise ReportError("refusing to write %s: it is inside the evidence folder" % path)

    def _write_body(self, body, split):
        for sec in self._sections:
            h = "h2" if sec.level == 2 else "h3"
            body.write('<section class="rsec lvl%d" id="%s" data-toc="%s" aria-labelledby='
                       '"%s-h">\n<%s id="%s-h">%s</%s>\n' % (
                           sec.level, sec.id, sec.id, sec.id, h, sec.id,
                           sec.title_html if sec.title_html is not None else esc(sec.title),
                           h))
            for b in sec.blocks:
                if isinstance(b, _Table):
                    self._stream_table(b, body, split, sec.level + 1)
                else:
                    body.write(b)
                    body.write("\n")
            body.write("</section>\n")

    def _stream_table(self, spec, body, split, head_level):
        per = self.L["html_rows_per_part"] if split else 0
        sink = _Sink(self, spec, body, 1, 1, head_level)
        spec.pieces.append({"no": 1, "path": None, "first": 1, "sink": sink})
        n = 0
        cancel, progress, every = self._cancel, self._progress, self._every
        if self._stopped_all:
            spec.stopped = "not written: the export was stopped before this table"
            sink.close()
            spec.pieces[0]["count"] = 0
            return
        try:
            it = iter(spec.rows)
        except TypeError as e:
            it = iter(())
            spec.stopped = "by an error before the first row: %s" % e
        while True:
            if cancel is not None and cancel():
                spec.stopped = "by the user after %s" % _count(n, "row")
                self._stopped_all = True
                break
            try:
                row = next(it)
            except StopIteration:
                spec.complete = not spec.stopped
                break
            except Exception as e:      # noqa: BLE001 - a read error ends the table, marked so
                spec.stopped = "by an error after %s: %s" % (_count(n, "row"), e)
                break
            if per and sink.count >= per:
                spec.pieces[-1]["count"] = sink.count
                sink.close()
                sink = self._part_sink(spec, n + 1)
            try:
                sink.add(row)
            except Exception as e:  # noqa: BLE001 - an encode error ends the table, marked so
                spec.stopped = "by an error after %s: %s" % (_count(n, "row"), e)
                break
            n += 1
            self._n += 1
            if progress is not None and every and self._n % every == 0:
                progress(self._n)
        spec.pieces[-1]["count"] = sink.count
        sink.close()
        spec.written = n
        if progress is not None:
            progress(self._n)

    def _part_name(self, no):
        stem, ext = os.path.splitext(self._path)
        ext = ext or ".html"
        cand = "%s_part%03d%s" % (stem, no, ext)
        k = 2
        taken = set(p["path"] for p in self._part_files)
        while os.path.exists(cand) or cand in taken or cand == self._path:
            cand = "%s_part%03d_%d%s" % (stem, no, k, ext)
            k += 1
        return cand

    def _part_sink(self, spec, first):
        no = len(self._part_files) + 2
        final = self._part_name(no)
        self._guard(self._protected, final)
        fd, tmp = tempfile.mkstemp(suffix=".html.part")
        f = io.open(fd, "w", encoding="utf-8", newline="\n", errors="backslashreplace")
        part = {"no": no, "path": final, "tmp": tmp, "spec": spec, "first": first,
                "count": 0, "piece": len(spec.pieces) + 1}
        self._part_files.append(part)
        sink = _Sink(self, spec, f, len(spec.pieces) + 1, first, 2)
        spec.pieces.append({"no": no, "path": final, "first": first, "sink": sink,
                            "part": part})
        return sink

    def _finish_part(self, part):
        spec = part["spec"]
        piece = next(p for p in spec.pieces if p.get("part") is part)
        part["count"] = piece.get("count", 0)
        with io.open(part["tmp"], "r", encoding="utf-8", newline="\n") as body, \
                open(part["path"], "w", encoding="utf-8", newline="\n",
                     errors="backslashreplace") as out:
            self._emit(out, body, part)
        part["size"] = os.path.getsize(part["path"])
        part["sha256"] = _hash(part["path"])

    # -- the page -----------------------------------------------------------------------------
    def _rows_text(self):
        total = sum(t.written for t in self._tables)
        bad = [t for t in self._tables if not t.complete]
        text = _count(total, "row")
        if len(self._tables) > 1:
            text += " in %s" % _count(len(self._tables), "table")
        return text, bad

    def _emit(self, out, body, part):
        title = self.title if part is None else "%s (part %d)" % (self.title, part["no"])
        out.write('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
                  '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                  '<meta http-equiv="Content-Security-Policy" content="%s">\n'
                  '<meta name="generator" content="%s">\n<title>%s</title>\n'
                  '<style>%s</style>\n<script>%s</script>\n</head>\n<body>\n'
                  % (CSP, esc("%s %s" % (self.tool_name, self.tool_version)),
                     esc(title), CSS, JS))
        out.write('<a class="skip" href="#main">Skip to the report</a>\n')
        out.write(self._topbar(title))
        out.write('<div class="layout" id="layout">\n')
        toc = self._toc_entries(part)
        out.write('<button type="button" class="btn sm toc-show" id="toc-show" '
                  'aria-expanded="false" aria-controls="toc" title="Show sidebar">Show contents</button>\n')
        out.write(self._nav(toc))
        out.write('<main id="main" tabindex="-1">\n<noscript><p class="note warn">Search, '
                  'sorting, filters and row details need JavaScript, which is off: '
                  'everything else reads as it is, and each table shows its first rows as a '
                  'plain table.</p></noscript>\n')
        out.write(self._cover(part))
        out.write(self._print_toc(toc))
        if part is None:
            out.write(self._summary())
        for line in body:
            if line.startswith("\x00SLOT "):
                _slot, tid, level = line.split()
                out.write(self._table_head(tid, int(level), part))
            else:
                out.write(line)
        out.write('<footer class="foot">Written by %s %s on %s. Read-only: nothing was '
                  'written to the evidence. Self-contained: this file loads nothing from '
                  'anywhere.</footer>\n</main>\n</div>\n'
                  % (esc(self.tool_name), esc(self.tool_version), esc(self.exported)))
        out.write(_CHROME)
        out.write("</body>\n</html>\n")

    def _topbar(self, title):
        return ('<header class="topbar"><button type="button" class="btn toc-btn" id="act-toc" '
                'data-act="toc" aria-controls="toc" aria-expanded="false">Contents</button>'
                '<div class="brand">%s<small>%s %s · %s</small>'
                '</div><div class="tools js-only" role="toolbar" aria-label="Report settings">'
                '<button type="button" class="btn" id="act-theme" data-act="theme">Theme: '
                'System</button><button type="button" class="btn" id="act-density" '
                'data-act="density" aria-pressed="false">Density: Comfortable</button>'
                '<button type="button" class="btn" id="act-tz" data-act="tz" '
                'aria-pressed="false">Dates: UTC</button>'
                '<button type="button" '
                'class="btn" data-act="print">Print</button><button type="button" class="btn" '
                'data-act="help" aria-haspopup="dialog">Keys and filters (?)</button></div>'
                '</header>\n' % (esc(title), esc(self.tool_name), esc(self.tool_version),
                                 esc(self.kind)))

    def _toc_entries(self, part):
        """[(id, title, level, count text)] of the page."""
        out = [("cover", "Cover", 2, "")]
        if part is not None:
            spec = part["spec"]
            out.append((spec.tid, spec.name, 2, format(part["count"], ",")))
            return out
        if self._has_summary():
            out.append(("summary", "Summary", 2, ""))
        for sec in self._sections:
            out.append((sec.id, sec.title, sec.level, ""))
            for b in sec.blocks:
                if isinstance(b, _Table):
                    n = b.written
                    first = b.pieces[0].get("count", n) if b.pieces else n
                    cnt = format(first, ",") if first == n else "%s of %s" % (
                        format(first, ","), format(n, ","))
                    out.append((b.tid, str(b.title), sec.level + 1, cnt))
        return out

    def _nav(self, toc):
        out = ['<nav class="toc" id="toc" aria-label="Contents">\n<div class="toc-head js-only"><h2>Contents</h2>'
               '<button type="button" class="btn sm" id="toc-toggle" aria-expanded="true" '
               'aria-controls="toc" title="Hide sidebar">Hide</button></div>\n<div class="tocsearch '
               'js-only"><label for="rs" class="vh">Search the whole report</label><input '
               'id="rs" type="search" placeholder="Search the report" autocomplete="off" '
               'spellcheck="false"><p id="rs-stat" class="muted small" aria-live="polite">'
               '</p></div>\n']
        out.append(self._toc_list(toc, "tocl"))
        out.append("</nav>\n")
        return "".join(out)

    @staticmethod
    def _toc_list(toc, cls):
        out = ['<ol class="%s">' % cls]
        depth = [2]
        first = True
        for tid, title, level, cnt in toc:
            level = max(2, min(level, depth[-1] + 1))
            if first:
                depth = [level]
                first = False
            elif level > depth[-1]:
                out.append("<ol>")
                depth.append(level)
            else:
                out.append("</li>")
                while level < depth[-1]:
                    out.append("</ol></li>")
                    depth.pop()
            out.append('<li data-sec="%s"><a href="#%s"><span>%s</span>%s</a>' % (
                tid, tid, esc(title), '<span class="cnt">%s</span>' % esc(cnt) if cnt else ""))
        if not first:
            out.append("</li>")
            while len(depth) > 1:
                out.append("</ol></li>")
                depth.pop()
        out.append("</ol>")
        return "".join(out)

    def _print_toc(self, toc):
        return ('<section class="print-only" id="print-toc" aria-hidden="true"><h2>Contents'
                '</h2>%s</section>\n' % self._toc_list(toc[1:], "ptoc").replace(
                    ' data-sec="', ' data-p-sec="').replace('href="#', 'data-href="#'))

    def _cover(self, part):
        out = ['<section class="cover" id="cover" data-toc="cover" aria-labelledby="cover-h">'
               '<p class="eyebrow">%s · %s</p><h1 id="cover-h">%s</h1>' % (
                   esc(self.tool_name), esc(self.kind), esc(self.title))]
        if self.subtitle:
            out.append('<p class="sub">%s</p>' % esc(self.subtitle))
        rows_text, bad = self._rows_text()
        status = []
        if self._tables:
            if bad:
                status.append(badge("INCOMPLETE", "bad"))
            else:
                status.append(badge("Complete", "ok"))
            status.append(badge(rows_text, "info"))
        status.append(badge("Read-only", "muted"))
        if self._part_files:
            status.append(badge("%s" % _count(len(self._part_files) + 1, "file"), "warn"))
        out.append('<div class="statusline">%s</div>' % "".join(status))
        if part is not None:
            spec = part["spec"]
            out.append('<p class="note warn">This file is part %d of %d of the report %s: it '
                       'holds rows %s to %s of the table %s. The report and every part are '
                       'listed in <a href="%s">%s</a>.</p>' % (
                           part["no"], len(self._part_files) + 1, esc(self.title),
                           format(part["first"], ","),
                           format(part["first"] + part["count"] - 1, ","), esc(spec.name),
                           esc(quote(os.path.basename(self._path))),
                           esc(os.path.basename(self._path))))
        for t in bad:
            out.append('<p class="note bad">INCOMPLETE: %s%s stopped %s.</p>' % (
                "the table " if len(self._tables) > 1 else "the export",
                " %s" % esc(t.name) if len(self._tables) > 1 else "", esc(t.stopped)))
        facts = []
        if self.case_name:
            facts.append(("Case", self.case_name))
        facts.extend(self.facts)
        if self.database:
            facts.append(("Database", self.database))
        facts.append(("Exported", self.exported if "UTC" in self.exported or
                      self.exported.endswith("Z") else self.exported + " UTC"))
        facts.append(("Tool", "%s %s (Python %s, SQLite %s)" % (
            self.tool_name, self.tool_version, self.python, self.sqlite)))
        if self._tables:
            facts.append(("Rows", "%s%s" % (rows_text, "" if not bad else
                                             " — INCOMPLETE: stopped %s" % bad[0].stopped)))
        if self.blob_mode:
            facts.append(("Values", "NULL is shown as a grey NULL (a text 'NULL' is plain "
                          "text); BLOBs by size and decoded summary, their bytes embedded %s."
                          % {"hex": "as hex (lossless)", "base64": "as base64 (lossless)",
                             "summary": "only as SHA-256 (not lossless)"}.get(
                              self.blob_mode, self.blob_mode)))
        facts.extend(self.extra)
        out.append('<dl class="facts">%s</dl>' % "".join(
            "<dt>%s</dt><dd>%s</dd>" % (esc(k), _cellmark(v)) for k, v in facts))
        out.append("<h2>Evidence</h2>")
        out.append(self._evidence_html())
        out.append("</section>\n")
        return "".join(out)

    def _evidence_html(self):
        if not self.evidence:
            return '<p class="muted">No evidence file is named in this report.</p>'
        cards = []
        for label, f in self.evidence:
            f = f or {}
            role = f.get("role") or "file"
            size = f.get("size")
            mt = f.get("mtime_utc") or (_mtime_text(f["mtime_ns"]) if f.get("mtime_ns")
                                        else "")
            sha = f.get("sha256")
            if sha:
                hline = '<div class="mono">SHA-256 %s</div>' % esc(sha)
            else:
                hline = '<div class="mono bad">SHA-256 %s</div>' % esc(
                    f.get("error") or f.get("note") or "not computed")
            cards.append('<div class="evcard">%s%s<div class="path">%s</div><div class="small '
                         'muted">%s%s</div>%s</div>' % (
                             badge(role, "info"),
                             " <strong>%s</strong>" % esc(label) if label else "",
                             esc(f.get("path") or ""),
                             "%s bytes" % format(size, ",") if isinstance(size, int)
                             and not isinstance(size, bool) else "size unknown",
                             " · modified %s" % esc(mt) if mt else "", hline))
        return '<div class="evgrid">%s</div>' % "".join(cards)

    # -- summary ------------------------------------------------------------------------------
    def _has_summary(self):
        return bool(self._cards or self._summary_extra or self._part_files or
                    (self.auto_summary and self._tables))

    def _summary(self):
        if not self._has_summary():
            return ""
        cards = list(self._cards)
        extra = list(self._summary_extra)
        tables = self._tables if self.auto_summary else []
        if tables:
            total = sum(t.written for t in tables)
            bad = [t for t in tables if not t.complete]
            cards.append(card_html("Rows", format(total, ","),
                                   "in %s" % _count(len(tables), "table") if len(tables) > 1
                                   else tables[0].name,
                                   "bad" if bad else "ok"))
            labels = []
            for label, f in self.evidence:
                if (f or {}).get("role") in (None, "main") and (label or (f or {}).get("path")):
                    labels.append(label or os.path.basename(f.get("path") or ""))
            if labels:
                cards.append(card_html("Databases", format(len(labels), ","),
                                       ", ".join(labels[:3]) + (" …" if len(labels) > 3
                                                                else "")))
            lo = hi = None
            cols = []
            for t in tables:
                for c, _k, label in t.dates:
                    if c in t.stats.dmin:
                        cols.append("%s (%s)" % (t.fields[c], label))
                        lo = t.stats.dmin[c] if lo is None else min(lo, t.stats.dmin[c])
                        hi = t.stats.dmax[c] if hi is None else max(hi, t.stats.dmax[c])
            if lo is not None:
                cards.append(card_html("Date range (UTC)", Markup(
                    '%s<br><span class="small">to</span> %s' % (esc(_ms_text(lo)),
                                                               esc(_ms_text(hi)))),
                    "from " + ", ".join(cols[:4]) + (" …" if len(cols) > 4 else "")))
            tags = OrderedDict()
            colors = {}
            for t in tables:
                for k, n in t.stats.tags.items():
                    tags[k] = tags.get(k, 0) + n
                for k, c in t.tag_colors.items():
                    colors.setdefault(k, c[0])
            if tags:
                cards.append(card_html("Tags", format(len(tags), ","), None, None, True, Markup(
                    '<div>%s</div>' % " ".join('%s <span class="small muted">%s</span>' % (
                        chip(k, colors.get(k, "#94A3B8")), format(n, ","))
                        for k, n in tags.items()))))
            for t in tables:
                for c, counter in t.stats.badges.items():
                    if t.fields[c].lower() != "confidence" or not counter:
                        continue
                    tot = float(sum(counter.values())) or 1.0
                    tones = t.badge_cols.get(c, {})
                    bar = "".join('<span class="%s" style="width:%.2f%%" title="%s: %s"></span>'
                                  % (tones.get(k) or tone_of(k), 100 * n / tot, esc(k),
                                     format(n, ",")) for k, n in counter.items())
                    cards.append(card_html("Confidence" + (" · %s" % t.name
                                                           if len(tables) > 1 else ""),
                                           format(int(tot), ","), None, None, False, Markup(
                                               '<div class="mix" aria-hidden="true">%s</div>'
                                               '<div class="small">%s</div>' % (bar, " ".join(
                                                   "%s %s" % (badge(k, tones.get(k) or
                                                                    tone_of(k)),
                                                              format(n, ","))
                                                   for k, n in counter.items())))))
        if self._part_files:
            cards.append(card_html("Files", format(len(self._part_files) + 1, ","),
                                   "this report and %s (limit html_rows_per_part = %s rows "
                                   "per file)" % (_count(len(self._part_files), "part file"),
                                                  format(self.L["html_rows_per_part"], ",")),
                                   "warn"))
        out = ['<section class="rsec lvl2" id="summary" data-toc="summary" '
               'aria-labelledby="summary-h"><h2 id="summary-h">Summary</h2>']
        if cards:
            out.append(cards_html(cards))
        if self._part_files:
            out.append(self._parts_note())
        if tables:
            out.append(self._chart(tables))
            out.append(self._tops(tables))
        out.extend(extra)
        out.append("</section>\n")
        return "".join(out)

    def _parts_note(self):
        rows = []
        for t in self._tables:
            for pc in t.pieces:
                if pc.get("part") is None:
                    continue
                p = pc["part"]
                rows.append([Markup('<a href="%s">%s</a>' % (
                    esc(quote(os.path.basename(p["path"]))), esc(os.path.basename(p["path"])))),
                    t.name, "%s – %s" % (format(p["first"], ","),
                                              format(p["first"] + p["count"] - 1, ",")),
                    format(p.get("size") or 0, ","), p.get("sha256") or ""])
        return ('<p class="note warn">This export is split into %s so each opens quickly: a '
                'table with more than %s rows (limit html_rows_per_part) continues in part '
                'files next to this one. Keep them together.</p>%s' % (
                    _count(len(self._part_files) + 1, "file"),
                    format(self.L["html_rows_per_part"], ","),
                    simple_table_html(["Part file", "Table", "Rows", "Size (bytes)", "SHA-256"],
                                      rows, num=(3,), mono=(4,), caption="Part files")))

    def _chart(self, tables):
        days = {}
        over = False
        cols = []
        for t in tables:
            if not t.chart or not t.dates or not t.stats.days:
                continue
            c = t.dates[0][0]
            cols.append("%s.%s" % (t.name, t.fields[c]) if len(tables) > 1 else t.fields[c])
            over = over or t.stats.days_over
            for d, n in t.stats.days.items():
                days[d] = days.get(d, 0) + n
        if not days:
            return ""
        lo, hi = min(days), max(days)
        span = hi - lo + 1
        nb = min(CHART_BINS, span)
        width = int(math.ceil(span / float(nb)))
        nb = int(math.ceil(span / float(width)))
        bins = [0] * nb
        for d, n in days.items():
            bins[(d - lo) // width] += n
        top = max(bins) or 1
        W, H, L, B, T = 720.0, 170.0, 44.0, 24.0, 10.0
        bw = (W - L - 6) / nb
        out = ['<h3>Activity over time</h3><p class="muted small">Rows per %s by %s (UTC)%s.'
               '</p>' % ("day" if width == 1 else "%d days" % width, esc(", ".join(cols)),
                        "; days after the first %s distinct ones are not counted"
                        % format(DAYS_KEPT, ",") if over else "")]
        out.append('<svg class="chart" viewBox="0 0 %d %d" role="img" aria-labelledby='
                   '"chart-t"><title id="chart-t">Rows over time, %s to %s, busiest %s: %s '
                   'rows</title>' % (W, H, _day_text(lo), _day_text(hi),
                                     "day" if width == 1 else "period", format(top, ",")))
        for f in (0.0, 0.5, 1.0):
            y = T + (H - B - T) * (1 - f)
            out.append('<line class="grid" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                       '<text class="axis" x="%.1f" y="%.1f" text-anchor="end">%s</text>' % (
                           L, y, W - 4, y, L - 6, y + 4, format(int(round(top * f)), ",")))
        for i, n in enumerate(bins):
            if not n:
                continue
            h = (H - B - T) * n / float(top)
            a = lo + i * width
            label = _day_text(a) if width == 1 else "%s to %s" % (
                _day_text(a), _day_text(min(hi, a + width - 1)))
            out.append('<rect class="bar" x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="1">'
                       '<title>%s: %s rows</title></rect>' % (
                           L + i * bw + 0.5, H - B - h, max(1.0, bw - 1), max(h, 0.5),
                           label, format(n, ",")))
        out.append('<text class="axis" x="%.1f" y="%.1f">%s</text><text class="axis" x="%.1f" '
                   'y="%.1f" text-anchor="end">%s</text></svg>' % (
                       L, H - 6, _day_text(lo), W - 4, H - 6, _day_text(hi)))
        rows = []
        for i, n in enumerate(bins):
            if n:
                a = lo + i * width
                rows.append([_day_text(a) if width == 1 else "%s to %s" % (
                    _day_text(a), _day_text(min(hi, a + width - 1))), format(n, ",")])
        out.append(details_html("The chart's numbers (%s)" % _count(len(rows), "period"),
                                simple_table_html(["Period (UTC)", "Rows"], rows, num=(1,),
                                                  caption="Rows over time")))
        return "".join(out)

    def _tops(self, tables):
        cards = []
        for t in tables:
            for c, counter in t.stats.keys.items():
                if not counter:
                    continue
                items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_SHOWN]
                tot = float(sum(counter.values())) or 1.0
                lis = "".join('<li><div class="bar" style="width:%.1f%%"></div><span>%s</span>'
                              '<span class="num">%s</span></li>' % (
                                  100 * n / tot, esc(k[:80]), format(n, ","))
                              for k, n in items)
                sub = "%s distinct" % format(len(counter), ",")
                if c in t.stats.key_over:
                    sub = ("more than %s distinct: counted among the first %s (limit "
                           "html_top_distinct)" % (format(len(counter), ","),
                                                   format(len(counter), ",")))
                cards.append(card_html("Top values · %s%s" % (
                    t.name + "." if len(tables) > 1 else "", t.fields[c]), Markup(""), sub,
                    None, False, Markup('<ul class="tops">%s</ul>' % lis)))
        if not cards:
            return ""
        return "<h3>Top values</h3>" + cards_html(cards)

    def _table_head(self, tid, level, part):
        spec = next((t for t in self._tables if t.tid == tid), None)
        if spec is None:
            return ""
        h = "h%d" % max(2, min(level, 4))
        if part is not None:
            count = part["count"]
            rows = "rows %s to %s of %s" % (format(part["first"], ","),
                                            format(part["first"] + count - 1, ","),
                                            format(spec.written, ","))
        else:
            count = spec.pieces[0].get("count", spec.written) if spec.pieces else spec.written
            rows = _count(spec.written, "row")
        out = ['<%s id="%s-h">%s %s%s</%s>' % (
            h, tid, esc(spec.title), badge(rows, "info"),
            badge("INCOMPLETE", "bad") if not spec.complete else "", h)]
        if spec.note:
            out.append('<p class="muted">%s</p>' % _cellmark(spec.note))
        if not spec.complete and spec.stopped:
            out.append('<p class="note bad">INCOMPLETE: the rows stopped %s.</p>'
                       % esc(spec.stopped))
        if spec.dates:
            out.append('<p class="muted small">Dates: %s — shown next to the raw value, in '
                       'the time zone chosen with the Dates button (UTC at first).</p>'
                       % esc(", ".join(
                           "%s (%s)" % (spec.fields[c], label) for c, _k, label in spec.dates)))
        if part is None and len(spec.pieces) > 1:
            links = []
            for pc in spec.pieces[1:]:
                p = pc["part"]
                links.append('<li><a href="%s">%s</a>: rows %s to %s (%s bytes, SHA-256 '
                             '<span class="mono">%s</span>)</li>' % (
                                 esc(quote(os.path.basename(p["path"]))),
                                 esc(os.path.basename(p["path"])), format(p["first"], ","),
                                 format(p["first"] + p["count"] - 1, ","),
                                 format(p.get("size") or 0, ","), esc(p.get("sha256") or "")))
            out.append('<div class="note warn"><p>This table has %s: rows 1 to %s are in this '
                       'file; the others continue in %s next to it (limit html_rows_per_part = '
                       '%s rows per file):</p><ul>%s</ul></div>' % (
                           _count(spec.written, "row"), format(count, ","),
                           _count(len(spec.pieces) - 1, "part file"),
                           format(self.L["html_rows_per_part"], ","), "".join(links)))
        elif part is not None:
            out.append('<p class="note warn">Part %d: rows %s to %s of the %s of this table. '
                       'The first rows and the other parts: <a href="%s">%s</a>.</p>' % (
                           part["no"], format(part["first"], ","),
                           format(part["first"] + count - 1, ","),
                           _count(spec.written, "row"),
                           esc(quote(os.path.basename(self._path))),
                           esc(os.path.basename(self._path))))
        return "".join(out) + "\n"


_CHROME = (
    '<aside id="drawer" class="drawer" role="dialog" aria-modal="false" '
    'aria-labelledby="drawer-title" hidden><div class="dhead"><h2 id="drawer-title" '
    'tabindex="-1">Row</h2><p id="drawer-pos" class="muted small"></p><div class="dtools">'
    '<button type="button" class="btn sm" data-act="d-prev">Previous (k)</button>'
    '<button type="button" class="btn sm" data-act="d-next">Next (j)</button>'
    '<button type="button" class="btn sm" data-act="d-json">Copy row as JSON</button>'
    '<button type="button" class="btn sm" data-act="d-tsv">Copy row as TSV</button>'
    '<button type="button" class="btn sm" data-act="d-close">Close (Esc)</button></div></div>'
    '<div id="drawer-body" class="dbody"></div></aside>\n'
    '<div id="pop" class="pop" role="dialog" aria-label="Options" hidden></div>\n'
    '<div id="blobi" class="overlay" hidden><div class="panel bpanel" role="dialog" '
    'aria-modal="true" aria-labelledby="bi-title"></div></div>\n'
    '<div id="help" class="overlay" hidden><div class="panel" role="dialog" aria-modal="true" '
    'aria-labelledby="help-title"><h2 id="help-title">Keys and filters</h2>'
    '<div class="keys"><kbd>/</kbd><span>Search the table in view (the report search when no '
    'table is in view)</span><kbd>j</kbd><span>Next row (also the arrow keys in a table)'
    '</span><kbd>k</kbd><span>Previous row</span><kbd>Enter</kbd><span>Open the row: every '
    'value in full, dates decoded, BLOBs, provenance</span><kbd>Esc</kbd><span>Close the row, '
    'a menu or this help</span><kbd>c</kbd><span>Copy the value clicked last (or the row)'
    '</span><kbd>?</kbd><span>This help</span><span>Shift+click</span><span>Add a column to '
    'the sort</span></div>'
    '<h3>Filter expressions (the box under a column name)</h3><div class="stw"><table '
    'class="st"><thead><tr><th scope="col">Type</th><th scope="col">Means</th></tr></thead>'
    '<tbody><tr><td class="mono">text</td><td>contains the text, any case</td></tr>'
    '<tr><td class="mono">!text</td><td>does not contain it (NULL matches)</td></tr>'
    '<tr><td class="mono">a%b_c</td><td>LIKE pattern: % any run of characters, _ one</td>'
    '</tr><tr><td class="mono">=x &lt;&gt;x &gt;x &gt;=x &lt;x &lt;=x</td><td>compare: '
    'numbers as numbers; on a date column YYYY-MM-DD [HH:MM[:SS]] compares the date (in the '
    'time shown: UTC or local)</td></tr><tr><td class="mono">a~b</td><td>between a and b, '
    'inclusive</td></tr><tr><td class="mono">"x"</td><td>exactly the text x</td></tr>'
    '<tr><td class="mono">*=x ^=x $=x</td><td>contains / starts with / ends with (!*= !^= '
    '!$= for not)</td></tr><tr><td class="mono">/regex/ /regex/i</td><td>regular expression '
    '(i: any case)</td></tr><tr><td class="mono">NULL, NOT NULL, EMPTY, NOT EMPTY</td><td>'
    'the value is (not) NULL; (not) NULL or empty text</td></tr><tr><td class="mono">IN (a, '
    'b), NOT IN (a, b)</td><td>equal to one of / none of the values</td></tr></tbody></table>'
    '</div><p class="muted small">The same language as the tool\'s column filters, without '
    '{..} AND / OR. Search, filters and sort run in this page only; nothing is sent anywhere. '
    '</p><button type="button" class="btn" data-act="help-close">Close</button></div>'
    '</div>\n'
    '<div id="toast" class="toast" role="status" aria-live="polite" hidden></div>\n'
    '<button type="button" id="totop" class="btn totop" data-act="totop" hidden>Back to top'
    '</button>\n')


# -- the one export writer's HTML format ---------------------------------------------------------
def write_export(path, columns, rows, info, blob_mode="hex", cancel=None, progress=None,
                 every=500, protected=None, options=None):
    """engine.export.write_rows' HTML format: one table of rows with the export's provenance.
    options (all optional): title, case_name, subtitle, kind, table (name), section, badges,
    detail, dates, tags, notes, key_columns, limits. Returns a ReportResult."""
    o = dict(options or {})
    source = str(info.get("source") or "Export")
    rep = Report(o.get("title") or source, provenance=info, case_name=o.get("case_name", ""),
                 subtitle=o.get("subtitle", ""), kind=o.get("kind", "Export"),
                 limits=o.get("limits"))
    rep.add_section(o.get("section") or "Rows")
    rep.add_table(o.get("table") or source, columns, rows, badges=o.get("badges"),
                  detail=o.get("detail"), dates=o.get("dates"), tags=o.get("tags"),
                  notes=o.get("notes"), key_columns=o.get("key_columns"), blob_mode=blob_mode)
    return rep.write(path, protected, cancel, progress, every)


# -- reading a report back (tests, tools) --------------------------------------------------------
_DATA_RE = re.compile(r'<script type="application/json" class="gdata" data-grid="([^"]+)">'
                      r'(.*?)</script>', re.S)
_META_RE = re.compile(r'<script type="application/json" class="gmeta" data-grid="([^"]+)">'
                      r'(.*?)</script>', re.S)


def read_rows(source):
    """{table id: {"name", "fields", "meta", "rows"}} of a report page (its text or path),
    the rows decoded back to the values written (the decoded dates left out)."""
    if not source.lstrip().startswith("<"):
        with open(source, encoding="utf-8") as f:
            source = f.read()
    out = OrderedDict()
    for tid, text in _META_RE.findall(source):
        m = json.loads(text, object_pairs_hook=OrderedDict)
        out[tid] = {"name": m["name"], "fields": m["fields"], "meta": m, "rows": []}
    for tid, text in _DATA_RE.findall(source):
        t = out.get(tid)
        if t is None:
            continue
        nf = len(t["fields"])
        for r in json.loads(text, object_pairs_hook=OrderedDict):
            t["rows"].append([decode_cell(c) for c in r[:nf]])
    return out
