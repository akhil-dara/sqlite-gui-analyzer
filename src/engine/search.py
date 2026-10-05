"""Search matching rules, row-identity aware (WITHOUT ROWID, views, native tables).

Modes: ci / cs (contains, any case / exact case), ex (equals), sw / ew (starts / ends with, any
case), rx (regular expression), blob (text anywhere, any case, BLOBs included), hex (byte
pattern), col (column names).

TEXT and numeric values are matched as text. BLOB values are matched as bytes (engine.bytesearch):
- blob mode, and every text mode with deep_blob, looks for the term as UTF-8, UTF-16LE and
  UTF-16BE bytes at any offset (ex / sw / ew anchored to the whole value, its start or its end;
  a byte order mark before and one NUL terminator after the text are allowed there);
- rx with deep_blob runs the regex on the BLOB read as UTF-8 and as UTF-16LE / UTF-16BE (from
  byte 0 and from byte 1);
- hex matches a byte pattern ('??' = any byte) at byte boundaries in BLOBs and in the stored
  bytes of TEXT (database encoding); blob mode with deep_blob also does, when the term reads as
  hex;
- decoded=True also matches the text rule against the strings engine.decode finds in a BLOB.
BLOB hits say where: 'encoding' (utf-8 / utf-16le / utf-16be / hex / decoded) and 'offset' (the
byte offset of the match; None for text and decoded hits), and 'value' is a readable snippet.
"""

import re

from .backends import like_escape, text_of
from .bytesearch import (HexPattern, TextInBytes, ascii_case_run, invariant_run, needles,
                         regex_in_bytes, snippet, text_snippet)
from .fileformat.record import InvalidText
from .schema import Locator, column_affinity, quote_ident

MODES = ("ci", "cs", "ex", "sw", "ew", "rx", "blob", "hex", "col")
TEXT_MODES = ("ci", "cs", "ex", "sw", "ew")
_ANCHORS = {"ex": "whole", "sw": "start", "ew": "end"}
NUMERIC_AFFINITIES = ("INTEGER", "REAL", "NUMERIC")


class DecodedSearchUnavailable(RuntimeError):
    """Search in decoded BLOB content was asked for, but engine.decode cannot be loaded."""


def load_decoder():
    """engine.decode.decoded_strings, imported only when decoded content is searched."""
    try:
        from .decode import decoded_strings
    except ImportError as e:
        raise DecodedSearchUnavailable("Searching decoded BLOB content needs the engine.decode "
                                       "package, which could not be loaded: %s" % e)
    return decoded_strings


_LOWER_PROBE = b"\x00\x80\xc3\x80XY\x00H\x00I\x00Z\xff"


def blob_lower_works(conn):
    """Whether lower() on a BLOB, on this connection, lowers exactly its ASCII letters A-Z and
    keeps every other byte (NULs, invalid UTF-8), and instr() finds UTF-8 and UTF-16 text in the
    result (and nothing else): SQLite's built-in functions do on a UTF-8 database, an ICU lower()
    does not. (instr() on text skips only UTF-8 continuation bytes as match starts, which an
    ASCII or NUL first byte never is.)"""
    p = _LOWER_PROBE
    try:
        got = conn.execute("SELECT CAST(lower(?) AS BLOB), instr(lower(?), ?) > 0, "
                           "instr(lower(?), ?) > 0, instr(lower(?), ?) > 0, instr(lower(?), ?)",
                           (p, p, "xy", p, "h\x00i\x00", p, "\x00h\x00i", p, "hi")).fetchone()
    except Exception:           # noqa: BLE001 - any failure means: do not rely on it
        return False
    return tuple(got) == (p.lower(), 1, 1, 1, 0)


def check_term(term, mode):
    """Raise ValueError with a readable message when `term` cannot be searched in `mode`
    (a malformed hex pattern or regular expression)."""
    if mode == "hex":
        HexPattern(term)
    elif mode == "rx":
        try:
            re.compile(term)
        except re.error as e:
            raise ValueError("not a valid regular expression: %s" % e)


# Every character of the text SQLite or Python gives a number: digits, sign, decimal point,
# exponent, and inf / Inf / nan
_NUMBER_TEXT_CHARS = frozenset("0123456789+-.einfa")


def number_text_can_contain(term):
    """Whether `term` (in any case) could occur inside the text of an integer or real."""
    return set(term.lower()) <= _NUMBER_TEXT_CHARS


