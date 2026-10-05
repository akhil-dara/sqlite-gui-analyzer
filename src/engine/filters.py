"""Row filters: per-column filter expressions and the global word filter.

Every expression is evaluated two ways that give the same answer: as an SQL fragment (tables
SQLite serves) and as a Python predicate over one value (tables read natively, WAL-only rows,
rows already in memory). Both follow SQLite's rules for the value as stored:

  text          contains the text, any case (ASCII letters only, like SQLite's LIKE)
  !text         does not contain the text (a NULL contains nothing, so it matches)
  a%b_c         LIKE pattern over the whole value: % any run of characters, _ exactly one;
                any case for ASCII letters. Typed when the text holds a %
  !a%b          does not match the LIKE pattern
  /regex/       Python regular expression (re.search), case-sensitive; /regex/i ignores case
  =x  <>x  !=x  equal / not equal
  >x  >=x  <x  <=x
  a~b           inclusive range; both ends numbers, or both text (so PROGRA~1 stays text)
  NULL          the value is NULL;  NOT NULL: it is not
  "x"  'x'      exactly the text x ("" or '' is the empty text); a doubled quote inside
                stands for one quote. Also usable as an operand: ="10" compares as text
  *=text        contains the text (as plain text does, but % _ ~ = etc. are literal);
                !*=text does not contain it
  ^=text        starts with the text, any case (ASCII letters); !^=text: does not
  $=text        ends with the text, any case (ASCII letters); !$=text: does not
                (for *= ^= $= a "quoted" or 'quoted' text stands for the text inside, so
                ^=" a" finds a leading space; NULL never matches, but matches the ! forms)
  EMPTY         NULL or the empty text '' (not a zero-length BLOB x'', not 0, not text that
                only looks empty such as a NUL character); NOT EMPTY: every other value
  IN (a, b)     equal to one of the values; NOT IN (a, b): equal to none of them. Items are
                operands as for = (5, 2.5, "text", 'text', x'00ff', bare text without , ( ) )
                or the keyword NULL. NOT IN matches NULL unless NULL is in the list
  {x} AND {y}   both conditions hold;  {x} OR {y}: at least one does. The braces hold any
                one expression (also another {..} AND/OR {..}); quotes inside are respected

Text that starts with ^= $= *= !^= !$= !*= or { , or starts with IN ( / NOT IN ( , or is
EMPTY / NOT EMPTY (keywords in any case) used to mean 'contains that text'; use *= for that.
Every other text means what it always meant.

An operand that parses as a number (5, -2.5, 1e3) compares as a number; a quoted or other
operand as text; x'00ff' as a BLOB. Values of another storage class compare by SQLite's order
NULL < numbers < text < BLOB, so >5 also matches every text and BLOB value. A comparison never
matches NULL (select those with NULL). Text compares by its bytes in the database encoding
(SQLite's BINARY collation), so it is case-sensitive.

The text a value 'contains' is what SQLite's CAST(value AS TEXT) gives: a REAL is written with
15 significant digits (0.1 + 0.2 is '0.3'), and text stops at a NUL character as it does for
SQLite's LIKE. A regular expression sees the value as Python shows it (repr of a REAL), with a
BLOB decoded as UTF-8 with replacement characters.

condition_text() and combine() write expression text from structured input (for filter
dialogs), so a caller never assembles the syntax by hand.
"""

import math
import re

from .fileformat.record import InvalidText
from .schema import quote_ident


class FilterError(ValueError):
    """An expression that cannot be used; str(error) is written for the user."""


_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_HEX_BLOB_RE = re.compile(r"[xX]'((?:[0-9a-fA-F]{2})*)'\Z")
_NOT_NULL_RE = re.compile(r"NOT\s+NULL\Z", re.IGNORECASE)
_EMPTY_RE = re.compile(r"(NOT\s+)?EMPTY\Z", re.IGNORECASE | re.ASCII)
_IN_RE = re.compile(r"(NOT\s+)?IN\s*\(", re.IGNORECASE | re.ASCII)
_JOIN_RE = re.compile(r"\}\s*(AND|OR)\s*\{", re.IGNORECASE | re.ASCII)
_AFFIX_OPS = (("!^=", "notstarts"), ("!$=", "notends"), ("!*=", "notcontains"),
              ("^=", "starts"), ("$=", "ends"), ("*=", "contains"))
_IN_PARAMS_MAX = 50     # longer IN lists are written as literals (host parameter limits)
_WORD_RE = re.compile(r'"([^"]*)"|(\S+)')
_INT64_MIN, _INT64_MAX = -(1 << 63), (1 << 63) - 1
_ASCII_LOWER = dict((c, c + 32) for c in range(65, 91))
_OPS = (">=", "<=", "<>", "!=", ">", "<", "=")
_RANK = {"num": 1, "text": 2, "blob": 3}


def ascii_lower(s):
    """Lower-case ASCII letters only, as SQLite's LIKE compares them."""
    return s.translate(_ASCII_LOWER)


