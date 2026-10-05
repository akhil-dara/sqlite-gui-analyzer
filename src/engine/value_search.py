"""Find one value everywhere: every column of every table (optionally views), and records
recovered from WAL frames and freed pages.

The value is matched by what it is, not as a search term:

  a number     equal numbers (5 = 5.0), and text or BLOB bytes that read exactly as it
               ('5', x'35'), whatever the column's declared type;
  text         equal text, a number that reads exactly as it, and BLOBs holding its bytes
               (UTF-8, or the database's encoding);
  a BLOB       equal bytes, and text whose stored bytes are those bytes.

With contains=True (text and BLOBs) a value holding it inside also matches: text containing
the text, BLOBs holding its bytes as UTF-8, UTF-16LE or UTF-16BE (a BLOB value: its bytes) at
any offset. Every hit says whether the WHOLE value matched or it is CONTAINED in a larger one.

Tables are searched in parallel through Session.search_tables with a ValueMatcher, whose SQL
pre-filter is exact (IN) or instr() on at most NEEDLE_CAP bytes; Python confirms every hit.
"""

from .bytesearch import snippet
from .filters import _number, real_text
from .fileformat.record import InvalidText
from .schema import quote_ident
from .search import search_records, truncate, value_type

NEEDLE_CAP = 64             # bytes of a long value the SQL pre-filter looks for
ROW_LIMIT = 1000            # matching rows kept per table (and per recovered source)
WHOLE, CONTAINED = "whole", "contained"
_COMMON_TEXT = frozenset(("true", "false", "yes", "no", "null", "none", "y", "n", "t", "f",
                          "on", "off"))


def number_texts(n):
    """The texts a number is written as: '5' for 5; '2.5' for 2.5; '5.0' and '5' for 5.0."""
    if isinstance(n, bool):
        n = int(n)
    if isinstance(n, int):
        return [str(n)]
    out = [real_text(n), repr(n)]
    if n == n and abs(n) != float("inf") and n.is_integer():
        out.append(str(int(n)))
    return list(dict.fromkeys(out))