def regex_literal_hint(pattern):
    """Longest literal substring every match of `pattern` must contain ("" if none).

    Used as a LIKE pre-filter so regex search only examines candidate rows, so it must never
    name text a match can lack: a group or repeat joins the literal before and after it only
    when it is itself a fixed string ('ab(cd)' -> 'abcd', 'a(\\d)b' -> 'a' or 'b', 'hel+o' ->
    'hel' or 'lo', 'x(a|b)y' -> 'x' or 'y').
    """
    try:
        from re import _parser as _sp, _constants as _sc
    except ImportError:
        import sre_parse as _sp
        import sre_constants as _sc
    try:
        parsed = _sp.parse(pattern)
    except Exception:
        return ""
    LITERAL, SUBPATTERN = _sc.LITERAL, _sc.SUBPATTERN
    MAX_REPEAT, MIN_REPEAT = _sc.MAX_REPEAT, _sc.MIN_REPEAT

    def runs(items):
        """(literal runs every match of items contains, the fixed string items always match
        or None)."""
        out, buf, fixed = [], [], True
        for op, av in items:
            if op == LITERAL:
                buf.append(chr(av))
                continue
            if op == SUBPATTERN:
                sub, sub_fixed = runs(av[-1])
                if sub_fixed is not None:
                    buf.append(sub_fixed)
                    continue
                sub_min = sub_max = 1
            elif op in (MAX_REPEAT, MIN_REPEAT):
                sub, sub_fixed = runs(av[2])
                sub_min, sub_max = av[0], av[1]
                if sub_fixed is not None and sub_min == sub_max:
                    buf.append(sub_fixed * sub_min)     # a fixed count of a fixed string
                    continue
            else:
                sub, sub_fixed, sub_min, sub_max = [], None, 0, 0
            fixed = False
            if sub_fixed and sub_min >= 1:
                # sub_fixed repeated at least sub_min times: its first copies follow the text
                # before, its last copies precede the text after
                rep = sub_fixed * sub_min
                out.append("".join(buf) + rep)
                buf = [rep]
                continue
            if buf:
                out.append("".join(buf))
            buf = []
            if sub_min >= 1:
                out.extend(sub)                 # a required group: its own runs are required
        if buf:
            out.append("".join(buf))
        return out, ("".join(buf) if fixed else None)

    found, _fixed = runs(parsed)
    return max(found, key=len) if found else ""