def like_escape(s):
    """Escape LIKE wildcards for a pattern that uses ESCAPE '\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sql_literal(s):
    """An SQL string literal for s (the only escape in SQL text literals is a doubled quote)."""
    return "'" + s.replace("'", "''") + "'"


def real_text(f):
    """A REAL as SQLite's CAST(x AS TEXT) writes it: 15 significant digits and always a
    decimal point (2.0, 1.0e+20), -0.0 as 0.0, infinities as Inf / -Inf."""
    if f != f:
        return "NaN"            # SQLite stores NaN as NULL; only a damaged record holds one
    if f in (float("inf"), float("-inf")):
        return "Inf" if f > 0 else "-Inf"
    if f == 0:
        return "0.0"
    mant, e, exp = ("%.15g" % f).partition("e")
    if "." not in mant:
        mant += ".0"
    return mant + e + exp


def like_text(value, encoding="utf-8"):
    """The text SQLite's LIKE sees for a value (CAST(value AS TEXT), up to the first NUL
    character), or None for NULL."""
    if value is None:
        return None
    if isinstance(value, bytes):        # a BLOB, or TEXT that is not valid in the encoding
        text = bytes(value).decode(encoding, "replace")
    elif isinstance(value, float):
        return real_text(value)
    elif isinstance(value, str):
        text = value
    else:
        return str(value)
    return text.split("\x00", 1)[0] if "\x00" in text else text


def regex_text(value, encoding="utf-8"):
    """The text a /regex/ filter searches: a TEXT value as stored (invalid bytes replaced), a
    BLOB decoded as UTF-8 with replacement characters, a number as Python writes it."""
    if value is None:
        return None
    if isinstance(value, InvalidText):
        return bytes(value).decode(encoding, "replace")
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else str(value)


def regexp_value(pattern, encoding, storage, value):
    """SQL function sga_regexp(pattern, encoding, typeof(x), x-or-its-text-bytes).

    TEXT arrives as CAST(x AS BLOB): Python's sqlite3 cannot hand a user function TEXT that is
    not valid UTF-8 (the whole statement would fail), so the bytes are decoded here the way
    regex_text() decodes them. Never raises: an error must not look like a failing table.
    """
    try:
        if value is None:
            return 0
        if storage == "text":
            text = bytes(value).decode(encoding, "replace")
        elif isinstance(value, bytes):
            text = value.decode("utf-8", "replace")
        else:
            text = str(value)
        return 1 if re.search(pattern, text) is not None else 0
    except Exception:           # noqa: BLE001 - a user function must not raise into SQLite
        return 0


def _storage_rank(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return 1
    if isinstance(v, (str, InvalidText)):
        return 2
    if isinstance(v, bytes):
        return 3
    return 2


def compare(value, operand, encoding="utf-8"):
    """-1 / 0 / 1 comparing a stored value with an operand ('num'|'text'|'blob', x) the way
    SQLite compares two values without affinity (BINARY collation); None for a NULL value."""
    rv = _storage_rank(value)
    if rv == 0:
        return None
    kind, x = operand
    ro = _RANK[kind]
    if rv != ro:
        return -1 if rv < ro else 1
    if rv == 1:
        if value != value:      # NaN: never stored by SQLite, never equal or ordered
            return None
        a, b = value, x
    elif rv == 3:
        a, b = bytes(value), x
    elif encoding == "utf-8":
        # code point order is UTF-8 byte order; invalid text compares by its bytes
        a, b = (bytes(value), x.encode("utf-8")) if isinstance(value, InvalidText) else (value, x)
    else:
        a = bytes(value) if isinstance(value, InvalidText) else value.encode(encoding, "surrogatepass")
        b = x.encode(encoding)
    return (a > b) - (a < b)


_CMP_OK = {"=": lambda c: c == 0, "<>": lambda c: c != 0, ">": lambda c: c > 0,
           ">=": lambda c: c >= 0, "<": lambda c: c < 0, "<=": lambda c: c <= 0}


def _number(s):
    if not _NUMBER_RE.match(s):
        return None
    if not any(ch in s for ch in ".eE"):
        v = int(s)
        if _INT64_MIN <= v <= _INT64_MAX:
            return v
    return float(s)


def _quoted(s):
    """The text inside "..." or '...' (a doubled quote stands for one), else None."""
    if len(s) < 2 or s[0] not in "'\"" or s[-1] != s[0]:
        return None
    q = s[0]
    inner = s[1:-1]
    if q in inner.replace(q + q, ""):
        return None             # a lone quote inside: not one quoted string
    return inner.replace(q + q, q)


def _operand(s):
    q = _quoted(s)
    if q is not None:
        return ("text", q)
    m = _HEX_BLOB_RE.match(s)
    if m:
        return ("blob", bytes.fromhex(m.group(1)))
    n = _number(s)
    if n is not None:
        return ("num", n)
    return ("text", s)


def _operand_sql(operand):
    return "?" + (" COLLATE BINARY" if operand[0] == "text" else "")


def _like_regex(pattern):
    """Python regex equivalent of an ASCII-case-insensitive LIKE pattern without ESCAPE."""
    out = []
    for ch in ascii_lower(pattern):
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
    return re.compile("".join(out) + r"\Z", re.DOTALL)


class Expr(object):
    """One parsed column filter. sql() and match() give the same answer for every value."""

    __slots__ = ("kind", "text", "term", "pattern", "op", "operand", "lo", "hi", "items",
                 "has_null", "left", "right", "_rx", "_lterm", "_sets")

    def __init__(self, kind, text, term=None, pattern=None, op=None, operand=None, lo=None,
                 hi=None, items=None, has_null=False, left=None, right=None):
        self.kind, self.text = kind, text
        self.term, self.pattern, self.op, self.operand, self.lo, self.hi = \
            term, pattern, op, operand, lo, hi
        self.items = tuple(items or ())         # IN / NOT IN operands (NULL is has_null)
        self.has_null = bool(has_null)
        self.left, self.right = left, right     # AND / OR
        self._lterm = ascii_lower(term) if term is not None else None
        if kind in ("like", "notlike"):
            self._rx = _like_regex(pattern)
        elif kind == "regex":
            self._rx = re.compile(pattern)
        else:
            self._rx = None
        self._sets = _item_sets(self.items) if kind in ("in", "notin") else None

    def key(self):
        return (self.kind, self.text)

    def __eq__(self, other):
        return isinstance(other, Expr) and self.key() == other.key()

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(self.key())

    def __repr__(self):
        return "Expr(%s, %r)" % (self.kind, self.text)

    def describe(self):
        """One line saying what the expression selects."""
        k = self.kind
        if k == "contains":
            return "contains %r (any case)" % self.term
        if k == "notcontains":
            return "does not contain %r (any case)" % self.term
        if k == "starts":
            return "starts with %r (any case)" % self.term
        if k == "notstarts":
            return "does not start with %r (any case)" % self.term
        if k == "ends":
            return "ends with %r (any case)" % self.term
        if k == "notends":
            return "does not end with %r (any case)" % self.term
        if k == "like":
            return "matches the LIKE pattern %r (%% any text, _ one character)" % self.pattern
        if k == "notlike":
            return "does not match the LIKE pattern %r" % self.pattern
        if k == "regex":
            return "matches the regular expression %r" % self.pattern
        if k == "null":
            return "is NULL"
        if k == "notnull":
            return "is not NULL"
        if k == "empty":
            return "is empty (NULL or the empty text)"
        if k == "notempty":
            return "is not empty (not NULL and not the empty text)"
        if k in ("in", "notin"):
            return "%s %s" % ("is one of" if k == "in" else "is none of",
                              _describe_items(self.items, self.has_null))
        if k in ("and", "or"):
            return "%s %s %s" % (_describe_part(self.left), k, _describe_part(self.right))
        if k == "cmp":
            return "%s %s" % (self.op, _describe_operand(self.operand))
        return "between %s and %s (inclusive)" % (_describe_operand(self.lo),
                                                  _describe_operand(self.hi))

    # -- SQL ---------------------------------------------------------------------------------
    def sql(self, column, encoding="utf-8"):
        """(fragment, params) selecting the rows whose `column` matches.

        Comparisons use '+column' (no affinity, so 5 and '5' stay different, as in match())
        with BINARY collation for text; NULL tests use typeof(), because SQLite drops
        'x IS NULL' for a column declared NOT NULL even when a damaged row holds a NULL there.
        """
        q = quote_ident(column)
        k = self.kind
        if k in ("contains", "notcontains", "starts", "notstarts", "ends", "notends"):
            core = like_escape(self.term)
            pat = sql_literal(("%" if k[-6:] != "starts" else "") + core +
                              ("%" if k[-4:] != "ends" else ""))
            if k.startswith("not"):
                return ("(typeof(%s) = 'null' OR CAST(%s AS TEXT) NOT LIKE %s ESCAPE '\\')"
                        % (q, q, pat)), []
            return "CAST(%s AS TEXT) LIKE %s ESCAPE '\\'" % (q, pat), []
        if k == "like":
            return "CAST(%s AS TEXT) LIKE %s" % (q, sql_literal(self.pattern)), []
        if k == "notlike":
            return ("(typeof(%s) = 'null' OR CAST(%s AS TEXT) NOT LIKE %s)"
                    % (q, q, sql_literal(self.pattern))), []
        if k == "regex":
            return ("sga_regexp(?, ?, typeof(%s), CASE WHEN typeof(%s) = 'text' "
                    "THEN CAST(%s AS BLOB) ELSE %s END)" % (q, q, q, q)), [self.pattern, encoding]
        if k == "null":
            return "typeof(%s) = 'null'" % q, []
        if k == "notnull":
            return "typeof(%s) <> 'null'" % q, []
        if k == "empty":
            return "(typeof(%s) = 'null' OR +%s = '' COLLATE BINARY)" % (q, q), []
        if k == "notempty":
            return "(typeof(%s) <> 'null' AND +%s <> '' COLLATE BINARY)" % (q, q), []
        if k in ("in", "notin"):
            return self._in_sql(q)
        if k in ("and", "or"):
            lf, lp = self.left.sql(column, encoding)
            rf, rp = self.right.sql(column, encoding)
            return "(%s %s %s)" % (lf, k.upper(), rf), lp + rp
        if k == "cmp":
            return "+%s %s %s" % (q, self.op, _operand_sql(self.operand)), [self.operand[1]]
        return ("(+%s >= %s AND +%s <= %s)" % (q, _operand_sql(self.lo), q, _operand_sql(self.hi)),
                [self.lo[1], self.hi[1]])

    def _in_sql(self, q):
        neg = self.kind == "notin"
        if not self.items:                      # only NULL in the list
            return "typeof(%s) %s 'null'" % (q, "<>" if neg else "="), []
        if len(self.items) <= _IN_PARAMS_MAX:
            values, params = ", ".join("?" * len(self.items)), [x for _k, x in self.items]
        else:
            # SQLite limits host parameters per statement (999 on old builds): long lists are
            # written as exact literals instead
            values, params = ", ".join(_sql_value(o) for o in self.items), []
        test = "+%s COLLATE BINARY %sIN (%s)" % (q, "NOT " if neg else "", values)
        if not neg:
            return ("(typeof(%s) = 'null' OR %s)" % (q, test) if self.has_null else test), params
        if self.has_null:
            return "(typeof(%s) <> 'null' AND %s)" % (q, test), params
        return "(typeof(%s) = 'null' OR %s)" % (q, test), params

    # -- Python --------------------------------------------------------------------------------
    def match(self, value, encoding="utf-8"):
        """True when a stored value matches, exactly as the SQL fragment decides."""
        k = self.kind
        if k in ("contains", "notcontains"):
            t = like_text(value, encoding)
            hit = t is not None and self._lterm in ascii_lower(t)
            return hit if k == "contains" else not hit
        if k in ("starts", "notstarts"):
            t = like_text(value, encoding)
            hit = t is not None and ascii_lower(t).startswith(self._lterm)
            return hit if k == "starts" else not hit
        if k in ("ends", "notends"):
            t = like_text(value, encoding)
            hit = t is not None and ascii_lower(t).endswith(self._lterm)
            return hit if k == "ends" else not hit
        if k in ("like", "notlike"):
            t = like_text(value, encoding)
            hit = t is not None and self._rx.match(ascii_lower(t)) is not None
            return hit if k == "like" else not hit
        if k == "regex":
            t = regex_text(value, encoding)
            return t is not None and self._rx.search(t) is not None
        if k == "null":
            return value is None
        if k == "notnull":
            return value is not None
        if k in ("empty", "notempty"):
            hit = value is None or (_storage_rank(value) == 2 and len(value) == 0)
            return hit if k == "empty" else not hit
        if k in ("in", "notin"):
            if value is None:
                return self.has_null if k == "in" else not self.has_null
            hit = self._in_hit(value, encoding)
            return hit if k == "in" else not hit
        if k == "and":
            return self.left.match(value, encoding) and self.right.match(value, encoding)
        if k == "or":
            return self.left.match(value, encoding) or self.right.match(value, encoding)
        if k == "cmp":
            c = compare(value, self.operand, encoding)
            return c is not None and _CMP_OK[self.op](c)
        c = compare(value, self.lo, encoding)
        if c is None or c < 0:
            return False
        c = compare(value, self.hi, encoding)
        return c is not None and c <= 0

    def _in_hit(self, value, encoding):
        nums, texts, blobs = self._sets
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, (int, float)):
            return value == value and value in nums     # 5 == 5.0, as in SQLite
        if isinstance(value, InvalidText):
            return False                # never equal to text that is valid in the encoding
        if isinstance(value, str):
            return value in texts       # equal text is equal bytes in any encoding
        if isinstance(value, bytes):
            return bytes(value) in blobs
        return any(compare(value, o, encoding) == 0 for o in self.items)


def _item_sets(items):
    nums, texts, blobs = set(), set(), set()
    for kind, x in items:
        if kind == "num":
            if x == x:
                nums.add(x)
        elif kind == "text":
            texts.add(x)
        else:
            blobs.add(bytes(x))
    return nums, texts, blobs


def _real_sql(f):
    """An SQL expression whose value is exactly the REAL f (no decimal parsing involved:
    SQLite's text-to-real conversion is not correctly rounded on every build)."""
    if f != f:
        return "NULL"
    if f in (float("inf"), float("-inf")):
        return "9e999" if f > 0 else "-9e999"
    if f == int(f) and abs(f) < 2.0 ** 63:
        return "%d" % int(f)            # an integral REAL equals the same INTEGER in SQLite
    m, e = math.frexp(f)
    n, e = int(m * (1 << 53)), e - 53   # f == n * 2**e exactly
    while n % 2 == 0:
        n //= 2
        e += 1
    out = "%d" % n
    op, k = ("*" if e > 0 else "/"), abs(e)
    while k > 0:
        step = min(k, 52)
        out = "(%s %s CAST(%d AS REAL))" % (out, op, 1 << step)
        k -= step
    return out


def _sql_value(operand):
    kind, x = operand
    if kind == "num":
        return "%d" % x if isinstance(x, int) else _real_sql(x)
    if kind == "blob":
        return "X'%s'" % bytes(x).hex()
    return sql_literal(x)


def _brief_operand(operand):
    kind, x = operand
    if kind == "num":
        return repr(x)
    if kind == "blob":
        return "x'%s'" % bytes(x).hex()
    return repr(x)


def _describe_items(items, has_null, shown=10):
    parts = [_brief_operand(o) for o in items[:shown]]
    if has_null and len(parts) < shown:
        parts.append("NULL")
    total = len(items) + bool(has_null)
    more = total - len(parts)
    return "%d value%s: %s%s" % (total, "" if total == 1 else "s", ", ".join(parts),
                                 " (and %d more)" % more if more else "")


def _describe_part(e):
    d = e.describe()
    return "(%s)" % d if e.kind in ("and", "or") else d


def _describe_operand(operand):
    kind, x = operand
    if kind == "num":
        return "the number %r" % (x,)
    if kind == "blob":
        return "the BLOB x'%s'" % x.hex()
    return "the text %r" % x


def check_chars(s):
    """Raise FilterError for text no filter can hold (NUL, unpaired surrogates)."""
    if "\x00" in s:
        raise FilterError("A filter cannot contain a NUL character")
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise FilterError("The filter contains characters that cannot be used (unpaired surrogates)")


def _parse_regex(s):
    if len(s) >= 2 and s.endswith("/"):
        body, icase = s[1:-1], False
    elif len(s) >= 3 and s[-2:] in ("/i", "/I"):
        body, icase = s[1:-2], True
    else:
        raise FilterError("A regular expression must end with / (or /i to ignore case), "
                          "e.g. /^ab+c$/. To find text that starts with /, leave the / out "
                          "or use a LIKE pattern such as %/data%")
    pattern = ("(?i)" if icase else "") + body
    try:
        re.compile(pattern)
    except (re.error, OverflowError, RecursionError, ValueError) as e:
        raise FilterError("Invalid regular expression %s: %s" % (s, e))
    return Expr("regex", s, pattern=pattern)


def _parse_range(s):
    lo, _sep, hi = s.partition("~")
    lo, hi = lo.strip(), hi.strip()
    if not lo or not hi:
        return None
    a, b = _operand(lo), _operand(hi)
    if (a[0] == "num") != (b[0] == "num"):
        return None             # e.g. PROGRA~1: a number and a text end are plain text
    return Expr("range", s, lo=a, hi=b)


def _quote_end(s, i):
    """Index just after the quoted string opening at s[i] (a doubled quote stands for one),
    or -1 when it is not closed."""
    q, j = s[i], i + 1
    while True:
        k = s.find(q, j)
        if k < 0:
            return -1
        if s[k + 1:k + 2] == q:
            j = k + 2
            continue
        return k + 1


_REGEX_END_RE = re.compile(r"/[iI]?\s*\}")


def _quote_starts(s, i):
    """True when the quote at s[i] opens a quoted operand: not one inside a word (it's), but
    the quote of x'00ff'."""
    if i == 0 or not s[i - 1].isalnum():
        return True
    return s[i] == "'" and s[i - 1] in "xX" and (i < 2 or not s[i - 2].isalnum())


def _braces_balanced(s, quotes):
    """True when the { } in s (one expression) pair up. With quotes, quoted operands and
    regular expressions (/../ right after a brace) are skipped: their braces do not count."""
    if quotes and s.lstrip().startswith("/"):
        return True             # one regular expression: its braces are its own
    depth, i, n = 0, 0, len(s)
    while i < n:
        c = s[i]
        if quotes and c in "\"'" and _quote_starts(s, i):
            i = _quote_end(s, i)
            if i < 0:
                return False
            continue
        if c == "{":
            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            if quotes and j < n and s[j] == "/":
                m = _REGEX_END_RE.search(s, j + 1)
                if m is None:
                    return False
                i = m.end()     # the whole {/regex/} block
                continue
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


_JOIN_HELP = ("write two conditions in braces joined by AND or OR, e.g. {abc} AND {!xyz} "
              "(to find text that starts with {, use *={...)")


def _parse_join(s):
    cands = []
    if s.endswith("}"):
        cands = [(m.start(), m.end(), m.group(1).lower()) for m in _JOIN_RE.finditer(s)]
    pick = None
    for quotes in (True, False):
        good = [c for c in cands if _braces_balanced(s[1:c[0]], quotes)
                and _braces_balanced(s[c[1]:-1], quotes)]
        if good:
            pick = good[0]
            break
    if pick is None and len(cands) == 1:
        pick = cands[0]             # e.g. a regular expression holding a single brace
    if pick is None:
        if not (s.endswith("}") and (_braces_balanced(s, True) or _braces_balanced(s, False))):
            raise FilterError("Unbalanced braces: " + _JOIN_HELP)
        if len(cands) > 1:
            raise FilterError("Join two conditions at a time; nest braces for more, "
                              "e.g. {{a} OR {b}} AND {c}")
        raise FilterError("Braces join conditions: " + _JOIN_HELP)
    left, right = s[1:pick[0]], s[pick[1]:-1]
    if not left.strip() or not right.strip():
        raise FilterError("A condition inside { } is empty: " + _JOIN_HELP)
    return Expr(pick[2], s, left=parse_expr(left), right=parse_expr(right))


def _parse_in(s, m):
    neg = m.group(1) is not None
    label = "NOT IN" if neg else "IN"
    example = "e.g. %s (1, 2, \"some text\", NULL)" % label
    items, has_null = [], False
    i, n = m.end(), len(s)
    while True:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            raise FilterError("The %s list has no closing ), %s" % (label, example))
        c = s[i]
        if c in "\"'" or (c in "xX" and s[i + 1:i + 2] == "'"):
            start = i
            i = _quote_end(s, i + (1 if c in "xX" else 0))
            if i < 0:
                raise FilterError("A quote in the %s list is not closed: %s"
                                  % (label, s[start:start + 20]))
            tok, bare = s[start:i], False
        else:
            start = i
            while i < n and s[i] not in ",()":
                i += 1
            if i < n and s[i] == "(":
                raise FilterError("Put %s values that contain ( or ) in quotes, %s"
                                  % (label, example))
            tok, bare = s[start:i].strip(), True
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            raise FilterError("The %s list has no closing ), %s" % (label, example))
        if s[i] not in ",)":
            raise FilterError("Separate the %s values with commas, %s" % (label, example))
        if not tok:
            if s[i] == ")" and not items and not has_null:
                raise FilterError("%s needs at least one value, %s" % (label, example))
            raise FilterError("A value in the %s list is empty (two commas in a row?), %s"
                              % (label, example))
        if bare and tok.upper() == "NULL":
            has_null = True
        else:
            items.append(_operand(tok))
        i += 1
        if s[i - 1] == ")":
            break
    if s[i:].strip():
        raise FilterError("Nothing may follow the closing ) of the %s list" % label)
    return Expr("notin" if neg else "in", s, items=items, has_null=has_null)


def _parse_affix(s, op, kind):
    rest = s[len(op):].strip()
    if not rest:
        what = {"starts": "starts with", "notstarts": "does not start with",
                "ends": "ends with", "notends": "does not end with",
                "contains": "contains", "notcontains": "does not contain"}[kind]
        raise FilterError("%s needs text after it, e.g. %sabc (%s abc)" % (op, op, what))
    q = _quoted(rest)
    return Expr(kind, s, term=q if q is not None else rest)


def parse_expr(text):
    """Parse one column filter; None for empty text. Raises FilterError for unusable input."""
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    check_chars(s)
    if s.upper() == "NULL":
        return Expr("null", s)
    if _NOT_NULL_RE.match(s):
        return Expr("notnull", s)
    m = _EMPTY_RE.match(s)
    if m:
        return Expr("notempty" if m.group(1) else "empty", s)
    if s[0] == "{":
        return _parse_join(s)
    m = _IN_RE.match(s)
    if m:
        return _parse_in(s, m)
    if s[0] == "/":
        return _parse_regex(s)
    for op, kind in _AFFIX_OPS:
        if s.startswith(op):
            return _parse_affix(s, op, kind)
    for op in _OPS:
        if s.startswith(op):
            rest = s[len(op):].strip()
            if not rest:
                raise FilterError("%s needs a value after it, e.g. %s5 or %s\"text\""
                                  % (op, op, op))
            return Expr("cmp", s, op="<>" if op == "!=" else op, operand=_operand(rest))
    if s[0] == "!":
        rest = s[1:].strip()
        if not rest:
            raise FilterError("! needs text after it: !abc keeps rows that do not contain abc")
        if "%" in rest:
            return Expr("notlike", s, pattern=rest)
        return Expr("notcontains", s, term=rest)
    q = _quoted(s)
    if q is not None:
        return Expr("cmp", s, op="=", operand=("text", q))
    if "%" in s:
        return Expr("like", s, pattern=s)
    if "~" in s:
        rng = _parse_range(s)
        if rng is not None:
            return rng
    return Expr("contains", s, term=s)


def check_expr(text):
    """None when the text is a usable filter (or empty), else the error message."""
    try:
        parse_expr(text)
    except FilterError as e:
        return str(e)
    return None


def parse_words(text):
    """Words of the global filter: whitespace separated, "double quotes" keep a phrase."""
    if not text:
        return []
    check_chars(text)
    out = []
    for m in _WORD_RE.finditer(text):
        w = m.group(1) if m.group(1) is not None else m.group(2)
        if w:
            out.append(w)
    return out


def value_expr(value):
    """A column filter selecting exactly this value (for 'Filter by this value'), or None
    when the value cannot be written as one (invalid text, a large BLOB)."""
    if value is None:
        return "NULL"
    if isinstance(value, InvalidText):
        return None
    if isinstance(value, bytes):
        return "=x'%s'" % value.hex() if len(value) <= 256 else None
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return "=%d" % value
    if isinstance(value, float):
        return "=" + repr(value) if value == value and abs(value) != float("inf") else None
    s = str(value)
    if "\x00" in s:
        return None
    if s and s == s.strip() and _operand(s) == ("text", s):
        return "=" + s
    return '="%s"' % s.replace('"', '""')


# -- writing expressions from structured input ------------------------------------------------

_BARE_BAN = set("\"'{}(),")
_TERM_BAN = set("\"'{}")


def _quote_text(s):
    return '"%s"' % s.replace('"', '""')


def _plain_chars(s, banned):
    return all(c not in banned and (c == " " or not c.isspace()) for c in s)


def _operand_of(value):
    """The operand (kind, x) a Python value compares as."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, InvalidText):
        raise ValueError("Text that is not valid in the database encoding cannot be written "
                         "as a filter value")
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise ValueError("%d does not fit in a 64-bit integer" % value)
        return ("num", value)
    if isinstance(value, float):
        if value != value:
            raise ValueError("NaN cannot be a filter value")
        return ("num", value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ("blob", bytes(value))
    if isinstance(value, str):
        check_chars(value)
        return ("text", value)
    raise TypeError("Unsupported filter value %r" % (value,))


def _value_text(value):
    """Operand text that _operand() reads back as exactly this value (None: NULL keyword)."""
    if value is None:
        return "NULL"
    kind, x = _operand_of(value)
    if kind == "num":
        if isinstance(x, int):
            return "%d" % x
        if x in (float("inf"), float("-inf")):
            return "1e999" if x > 0 else "-1e999"
        return repr(x)          # always holds '.', 'e', 'inf' so it reads back as a REAL
    if kind == "blob":
        return "x'%s'" % x.hex()
    if (x and x == x.strip() and x[0] not in "=<>!" and x.upper() != "NULL"
            and _plain_chars(x, _BARE_BAN) and _operand(x) == ("text", x)):
        return x
    return _quote_text(x)


def _same_operand(a, b):
    return a[0] == b[0] and type(a[1]) is type(b[1]) and a[1] == b[1]


def _term_text(op, t, kind):
    """op + t written so that it parses as `kind` with exactly the term t."""
    if not isinstance(t, str):
        raise TypeError("The text to look for must be a str")
    check_chars(t)
    if t and _plain_chars(t, _TERM_BAN):
        for text in ((t if kind == "contains" else "!" + t) if op in ("*=", "!*=") else None,
                     op + t):
            if text is None:
                continue
            try:
                e = parse_expr(text)
            except FilterError:
                continue
            if e is not None and e.kind == kind and e.term == t and e.text == text:
                return text
    return op + _quote_text(t)


_TERM_KINDS = {"contains": ("*=", "contains"), "not_contains": ("!*=", "notcontains"),
               "starts": ("^=", "starts"), "not_starts": ("!^=", "notstarts"),
               "ends": ("$=", "ends"), "not_ends": ("!$=", "notends")}
_CMP_KINDS = {"equals": "=", "not_equals": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}


def condition_text(kind, *args):
    """Expression text for one condition from structured input; parse_expr() reads it back
    as exactly that condition. Values are Python objects (int, float, str, bytes, None) and
    keep their storage class: condition_text("equals", "5") is '="5"' (the text 5).

      contains / not_contains / starts / not_starts / ends / not_ends   (text)
      equals / not_equals (value; None gives NULL / NOT NULL)
      gt / ge / lt / le (value)     between (low, high), inclusive
      in / not_in (iterable of values, None for NULL; at least one)
      regex (pattern, icase=False)  empty / not_empty / null / not_null  (no arguments)

    Raises ValueError (FilterError for text no filter can hold) for values that cannot be
    written, TypeError for unsupported types."""
    def need(count):
        if len(args) != count:
            raise TypeError("%s takes %d argument%s" % (kind, count, "" if count == 1 else "s"))

    if kind in _TERM_KINDS:
        need(1)
        op, k = _TERM_KINDS[kind]
        return _term_text(op, args[0], k)
    if kind in ("empty", "not_empty", "null", "not_null"):
        need(0)
        return {"empty": "EMPTY", "not_empty": "NOT EMPTY", "null": "NULL",
                "not_null": "NOT NULL"}[kind]
    if kind in _CMP_KINDS:
        need(1)
        v = args[0]
        if v is None:
            if kind in ("equals", "not_equals"):
                return "NULL" if kind == "equals" else "NOT NULL"
            raise ValueError("NULL cannot be compared with %s" % _CMP_KINDS[kind])
        return _CMP_KINDS[kind] + _value_text(v)
    if kind == "between":
        need(2)
        lo, hi = args
        if lo is None or hi is None:
            raise ValueError("between needs two values that are not NULL")
        text = "%s~%s" % (_value_text(lo), _value_text(hi))
        try:
            e = parse_expr(text)
        except FilterError:
            e = None
        if (e is not None and e.kind == "range" and _same_operand(e.lo, _operand_of(lo))
                and _same_operand(e.hi, _operand_of(hi))):
            return text
        return combine(">=" + _value_text(lo), "AND", "<=" + _value_text(hi))
    if kind in ("in", "not_in"):
        need(1)
        values = list(args[0])
        if not values:
            raise ValueError("%s needs at least one value" % kind)
        return "%s (%s)" % ("IN" if kind == "in" else "NOT IN",
                            ", ".join(_value_text(v) for v in values))
    if kind == "regex":
        if len(args) not in (1, 2):
            raise TypeError("regex takes a pattern and an optional icase flag")
        pattern, icase = args[0], (args[1] if len(args) == 2 else False)
        check_chars(pattern)
        text = "/%s/%s" % (pattern, "i" if icase else "")
        parse_expr(text)            # FilterError for an invalid expression
        return text
    raise ValueError("Unknown condition kind %r" % (kind,))


def combine(text1, op, text2):
    """'{text1} AND {text2}' (or OR): both / either condition. Raises FilterError when the
    two texts cannot be joined so that they read back as written."""
    op = str(op).upper()
    if op not in ("AND", "OR"):
        raise ValueError("combine() joins with AND or OR, not %r" % (op,))
    a, b = (text1 or "").strip(), (text2 or "").strip()
    if not a or not b:
        raise ValueError("combine() needs two conditions")
    text = "{%s} %s {%s}" % (a, op, b)
    e = parse_expr(text)
    if e.kind != op.lower() or e.left.text != a or e.right.text != b:
        raise FilterError("These two conditions cannot be joined: %s / %s" % (a, b))
    return text


def balanced(op, parts):
    """Join SQL conditions with AND / OR as a balanced tree: a flat chain of hundreds of terms
    (a filter over every column of a wide table) exceeds SQLite's expression depth limit."""
    parts = list(parts)
    if not parts:
        return ""
    while len(parts) > 1:
        parts = ["(%s %s %s)" % (parts[i], op, parts[i + 1]) if i + 1 < len(parts) else parts[i]
                 for i in range(0, len(parts), 2)]
    return parts[0]


class RowFilter(object):
    """Filter on whole rows: expressions per column (AND) and terms that must each occur in
    at least one column. Columns the rows do not have are ignored."""

    def __init__(self, col_exprs=None, anywhere=None):
        self.col_exprs = []                 # [(column, Expr)]
        for col, e in (col_exprs or []):
            if isinstance(e, str):
                e = parse_expr(e)
            if e is not None:
                self.col_exprs.append((col, e))
        self.anywhere = [t for t in (anywhere or []) if t]
        self._lower = [ascii_lower(t) for t in self.anywhere]
        self._cols_ref = None
        self._positions = None

    def __bool__(self):
        return bool(self.col_exprs or self.anywhere)

    __nonzero__ = __bool__

    def key(self):
        return (tuple((c, e.key()) for c, e in self.col_exprs), tuple(self.anywhere))

    def _pos(self, columns):
        if columns is not self._cols_ref:
            index = dict((c, i) for i, c in reversed(list(enumerate(columns))))
            self._positions = [(index[c], e) for c, e in self.col_exprs if c in index]
            self._cols_ref = columns
        return self._positions

    def matches(self, columns, row, encoding="utf-8"):
        for i, e in self._pos(columns):
            if not e.match(row[i] if i < len(row) else None, encoding):
                return False
        if self._lower:
            texts = []
            for v in row[:len(columns)]:
                t = like_text(v, encoding)
                if t is not None:
                    texts.append(ascii_lower(t))
            for term in self._lower:
                if not any(term in t for t in texts):
                    return False
        return True

    def where_sql(self, columns, encoding="utf-8"):
        """(where, params) for these (visible) columns; ('', []) when nothing applies."""
        known = set(columns)
        parts, params = [], []
        for col, e in self.col_exprs:
            if col in known:
                frag, p = e.sql(col, encoding)
                parts.append(frag)
                params.extend(p)
        for term in self.anywhere:
            lit = sql_literal("%" + like_escape(term) + "%")
            parts.append(balanced("OR", ["CAST(%s AS TEXT) LIKE %s ESCAPE '\\'"
                                         % (quote_ident(c), lit) for c in columns]) or "0")
        return balanced("AND", parts), params