def is_common(value):
    """True for values so common that finding them elsewhere may be chance: small numbers
    (below 100 in size), one- or two-character text, words like true / false, tiny BLOBs."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return abs(value) < 100
    if isinstance(value, bytes):
        return len(value) <= 2
    s = value.strip()
    return len(s) <= 2 or s.lower() in _COMMON_TEXT or \
        (_number(s) is not None and abs(_number(s)) < 100)


class ValueMatcher(object):
    """Search rule for one value (the interface of engine.search.Matcher that Session.search
    uses: skip_column, sql_where, cell_hit)."""

    mode = "value"

    def __init__(self, value, contains=False, encoding="utf-8"):
        if isinstance(value, bool):
            value = int(value)
        self.value, self.encoding = value, encoding
        self.number = None
        self.texts, self.blobs, self.needles = [], [], []
        if isinstance(value, (int, float)):
            self.number = value
            self.texts = number_texts(value)
            self.blobs = [t.encode("utf-8") for t in self.texts]
            contains = False            # numbers match whole values only
        elif isinstance(value, bytes):
            data = bytes(value)
            self.blobs = [data]
            for enc in ("utf-8", encoding):
                try:
                    t = data.decode(enc)
                except UnicodeDecodeError:
                    continue
                if t not in self.texts and "\x00" not in t:
                    self.texts.append(t)
            self.needles = [(data, "bytes")]
        else:
            s = str(value)
            self.texts = [s]
            n = _number(s)
            if n is not None and s in number_texts(n):
                self.number = n
            for enc in ("utf-8", encoding):
                b = s.encode(enc, "surrogatepass")
                if b not in self.blobs:
                    self.blobs.append(b)
            seen = set()
            for enc in ("utf-8", "utf-16-le", "utf-16-be", encoding):
                b = s.encode(enc, "surrogatepass")
                if b and b not in seen:
                    seen.add(b)
                    self.needles.append((b, enc.replace("-", "")))
        self.contains = contains

    # -- the rule ----------------------------------------------------------------------------
    @staticmethod
    def skip_column(_decl_type):
        return False

    def _stored_bytes(self, v):
        if isinstance(v, bytes):
            return bytes(v)
        return v.encode(self.encoding, "surrogatepass")

    def cell_hit(self, v):
        """(shown, type, how, offset) for a matching value, else None; how starts with
        'whole' or 'contained' and says what matched (e.g. 'whole: number as text')."""
        if v is None:
            return None
        typ = value_type(v)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if self.number is not None and v == self.number:
                return truncate(str(v)), typ, WHOLE + ": number", None
            return None
        if isinstance(v, bytes) and not isinstance(v, InvalidText):
            data = bytes(v)
            if data in self.blobs:
                how = "bytes" if isinstance(self.value, bytes) else "text stored as bytes"
                return snippet(data, 0, len(data), "bytes"), typ, WHOLE + ": " + how, 0
            if self.contains:
                for needle, label in self.needles:
                    pos = data.find(needle)
                    if pos >= 0:
                        return (snippet(data, pos, pos + len(needle), label), typ,
                                CONTAINED + ": " + label, pos)
            return None
        # TEXT (or text not valid in the database encoding)
        s = v if isinstance(v, str) else None
        if s is not None and s in self.texts:
            how = "number as text" if isinstance(self.value, (int, float)) else (
                "bytes as text" if isinstance(self.value, bytes) else "text")
            return truncate(s), typ, WHOLE + ": " + how, None
        stored = self._stored_bytes(v)
        if isinstance(self.value, bytes) and stored in self.blobs:
            return truncate(s if s is not None else repr(stored)), typ, \
                WHOLE + ": same bytes as text", None
        if self.contains:
            if s is not None:
                for t in self.texts:
                    pos = s.find(t)
                    if pos >= 0:
                        return truncate(s), typ, CONTAINED + ": text", pos
            for needle, label in self.needles:
                pos = stored.find(needle)
                if pos >= 0:
                    return truncate(s if s is not None else repr(stored)), typ, \
                        CONTAINED + ": " + label, pos
        return None

    def sql_where(self, columns, decl_types=None):
        """WHERE clause selecting every row that can match (Python re-checks each)."""
        whole = []
        if self.number is not None:
            whole.append(self.number)
        whole.extend(self.texts)
        whole.extend(self.blobs)
        needles = []
        if self.contains:
            for needle, _label in self.needles:
                cut = needle[:NEEDLE_CAP]
                if cut not in needles:
                    needles.append(cut)
        parts, params = [], []
        for name in columns:
            q = quote_ident(name)
            alts = ["%s IN (%s)" % (q, ", ".join("?" * len(whole)))]
            params.extend(whole)
            for n in needles:
                alts.append("instr(CAST(%s AS BLOB), ?) > 0" % q)
                params.append(n)
            parts.append("(%s)" % " OR ".join(alts) if len(alts) > 1 else alts[0])
        return " OR ".join(parts), params


class ValueHit(object):
    """One matching cell. source: 'DB', 'WAL' or 'Freelist'; kind: 'whole' or 'contained';
    how: what matched; values: the row (in `columns` order); provenance: where a recovered
    record was found (frame, page, frame_state, cell_offset, confidence, wal_record)."""
    __slots__ = ("source", "table", "column", "locator", "rowid", "columns", "values", "flags",
                 "kind", "how", "offset", "shown", "provenance")

    def __init__(self, hit, columns, source="DB"):
        self.source = hit.get("source", source)
        self.table, self.column = hit["table"], hit["column"]
        self.locator, self.rowid = hit.get("locator"), hit.get("rowid")
        self.columns, self.values = list(columns), list(hit.get("row") or ())
        self.flags = hit.get("flags") or ()
        label = hit.get("encoding") or WHOLE
        self.kind, _sep, self.how = label.partition(": ")
        self.offset, self.shown = hit.get("offset"), hit.get("value")
        self.provenance = dict((k, hit[k]) for k in ("frame", "page", "frame_state",
                                                      "cell_offset", "confidence", "frames",
                                                      "wal_record") if k in hit)

    def key(self):
        return (self.source, self.table, self.column)


def find_everywhere(session, value, contains=False, tables=None, include_views=False,
                    records=None, origin=None, limit=ROW_LIMIT, cancel=None, workers=None):
    """Yield (name, [ValueHit], error) as each table, then each recovered-record source,
    finishes. tables: the tables to search (default: every table, and views with
    include_views). records: {source name: callable() -> iterable of engine.search record
    dicts} (WAL versions, freed-page records). origin: (table, column, locator) of the cell
    the value came from, which is left out. A WAL version identical to the database's row is
    left out too (the database hit already shows it)."""
    matcher = ValueMatcher(value, contains, session.encoding)
    names = list(tables) if tables is not None else \
        session.tables() + (session.views() if include_views else [])
    for name, hits, err in session.search_tables(names, "", "ex", limit, cancel=cancel,
                                                 workers=workers, matcher=matcher):
        if cancel is not None and cancel():
            return
        cols = session.visible_columns(name) if hits else []
        out = []
        for h in hits:
            if origin is not None and (name, h["column"], h["locator"]) == tuple(origin):
                continue
            out.append(ValueHit(h, cols))
        yield name, out, err
    for label, make in (records or {}).items():
        if cancel is not None and cancel():
            return
        out, err = [], None
        try:
            for h in search_records(make(), matcher, limit, cancel):
                if h.get("source") == "WAL" and _same_as_db(session, h):
                    continue
                cols = list(h.get("columns") or [])
                out.append(ValueHit(h, cols or ["col%d" % i for i in range(len(h["row"]))],
                                    h.get("source", label)))
        except Exception as e:          # noqa: BLE001 - one source fails, not the search
            e.__traceback__ = None
            err = e
        yield label, out, err


def _same_as_db(session, hit):
    loc = hit.get("locator")
    if getattr(loc, "kind", None) not in ("rowid", "pk") or session.schema.get(hit["table"]) is None:
        return False
    try:
        row = session.row(hit["table"], loc, scan=False)
    except Exception:           # noqa: BLE001 - cannot compare: keep the WAL hit
        return False
    return row is not None and list(row.values) == list(hit.get("row") or ())
