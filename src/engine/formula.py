"""Formulas typed by the user, worked out on a row's values: `x / 100`, `round(x / 100, 2)`,
`amount * 1.18 + fee`, `upper(left(name, 3))`, `x if x > 0 else -x`, `coalesce(nick, name)`,
`date(x, "ms")`, `json(x, "to.name")`. `x` is the value the formula is made from; any other
column of the row is read by its name (a name that is no identifier: `col("Full name")`).

Nothing is executed: the formula is parsed (Python expression syntax) and walked with a
closed list of node kinds, operators and functions; names resolve to `x`, the row's columns
and the functions below, nothing else (no attributes, no imports, no calls to anything not
listed, exponents capped). A value the formula does not fit raises NotApplicable with a
plain reason, so the cell stays empty and says why (engine.calc).
"""

import ast
import math
import operator
import re
import warnings

MAX_LEN = 2000              # characters of a formula
MAX_POWER = 64              # |exponent| at most (no 10 ** 10 ** 10)
MAX_REPEAT = 10000          # text * n at most


class FormulaError(ValueError):
    """The formula cannot be read (said with the reason and the place)."""


class NotApplicable(Exception):
    """The formula does not fit this value / row (the cell is left empty, with the reason)."""


def _num(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if v is None:
        raise NotApplicable("empty")
    if isinstance(v, (bytes, bytearray)):
        raise NotApplicable("a BLOB is not a number")
    s = str(v).strip().replace(",", "")
    try:
        return int(s) if re.fullmatch(r"[-+]?\d+", s) else float(s)
    except ValueError:
        raise NotApplicable("'%s' is not a number" % s[:20])


def _text(v):
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return str(v)


def _tidy(x):
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
        return int(x)
    return x


def _int(v, what="a count"):
    n = _num(v)
    if isinstance(n, float):
        if not n.is_integer():
            raise NotApplicable("%s must be a whole number" % what)
        n = int(n)
    return n


# -- the functions a formula may call ---------------------------------------------------------
def _f_left(s, n):
    return _text(s)[:max(0, _int(n))]


def _f_right(s, n):
    n = max(0, _int(n))
    return _text(s)[-n:] if n else ""


def _f_mid(s, start, n):
    start = max(1, _int(start, "the start"))
    return _text(s)[start - 1:start - 1 + max(0, _int(n))]


def _f_split(s, sep, i):
    parts = _text(s).split(_text(sep) or " ")
    i = _int(i, "the part number")
    if i < 1 or i > len(parts):
        raise NotApplicable("no part %d" % i)
    return parts[i - 1]


def _f_coalesce(*a):
    for v in a:
        if v not in (None, ""):
            return v
    return None


def _f_iif(c, a, b):
    return a if c else b


def _f_thousands(v, d=2):
    return format(float(_num(v)), ",.%df" % max(0, _int(d, "decimals")))


def _f_round(v, d=0):
    r = round(float(_num(v)), _int(d, "decimals"))
    return _tidy(r) if _int(d, "decimals") <= 0 else r


def _f_json(v, path):
    """A field inside a decoded value: JSON, a plist (binary or XML), NSKeyedArchiver,
    protobuf... - whatever the tool's decoder reads out of the stored bytes."""
    from .decode import fields as fl
    got = fl.read(v, [_text(path)])[0]
    if got is None:
        raise NotApplicable("no field %s" % _text(path))
    return got


def _f_decoded(v):
    """The whole decoded value as JSON text (a plist, an archive, protobuf... as the row
    detail shows it); a plain text or number stays as it is."""
    import json
    from .decode import fields as fl
    tree = fl.plain(v)
    if tree is None:
        if isinstance(v, (bytes, bytearray)):
            raise NotApplicable("nothing decodes in these bytes")
        return v
    return json.dumps(tree, ensure_ascii=False, default=str)


def _f_kind(v):
    """What a value is: NULL, INTEGER, REAL, TEXT, or for a BLOB what its bytes hold
    ('JPEG image 1200×800', 'binary plist', 'protobuf'...)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool) or isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "REAL"
    if isinstance(v, (bytes, bytearray)):
        from .decode import summary
        return summary(bytes(v))
    return "TEXT"


_SIZE_UNITS = (("B", 1), ("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3),
               ("TB", 1024 ** 4))


def _f_size(v, unit=None):
    """A byte count as '1.5 MB' (unit None: the fitting one), or the number in a unit
    ('KB', 'MB', 'GB', 'TB', 'B'; 'kb' / 'mb' read the same); of a BLOB: its length."""
    n = len(v) if isinstance(v, (bytes, bytearray)) else _num(v)
    if unit is not None:
        u = _text(unit).strip().upper().replace("I", "")
        for name, div in _SIZE_UNITS:
            if u == name:
                return _tidy(round(n / float(div), 3))
        raise NotApplicable("unknown unit '%s' (B, KB, MB, GB, TB)" % _text(unit))
    for name, div in reversed(_SIZE_UNITS):
        if abs(n) >= div or name == "B":
            if name == "B":
                return "%d B" % n
            return "%.1f %s" % (n / float(div), name)
    return "%d B" % n


def _f_duration(v, unit="s"):
    """Seconds (or ms / us / ns / min / h / d) as '1d 02:03:04' / '02:03:04' / '03:04.5'."""
    u = _text(unit).strip().lower()
    scale = {"s": 1.0, "sec": 1.0, "ms": 0.001, "us": 1e-6, "ns": 1e-9, "min": 60.0,
             "m": 60.0, "h": 3600.0, "d": 86400.0}.get(u)
    if scale is None:
        raise NotApplicable("unknown unit '%s' (s, ms, us, ns, min, h, d)" % u)
    secs = _num(v) * scale
    neg = secs < 0
    secs = abs(secs)
    days, rest = divmod(secs, 86400)
    hours, rest = divmod(rest, 3600)
    mins, sec = divmod(rest, 60)
    frac = sec - int(sec)
    sec_text = "%02d" % int(sec) + (".%d" % round(frac * 10) if frac >= 0.05 and
                                     days == 0 and hours == 0 else "")
    if days:
        out = "%dd %02d:%02d:%s" % (days, hours, mins, sec_text)
    else:
        out = "%02d:%02d:%s" % (hours, mins, sec_text)
    return "-" + out if neg else out


def _date_kinds(kind):
    from . import timeline as tl
    k = _text(kind).strip().lower()
    names = {"s": "unix_s", "ms": "unix_ms", "us": "unix_us", "ns": "unix_ns",
             "cocoa": "cocoa_s", "webkit": "webkit_us", "chrome": "webkit_us",
             "filetime": "filetime", "hfs": "hfs_s", "dotnet": "dotnet_ticks",
             "ole": "ole_days", "gps": "gps_s", "iso": tl.ISO, "text": tl.ISO}
    if k in names:
        return [names[k]]
    if k in tl.KINDS:
        return [k]
    if k in ("", "auto"):
        return list(tl.KINDS)
    raise NotApplicable("unknown date kind '%s'" % k)


def _to_dt(v, kind="auto"):
    """A naive UTC datetime of the value read as kind (auto: the first kind that fits)."""
    from . import timeline as tl
    if v is None:
        raise NotApplicable("empty")
    for kd in _date_kinds(kind):
        try:
            dt = tl.to_utc(v, kd)
        except Exception:           # noqa: BLE001 - not that kind
            dt = None
        if dt is not None:
            return dt
    raise NotApplicable("not a date")


def _shifted(dt, zone):
    from datetime import timedelta
    from . import timeline as tl
    if zone is None:
        return dt
    minutes = tl.parse_offset(_text(zone))
    if minutes is None:
        raise NotApplicable("unknown zone '%s' (UTC+05:30, -0800, Z)" % _text(zone))
    return dt + timedelta(minutes=minutes)


def _f_date(v, kind="auto", zone=None):
    """The value as a date text (UTC, or shifted to a zone offset: 'UTC+05:30')."""
    from . import timeline as tl
    return tl.format_dt(_shifted(_to_dt(v, kind), zone), "iso", "")


def _f_dateformat(v, kind, pattern, zone=None):
    """The date written with a strftime pattern ('%d %b %Y %H:%M')."""
    dt = _shifted(_to_dt(v, kind), zone)
    try:
        return dt.strftime(_text(pattern))
    except (ValueError, TypeError) as e:
        raise NotApplicable("pattern: %s" % e)


def _f_datepart(part):
    def f(v, kind="auto", zone=None):
        dt = _shifted(_to_dt(v, kind), zone)
        if part == "weekday":
            return dt.strftime("%A")
        if part == "week":
            return dt.isocalendar()[1]
        if part == "quarter":
            return (dt.month - 1) // 3 + 1
        if part == "day_of_year":
            return dt.timetuple().tm_yday
        return getattr(dt, part)
    return f


def _f_datediff(a, b, kind="auto", unit="d"):
    """b - a in days (or 'h', 'min', 's', 'ms'); both read as kind."""
    da, db = _to_dt(a, kind), _to_dt(b, kind)
    secs = (db - da).total_seconds()
    u = _text(unit).strip().lower()
    div = {"d": 86400.0, "h": 3600.0, "min": 60.0, "m": 60.0, "s": 1.0, "ms": 0.001}.get(u)
    if div is None:
        raise NotApplicable("unknown unit '%s' (d, h, min, s, ms)" % u)
    return _tidy(round(secs / div, 3))


def _f_unix(v, kind="auto", to="s"):
    """The date as a Unix time in seconds (or 'ms', 'us', 'ns'), or any other kind's raw
    number ('webkit', 'cocoa'...): a conversion between the ways dates are stored."""
    from . import timeline as tl
    dt = _to_dt(v, kind)
    out = _date_kinds(to)
    if len(out) != 1:
        raise NotApplicable("say which kind to write: s, ms, us, ns, webkit, cocoa...")
    return _tidy(tl.from_utc(dt, out[0]))


def _f_hash(name):
    import hashlib

    def f(v):
        if v is None:
            raise NotApplicable("empty")
        data = bytes(v) if isinstance(v, (bytes, bytearray)) else _text(v).encode("utf-8")
        return hashlib.new(name, data).hexdigest()
    return f


def _f_base64(v):
    import base64
    data = bytes(v) if isinstance(v, (bytes, bytearray)) else _text(v).encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def _f_unbase64(v):
    import base64
    import binascii
    try:
        data = base64.b64decode(_text(v).strip() + "==", validate=False)
    except (binascii.Error, ValueError) as e:
        raise NotApplicable("not base64: %s" % e)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _f_fromhex(v):
    try:
        data = bytes.fromhex(re.sub(r"[\s:,-]", "", _text(v)))
    except ValueError as e:
        raise NotApplicable("not hex: %s" % e)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _f_utf8(v):
    if not isinstance(v, (bytes, bytearray)):
        return _text(v)
    return bytes(v).decode("utf-8", "replace")


def _f_urldecode(v):
    from urllib.parse import unquote_plus
    return unquote_plus(_text(v))


def _f_regex(v, pattern, group=0):
    try:
        m = re.search(_text(pattern), _text(v))
    except re.error as e:
        raise NotApplicable("regex: %s" % e)
    if m is None:
        raise NotApplicable("no match")
    g = group if isinstance(group, int) and not isinstance(group, bool) else \
        (_int(group, "the group") if _is_num(group) else _text(group))
    try:
        return m.group(g)
    except (IndexError, error_types()):
        raise NotApplicable("no group %s" % g)


def error_types():
    return re.error


def _f_matches(v, pattern):
    try:
        return re.search(_text(pattern), _text(v)) is not None
    except re.error as e:
        raise NotApplicable("regex: %s" % e)


def _f_pad(side):
    def f(v, n, ch="0"):
        s = _text(v)
        n = _int(n, "the width")
        c = _text(ch) or " "
        return s.rjust(n, c[0]) if side == "l" else s.ljust(n, c[0])
    return f


def _f_wordcount(v):
    return len(_text(v).split())


def _f_percent(v, of):
    of = _num(of)
    if of == 0:
        raise NotApplicable("division by 0")
    return _tidy(round(_num(v) / float(of) * 100, 2))


def _f_hex(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    if isinstance(v, bool) or not isinstance(v, int):
        v = _int(v, "hex")
    return format(v, "x")


def _f_len(v):
    return len(v) if isinstance(v, (bytes, bytearray)) else len(_text(v))


def _f_contains(s, t):
    return _text(t) in _text(s)


FUNCS = {
    "size": _f_size, "duration": _f_duration, "dateformat": _f_dateformat,
    "year": _f_datepart("year"), "month": _f_datepart("month"), "day": _f_datepart("day"),
    "hour": _f_datepart("hour"), "minute": _f_datepart("minute"),
    "second": _f_datepart("second"), "weekday": _f_datepart("weekday"),
    "week": _f_datepart("week"), "quarter": _f_datepart("quarter"),
    "dayofyear": _f_datepart("day_of_year"), "datediff": _f_datediff, "unix": _f_unix,
    "md5": _f_hash("md5"), "sha1": _f_hash("sha1"), "sha256": _f_hash("sha256"),
    "base64": _f_base64, "unbase64": _f_unbase64, "fromhex": _f_fromhex, "utf8": _f_utf8,
    "urldecode": _f_urldecode, "regex": _f_regex, "matches": _f_matches,
    "lpad": _f_pad("l"), "rpad": _f_pad("r"), "wordcount": _f_wordcount,
    "percent": _f_percent, "kind": _f_kind, "field": _f_json, "decoded": _f_decoded,
    "isnull": lambda v: v is None, "isempty": lambda v: v in (None, ""),
    "sign": lambda v: (_num(v) > 0) - (_num(v) < 0), "trunc": lambda v: int(_num(v)),
    "abs": lambda v: abs(_num(v)), "round": _f_round, "int": lambda v: int(_num(v)),
    "float": lambda v: float(_num(v)), "num": _num, "str": _text, "text": _text,
    "len": _f_len, "min": lambda *a: min(_num(v) for v in a),
    "max": lambda *a: max(_num(v) for v in a),
    "upper": lambda s: _text(s).upper(), "lower": lambda s: _text(s).lower(),
    "title": lambda s: _text(s).title(), "strip": lambda s: _text(s).strip(),
    "trim": lambda s: _text(s).strip(), "left": _f_left, "right": _f_right, "mid": _f_mid,
    "replace": lambda s, a, b: _text(s).replace(_text(a), _text(b)), "split": _f_split,
    "contains": _f_contains, "startswith": lambda s, t: _text(s).startswith(_text(t)),
    "endswith": lambda s, t: _text(s).endswith(_text(t)),
    "digits": lambda s: re.sub(r"\D", "", _text(s)), "coalesce": _f_coalesce,
    "ifnull": _f_coalesce, "iif": _f_iif, "thousands": _f_thousands, "hex": _f_hex,
    "date": _f_date, "json": _f_json, "sqrt": lambda v: math.sqrt(_num(v)),
    "floor": lambda v: math.floor(_num(v)), "ceil": lambda v: math.ceil(_num(v)),
}

# what the help lists (name, what it does), in groups
FUNCTION_HELP = (
    ("abs, round(x, d), int, trunc, sign, float, num, sqrt, floor, ceil",
     "numbers (a numeric text is read as a number)"),
    ("min(a, b, …), max(a, b, …), percent(x, of)", "the smallest / largest; x as % of"),
    ("thousands(x, d)", "1,234,567.89 with d decimals"),
    ("size(x), size(x, \"MB\")", "a byte count as '1.5 MB', or the number in a unit (a BLOB: its length)"),
    ("duration(x, \"ms\")", "seconds, ms, us, ns, min, h or d as '1d 02:03:04'"),
    ("text, str, len, upper, lower, title, trim, wordcount", "text"),
    ("left(s, n), right(s, n), mid(s, start, n)", "parts of a text (start counts from 1)"),
    ("replace(s, a, b), split(s, sep, n), digits(s), lpad(s, n, ch), rpad(s, n, ch)",
     "text edits; split takes part n"),
    ("contains(s, t), startswith(s, t), endswith(s, t), matches(s, regex)", "true / false"),
    ("regex(s, pattern, group)", "what a regular expression finds (group 0: the whole match)"),
    ("coalesce(a, b, …), iif(test, a, b), isnull(x), isempty(x)", "the first non-empty; a choice"),
    ("x if test else y, and, or, not, ==, !=, <, <=, >, >=, in", "conditions"),
    ("date(x, \"ms\"), date(x, \"ms\", \"UTC+05:30\")",
     "a timestamp as a date (s, ms, us, ns, cocoa, webkit, filetime…, auto), UTC or a zone"),
    ("dateformat(x, \"ms\", \"%d %b %Y %H:%M\")", "the date written your way (strftime)"),
    ("year(x, kind), month, day, hour, minute, second, weekday, week, quarter, dayofyear",
     "parts of a date"),
    ("datediff(a, b, kind, \"d\")", "b − a in days, h, min, s or ms"),
    ("unix(x, \"webkit\", \"ms\")", "a date stored one way as another (seconds, ms, us, ns, webkit, cocoa…)"),
    ("field(x, \"to.name\"), json(x, \"to.name\")",
     "a field inside a decoded value: JSON, plist, NSKeyedArchiver, protobuf…"),
    ("decoded(x)", "the whole decoded value as JSON text"),
    ("kind(x)", "NULL / INTEGER / REAL / TEXT, or what a BLOB holds (JPEG image, binary plist…)"),
    ("md5(x), sha1(x), sha256(x)", "the hash of a text (UTF-8) or BLOB"),
    ("hex(x), fromhex(s), base64(x), unbase64(s), utf8(x), urldecode(s)", "encodings"),
    ("col(\"Full name\")", "a column whose name is no plain word"),
)

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b}


class Formula(object):
    """A parsed formula: names (the row's columns it reads) and eval(x, look)."""

    def __init__(self, text):
        text = (text or "").strip()
        if not text:
            raise FormulaError("the formula is empty")
        if len(text) > MAX_LEN:
            raise FormulaError("the formula is longer than %d characters" % MAX_LEN)
        try:
            # a regular expression typed as "\\d+" keeps its backslash: Python's warning
            # about the unknown escape is not shown
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(text, mode="eval")
        except SyntaxError as e:
            raise FormulaError("cannot read it at character %s: %s" % (e.offset, e.msg))
        self.text = text
        self.names = []
        self._check(tree.body)
        self._tree = tree.body

    # -- the closed list of what a formula may contain ---------------------------------------
    def _check(self, n):
        if isinstance(n, ast.Constant):
            if not isinstance(n.value, (int, float, str, bool, type(None))):
                raise FormulaError("a value of that kind is not allowed")
            return
        if isinstance(n, ast.Name):
            if n.id != "x" and n.id not in FUNCS and n.id not in self.names:
                self.names.append(n.id)
            return
        if isinstance(n, ast.BinOp):
            if type(n.op) not in _BIN:
                raise FormulaError("the operator is not allowed")
            self._check(n.left)
            self._check(n.right)
            return
        if isinstance(n, ast.UnaryOp):
            if not isinstance(n.op, (ast.USub, ast.UAdd, ast.Not)):
                raise FormulaError("the operator is not allowed")
            self._check(n.operand)
            return
        if isinstance(n, ast.BoolOp):
            for v in n.values:
                self._check(v)
            return
        if isinstance(n, ast.Compare):
            if any(type(o) not in _CMP for o in n.ops):
                raise FormulaError("the comparison is not allowed")
            self._check(n.left)
            for c in n.comparators:
                self._check(c)
            return
        if isinstance(n, ast.IfExp):
            self._check(n.test)
            self._check(n.body)
            self._check(n.orelse)
            return
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.keywords:
                raise FormulaError("only the listed functions can be called, by name")
            if n.func.id == "col":
                if len(n.args) != 1 or not isinstance(n.args[0], ast.Constant) or \
                        not isinstance(n.args[0].value, str):
                    raise FormulaError('col() takes the column\'s name in quotes: col("name")')
                if n.args[0].value not in self.names:
                    self.names.append(n.args[0].value)
                return
            if n.func.id not in FUNCS:
                raise FormulaError("unknown function %s()" % n.func.id)
            for a in n.args:
                self._check(a)
            return
        if isinstance(n, ast.Subscript):
            self._check(n.value)
            sl = n.slice
            if isinstance(sl, ast.Slice):
                for part in (sl.lower, sl.upper, sl.step):
                    if part is not None:
                        self._check(part)
                return
            if hasattr(ast, "Index") and isinstance(sl, ast.Index):    # Python 3.8
                sl = sl.value
            self._check(sl)
            return
        raise FormulaError("%s is not allowed in a formula" % type(n).__name__.lower())

    # -- working it out --------------------------------------------------------------------
    def eval(self, x, look=None):
        """The formula's value for x and the row (look(name) -> the column's value)."""
        return self._ev(self._tree, x, look)

    def _ev(self, n, x, look):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id == "x":
                return x
            if n.id in FUNCS and n.id not in self.names:
                raise NotApplicable("%s is a function: call it, %s(...)" % (n.id, n.id))
            return self._col(n.id, look)
        if isinstance(n, ast.BinOp):
            a, b = self._ev(n.left, x, look), self._ev(n.right, x, look)
            return self._bin(n.op, a, b)
        if isinstance(n, ast.UnaryOp):
            v = self._ev(n.operand, x, look)
            if isinstance(n.op, ast.Not):
                return not v
            v = _num(v)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                v = True
                for part in n.values:
                    v = self._ev(part, x, look)
                    if not v:
                        return v
                return v
            v = False
            for part in n.values:
                v = self._ev(part, x, look)
                if v:
                    return v
            return v
        if isinstance(n, ast.Compare):
            left = self._ev(n.left, x, look)
            for op, comp in zip(n.ops, n.comparators):
                right = self._ev(comp, x, look)
                if not self._cmp(op, left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.IfExp):
            return self._ev(n.body if self._ev(n.test, x, look) else n.orelse, x, look)
        if isinstance(n, ast.Call):
            if n.func.id == "col":
                return self._col(n.args[0].value, look)
            args = [self._ev(a, x, look) for a in n.args]
            try:
                return FUNCS[n.func.id](*args)
            except TypeError as e:
                raise NotApplicable("%s(): %s" % (n.func.id, e))
        if isinstance(n, ast.Subscript):
            v = self._ev(n.value, x, look)
            if v is None:
                raise NotApplicable("empty")
            if not isinstance(v, (str, bytes, bytearray)):
                v = _text(v)
            sl = n.slice
            if hasattr(ast, "Index") and isinstance(sl, ast.Index):
                sl = sl.value
            if isinstance(sl, ast.Slice):
                lo = self._ev(sl.lower, x, look) if sl.lower is not None else None
                hi = self._ev(sl.upper, x, look) if sl.upper is not None else None
                st = self._ev(sl.step, x, look) if sl.step is not None else None
                return v[slice(_int(lo) if lo is not None else None,
                               _int(hi) if hi is not None else None,
                               _int(st) if st is not None else None)]
            i = _int(self._ev(sl, x, look), "the position")
            try:
                return v[i]
            except IndexError:
                raise NotApplicable("no character %d" % i)
        raise NotApplicable("cannot work out %s" % type(n).__name__)

    @staticmethod
    def _col(name, look):
        if look is None:
            raise NotApplicable("no row to read %s from" % name)
        try:
            return look(name)
        except KeyError:
            raise NotApplicable("no column %s" % name)

    @staticmethod
    def _bin(op, a, b):
        kind = type(op)
        if kind is ast.Add and (isinstance(a, str) or isinstance(b, str)) and \
                not (_is_num(a) and _is_num(b)):
            return _text(a) + _text(b)
        if kind is ast.Mult and isinstance(a, str) != isinstance(b, str):
            s, n = (a, b) if isinstance(a, str) else (b, a)
            n = _int(n, "the repeat count")
            if n > MAX_REPEAT:
                raise NotApplicable("repeat at most %d times" % MAX_REPEAT)
            return s * max(0, n)
        a, b = _num(a), _num(b)
        if kind in (ast.Div, ast.FloorDiv, ast.Mod) and b == 0:
            raise NotApplicable("division by 0")
        if kind is ast.Pow and abs(b) > MAX_POWER:
            raise NotApplicable("exponent at most %d" % MAX_POWER)
        try:
            return _tidy(_BIN[kind](a, b))
        except (OverflowError, ValueError) as e:
            raise NotApplicable(str(e))

    @staticmethod
    def _cmp(op, a, b):
        kind = type(op)
        if kind in (ast.In, ast.NotIn):
            return _CMP[kind](_text(a), _text(b))
        if a is None or b is None:
            if kind is ast.Eq:
                return a is b
            if kind is ast.NotEq:
                return a is not b
            raise NotApplicable("empty")
        if _is_num(a) and _is_num(b):
            return _CMP[kind](_num(a), _num(b))
        if isinstance(a, (bytes, bytearray)) or isinstance(b, (bytes, bytearray)):
            raise NotApplicable("a BLOB cannot be compared")
        return _CMP[kind](_text(a), _text(b))


def _is_num(v):
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        return bool(re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", s))
    return False


_cache = {}


def compile_formula(text):
    """A Formula (cached by text); FormulaError when it cannot be read."""
    f = _cache.get(text)
    if f is None:
        f = Formula(text)
        if len(_cache) > 256:
            _cache.clear()
        _cache[text] = f
    return f


def check(text):
    """None when the formula reads, else the reason."""
    try:
        compile_formula(text)
        return None
    except FormulaError as e:
        return str(e)