def value_type(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "INTEGER"
    if isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "REAL"
    if isinstance(v, InvalidText):
        return "TEXT"
    if isinstance(v, bytes):
        return "BLOB"
    return "TEXT"


def truncate(s, n=220):
    s = "" if s is None else str(s)
    return s[:n] + "..." if len(s) > n else s


class Matcher(object):
    """The matching rule of one search.

    term, mode: see MODES. deep_blob: text modes also search BLOB values (as bytes) and columns
    declared BLOB; in blob mode, the term is also tried as a hex pattern. decoded: BLOB values
    are also searched through the strings engine.decode finds in them (raises
    DecodedSearchUnavailable when that package is missing). encoding: the database text
    encoding, which gives the stored bytes of TEXT values for hex patterns. sql_lower: SQLite's
    lower() folds exactly the ASCII letters of a BLOB and instr() finds bytes in the result
    (see blob_lower_works), so the SQL pre-filter can look for an ASCII term in any case.

    Raises ValueError for a malformed pattern in hex mode, re.error for a bad regex.
    """

    def __init__(self, term, mode, deep_blob, decoded=False, encoding="utf-8", sql_lower=False):
        self.term, self.mode, self.deep_blob = term, mode, deep_blob
        self.decoded, self.encoding, self.sql_lower = decoded, encoding, sql_lower
        self.lower = term.lower()
        self.rx = re.compile(term) if mode == "rx" else None
        self.hex = None
        if mode == "hex":
            self.hex = HexPattern(term)
        elif mode == "blob" and deep_blob:
            try:
                self.hex = HexPattern(term)
            except ValueError:
                self.hex = None         # not a hex pattern: searched as text only
        self.in_bytes = None
        if mode == "blob" or (deep_blob and mode in TEXT_MODES):
            self.in_bytes = TextInBytes(term, fold_case=mode not in ("cs", "ex"),
                                        anchor=_ANCHORS.get(mode))
        self.rx_in_bytes = mode == "rx" and deep_blob
        self._decode = load_decoder() if decoded and mode in TEXT_MODES + ("rx", "blob") else None
        self._skip = {}

    def skip_column(self, decl_type):
        """Columns declared BLOB are only searched in blob and hex mode, with Deep BLOB, or when
        decoded content is searched."""
        skip = self._skip.get(decl_type)
        if skip is None:
            skip = self._skip[decl_type] = "BLOB" in (decl_type or "").upper() and \
                self.mode not in ("blob", "hex") and not self.deep_blob and not self.decoded
        return skip

    def text_hit(self, v):
        """Display text if a TEXT or numeric value matches as text, else None (BLOBs: blob_hit)."""
        if v is None:
            return None
        if isinstance(v, bytes) and not isinstance(v, InvalidText):
            return None
        s = text_of(v)
        m, t = self.mode, self.term
        ok = (m == "cs" and t in s) or (m == "ex" and s == t) or \
             (m == "sw" and s.lower().startswith(self.lower)) or \
             (m == "ew" and s.lower().endswith(self.lower)) or \
             (m == "rx" and self.rx.search(s) is not None) or \
             (m in ("ci", "blob") and self.lower in s.lower())
        return truncate(s) if ok else None

    def text_span(self, s):
        """(start, end) of the match in the string s under the text rule, or None."""
        m = self.mode
        if m == "rx":
            found = self.rx.search(s)
            return found.span() if found else None
        if m == "cs":
            i = s.find(self.term)
            return (i, i + len(self.term)) if i >= 0 else None
        if m == "ex":
            return (0, len(s)) if s == self.term else None
        low = s.lower()
        if m == "sw":
            return (0, len(self.term)) if low.startswith(self.lower) else None
        if m == "ew":
            return (max(0, len(s) - len(self.term)), len(s)) if low.endswith(self.lower) else None
        if m in ("ci", "blob"):
            i = low.find(self.lower)
            return (i, i + len(self.term)) if i >= 0 else None
        return None

    def blob_hit(self, data):
        """(shown, type, encoding, offset) if the BLOB `data` matches, else None."""
        found = None
        if self.in_bytes is not None:
            found = self.in_bytes.search(data)
        elif self.rx_in_bytes:
            found = regex_in_bytes(self.rx, data)
        if found is not None:
            start, end, label = found
            return snippet(data, start, end, label), "BLOB", label, start
        if self.hex is not None:
            pos = self.hex.find(data)
            if pos is not None:
                return snippet(data, pos, pos + self.hex.length, "hex"), "blob_hex", "hex", pos
        if self._decode is not None:
            for s in self._decode(data):
                span = self.text_span(s)
                if span is not None:
                    return text_snippet(s, span[0], span[1]), "BLOB", "decoded", None
        return None

    def cell_hit(self, v):
        """(shown, type, encoding, offset) if the value matches, else None."""
        if v is None:
            return None
        if isinstance(v, bytes) and not isinstance(v, InvalidText):
            return self.blob_hit(v)
        if self.mode != "hex":
            shown = self.text_hit(v)
            if shown is not None:
                return shown, value_type(v), "text", None
        if self.hex is not None and not isinstance(v, (int, float)):
            data = bytes(v) if isinstance(v, bytes) else v.encode(self.encoding, "surrogatepass")
            pos = self.hex.find(data)
            if pos is not None:
                return snippet(data, pos, pos + self.hex.length, "hex"), value_type(v), "hex", pos
        return None

    def _text_sql(self, q, numeric, skip_numbers):
        """(sql, params) selecting the TEXT / numeric values of column q that can match."""
        m = self.mode
        esc = like_escape(self.term)
        if m == "blob":
            part, params = "CAST(%s AS TEXT) LIKE ? ESCAPE '\\'" % q, ["%" + esc + "%"]
        elif m == "cs":
            part, params = "instr(%s, ?) > 0" % q, [self.term]
        elif m == "ex":
            part, params = "%s = ?" % q, [self.term]
        elif m == "sw":
            part, params = "%s LIKE ? ESCAPE '\\'" % q, [esc + "%"]
        elif m == "ew":
            part, params = "%s LIKE ? ESCAPE '\\'" % q, ["%" + esc]
        else:
            part, params = "%s LIKE ? ESCAPE '\\'" % q, ["%" + esc + "%"]
        if skip_numbers and numeric:
            part = "(%s >= '' COLLATE BINARY AND %s)" % (q, part)
        return part, params

    def _blob_sql(self, q, hint):
        """[(sql, params)] selecting the values of column q that can match as bytes."""
        out = []
        any_blob = self._decode is not None
        found_in, haystack = [], "CAST(%s AS BLOB)" % q
        if self.in_bytes is not None and self.sql_lower and self.in_bytes.fold and \
                self.in_bytes.ascii:
            # lower() folds the BLOB's ASCII letters exactly as bytes.lower(): the lowered term,
            # as text (all ASCII), is in the lowered BLOB whenever the term matches in any case
            found_in = [n.decode("ascii") for n in needles(self.term.lower())]
            haystack = "lower(%s)" % q
        elif self.in_bytes is not None:
            found_in = self.in_bytes.sql_needles()
        elif self.rx_in_bytes:
            # Without IGNORECASE (and no inline flags that could set it) the literal is matched
            # exactly as written. Otherwise its caseless characters are certain, and so are its
            # ASCII characters up to ASCII case (but i, k, s), which lower() folds exactly
            run = invariant_run(hint, strict=True)
            folded = ascii_case_run(hint) if self.sql_lower else ""
            if "(?" not in self.term and not self.rx.flags & re.IGNORECASE:
                found_in = needles(hint)
            elif folded and len(folded) >= len(run):
                found_in = [n.decode("ascii") for n in needles(folded.lower())]
                haystack = "lower(%s)" % q
            else:
                found_in = needles(run) if run else []
        if (self.in_bytes is not None or self.rx_in_bytes) and not found_in:
            any_blob = True
        if any_blob:
            out.append(("typeof(%s) = 'blob'" % q, []))
        elif found_in:
            out.append(("(typeof(%s) = 'blob' AND (%s))"
                        % (q, " OR ".join(["instr(%s, ?) > 0" % haystack] * len(found_in))),
                        list(found_in)))
        if self.hex is not None:
            out.append(("(typeof(%s) IN ('text', 'blob') AND instr(CAST(%s AS BLOB), ?) > 0)"
                        % (q, q), [self.hex.literal]))
        return out

    def sql_where(self, columns, decl_types=None):
        """WHERE clause selecting every row that can match (it may over-match: Python re-checks),
        or ("", []) when every row has to be read.

        Text rules use LIKE / instr / =. With the declared types, a numeric column's numbers are
        skipped without being turned into text when the term cannot occur in a number's text:
        "x >= '' COLLATE BINARY" holds only for TEXT and BLOB values (SQLite orders every number
        before any text), so text stored in the column is still searched. A regex is narrowed by
        a literal every match must contain (any case). BLOB values searched as bytes are
        selected exactly: by instr() on bytes every match contains, else by typeof().
        """
        hint = ""
        text_rule = self
        if self.mode == "rx":
            hint = regex_literal_hint(self.term)
            if not hint:
                return "", []           # nothing every match must contain: read every row
            text_rule = Matcher(hint, "ci", False)
        elif self.mode == "hex":
            text_rule = None
        skip_numbers = text_rule is not None and decl_types is not None and \
            text_rule.mode != "ex" and not number_text_can_contain(text_rule.term)
        parts, params = [], []
        for i, name in enumerate(columns):
            q = quote_ident(name)
            alts = []
            if text_rule is not None:
                numeric = decl_types is not None and \
                    column_affinity(decl_types[i]) in NUMERIC_AFFINITIES
                alts.append(text_rule._text_sql(q, numeric, skip_numbers))
            alts.extend(self._blob_sql(q, hint))
            if len(alts) == 1:
                parts.append(alts[0][0])
            else:
                parts.append("(%s)" % " OR ".join(sql for sql, _p in alts))
            for _sql, p in alts:
                params.extend(p)
        return " OR ".join(parts), params


def _hit(table, column, locator, found):
    shown, typ, encoding, offset = found
    return {"table": table, "column": column, "locator": locator, "rowid": locator.display(),
            "value": shown, "type": typ, "encoding": encoding, "offset": offset}


def match_row(table, columns, decl_types, locator, row, matcher, emit):
    """Check one row; call emit(hit) per matching column. Returns number of hits."""
    n = 0
    for i, v in enumerate(row):
        if i >= len(columns) or matcher.skip_column(decl_types[i]):
            continue
        found = matcher.cell_hit(v)
        if found is not None:
            emit(_hit(table, columns[i], locator, found))
            n += 1
    return n


def search_records(records, matcher, limit=500, cancel=None):
    """Search records that are not rows of a table's b-tree (e.g. recovered from freed pages).

    records: iterable of dicts with 'table', 'columns', 'values' and optionally 'decl_types'
    (default: none, so no column is skipped), 'locator' (default: the record's ordinal in this
    search, carrying its values), 'rowid' (shown instead of the locator), 'source' and
    'provenance' (a dict copied into each hit, e.g. page and cell offset). Yields the hit dicts
    of match_row plus 'row', 'source' and the provenance fields, for at most `limit` records
    (None: every one).
    """
    found = 0
    hits = []
    for ordinal, rec in enumerate(records):
        if cancel is not None and cancel():
            return
        cols, values = list(rec["columns"]), list(rec["values"])
        cols += ["col%d" % i for i in range(len(cols), len(values))]
        decl = list(rec.get("decl_types") or ())
        decl += [""] * (len(cols) - len(decl))
        locator = rec.get("locator") or Locator("ordinal", ordinal, snapshot=(cols, values))
        del hits[:]
        match_row(rec["table"], cols, decl, locator, values, matcher, hits.append)
        if not hits:
            continue
        for h in hits:
            if rec.get("rowid") is not None:
                h["rowid"] = rec["rowid"]
            h["row"] = values
            h["source"] = rec.get("source", "Records")
            h.update(rec.get("provenance") or {})
            yield h
        found += 1
        if limit is not None and found >= limit:
            return
