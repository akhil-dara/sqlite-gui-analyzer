"""Calculations on a column's values: steps applied in order to make a new column (the stored
column is never changed). Each operation has a plain name, a group, its settings and a
function; a step is [key, setting, ...]. apply() never raises: a step that does not fit a
value (dividing text, say) leaves that cell empty and says why in explain().

Some operations read another column of the same row ('Divide by another column', 'Join with
another column'): their setting is that column's heading, and apply() gets a row lookup.
"""

import math
import re

# setting kinds: "number", "int", "text", "column"
OPS = []                        # (key, group, label, settings [(name, kind, default)], fn, words)
_BY_KEY = {}


class NotApplicable(Exception):
    """This step does not fit this value (the cell is left empty)."""


def _num(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, (bytes, bytearray)):
        raise NotApplicable("a BLOB is not a number")
    s = str(v).strip().replace(",", "")
    try:
        return int(s) if re.fullmatch(r"[-+]?\d+", s) else float(s)
    except ValueError:
        raise NotApplicable("'%s' is not a number" % (s[:20],))


def _tidy(x):
    """A float that is a whole number becomes an int (1.0 -> 1); others stay."""
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
        return int(x)
    return x


def _text(v):
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, float):
        return repr(_tidy(v)) if not isinstance(_tidy(v), int) else str(_tidy(v))
    return str(v)


def op(key, group, label, settings=(), words=""):
    def wrap(fn):
        OPS.append((key, group, label, list(settings), fn, words))
        _BY_KEY[key] = OPS[-1]
        return fn
    return wrap


def _div(a, b):
    if b == 0:
        raise NotApplicable("division by 0")
    return _tidy(_num(a) / b)


# -- a formula of your own ---------------------------------------------------------------------
@op("expr", "Formula", "Formula (x is this value; other columns by their name)",
    [("formula", "text", "x / 100")], "formula expression excel custom = + - * / if then else "
    "round upper json date")
def _o_expr(v, text, row=None):
    from . import formula as fm
    try:
        f = fm.compile_formula(text)
    except fm.FormulaError as e:
        raise NotApplicable("formula: %s" % e)

    def look(name):
        if row is None:
            raise NotApplicable("no row")
        return row(name)
    try:
        return _tidy(f.eval(v, look))
    except fm.NotApplicable as e:
        raise NotApplicable(str(e))


# -- numbers ---------------------------------------------------------------------------------
@op("div", "Numbers", "Divide by", [("by", "number", 100)], "÷ / paise cents")
def _o_div(v, by, row=None):
    return _div(v, _num(by))


@op("mul", "Numbers", "Multiply by", [("by", "number", 100)], "× *")
def _o_mul(v, by, row=None):
    return _tidy(_num(v) * _num(by))


@op("add", "Numbers", "Add", [("number", "number", 1)], "+ plus")
def _o_add(v, n, row=None):
    return _tidy(_num(v) + _num(n))


@op("sub", "Numbers", "Subtract", [("number", "number", 1)], "- minus")
def _o_sub(v, n, row=None):
    return _tidy(_num(v) - _num(n))


@op("round", "Numbers", "Round to decimals", [("decimals", "int", 2)], "round")
def _o_round(v, d, row=None):
    d = int(d)
    x = round(float(_num(v)), d)
    return int(x) if d <= 0 else x


@op("int", "Numbers", "Whole number (drop the decimals)", [], "integer truncate floor")
def _o_int(v, row=None):
    return int(math.trunc(_num(v)))


@op("abs", "Numbers", "Without the sign (absolute)", [], "abs absolute positive")
def _o_abs(v, row=None):
    return abs(_num(v))


@op("thousands", "Numbers", "Group the thousands (1,234,567.89)", [("decimals", "int", 2)],
    "format comma separator money")
def _o_thousands(v, d, row=None):
    return format(float(_num(v)), ",.%df" % max(0, int(d)))


@op("percent", "Numbers", "As a percentage of", [("of", "number", 100)], "% ratio")
def _o_percent(v, of, row=None):
    return "%s%%" % _tidy(round(_div(v, _num(of)) * 100, 2))


# -- with another column of the same row --------------------------------------------------------
def _other(row, name):
    if row is None:
        raise NotApplicable("no row")
    return row(name)


@op("col_add", "With another column", "Add another column", [("column", "column", "")],
    "+ sum two columns")
def _o_col_add(v, name, row=None):
    return _tidy(_num(v) + _num(_other(row, name)))


@op("col_sub", "With another column", "Subtract another column", [("column", "column", "")],
    "- difference between two columns")
def _o_col_sub(v, name, row=None):
    return _tidy(_num(v) - _num(_other(row, name)))


@op("col_mul", "With another column", "Multiply by another column", [("column", "column", "")],
    "× times")
def _o_col_mul(v, name, row=None):
    return _tidy(_num(v) * _num(_other(row, name)))


@op("col_div", "With another column", "Divide by another column", [("column", "column", "")],
    "÷ ratio")
def _o_col_div(v, name, row=None):
    return _div(v, _num(_other(row, name)))


@op("col_join", "With another column", "Join with another column (text)",
    [("column", "column", ""), ("between", "text", " ")], "concat combine merge")
def _o_col_join(v, name, sep, row=None):
    o = _other(row, name)
    return "%s%s%s" % (_text(v), sep if v not in (None, "") and o not in (None, "") else "",
                       _text(o))


@op("coalesce", "With another column", "If empty, take another column",
    [("column", "column", "")], "coalesce fallback null")
def _o_coalesce(v, name, row=None):
    return v if v not in (None, "") else _other(row, name)


# -- text ----------------------------------------------------------------------------------------
@op("prefix", "Text", "Add text before", [("text", "text", "₹ ")], "prefix currency")
def _o_prefix(v, t, row=None):
    return "%s%s" % (t, _text(v)) if v is not None else None


@op("suffix", "Text", "Add text after", [("text", "text", "")], "suffix unit")
def _o_suffix(v, t, row=None):
    return "%s%s" % (_text(v), t) if v is not None else None


@op("upper", "Text", "CAPITALS", [], "upper case")
def _o_upper(v, row=None):
    return _text(v).upper()


@op("lower", "Text", "small letters", [], "lower case")
def _o_lower(v, row=None):
    return _text(v).lower()


@op("title", "Text", "Capital First Letters", [], "title proper case")
def _o_title(v, row=None):
    return _text(v).title()


@op("trim", "Text", "Remove spaces at the ends", [], "trim strip")
def _o_trim(v, row=None):
    return _text(v).strip()


@op("left", "Text", "First characters", [("count", "int", 10)], "left start begin")
def _o_left(v, n, row=None):
    return _text(v)[:max(0, int(n))]


@op("right", "Text", "Last characters", [("count", "int", 10)], "right end last digits")
def _o_right(v, n, row=None):
    n = max(0, int(n))
    return _text(v)[-n:] if n else ""


@op("mid", "Text", "Part of the text", [("from (1 = first)", "int", 1), ("count", "int", 5)],
    "mid substring substr")
def _o_mid(v, start, n, row=None):
    s = max(1, int(start)) - 1
    return _text(v)[s:s + max(0, int(n))]


@op("replace", "Text", "Replace text", [("find", "text", ""), ("with", "text", "")],
    "substitute")
def _o_replace(v, find, with_, row=None):
    return _text(v).replace(find, with_) if find else _text(v)


@op("split", "Text", "Piece of the text (split at)", [("at", "text", "@"),
                                                     ("piece (1 = first)", "int", 1)],
    "split separator token before after @")
def _o_split(v, sep, n, row=None):
    parts = _text(v).split(sep) if sep else [_text(v)]
    i = int(n) - 1 if int(n) > 0 else len(parts) + int(n)
    if not 0 <= i < len(parts):
        raise NotApplicable("no piece %s" % n)
    return parts[i]


@op("digits", "Text", "Only the digits", [], "numbers phone strip")
def _o_digits(v, row=None):
    return re.sub(r"\D", "", _text(v))


@op("length", "Text", "Number of characters", [], "len length count")
def _o_length(v, row=None):
    return len(_text(v))


@op("default", "Text", "If empty, show", [("text", "text", "(none)")], "null blank default")
def _o_default(v, t, row=None):
    return t if v in (None, "") else v


def get(key):
    return _BY_KEY.get(key)


def settings_of(key):
    o = _BY_KEY.get(key)
    return o[3] if o else []


def new_step(key):
    """[key, default settings...] of an operation."""
    return [key] + [d for _n, _k, d in settings_of(key)]


def step_text(step):
    """'Divide by 100', 'Join with another column title (between ' ')'."""
    o = _BY_KEY.get(step[0])
    if o is None:
        return "unknown step %s" % step[0]
    _key, _g, label, settings, _fn, _w = o
    args = step[1:]
    if not settings:
        return label
    shown = []
    for (name, kind, _d), a in zip(settings, args):
        shown.append(repr(a) if kind == "text" else str(a))
    if len(settings) == 1:
        return "%s %s" % (label, shown[0])
    return "%s (%s)" % (label, ", ".join("%s %s" % (n, s) for (n, _k, _d), s in
                                          zip(settings, shown)))


def describe(steps):
    return ", then ".join(step_text(s) for s in steps)


def apply(value, steps, row=None):
    """The value after the steps (None when a step does not fit it)."""
    return explain(value, steps, row)[0]


def explain(value, steps, row=None):
    """(result, why it is empty or None): the steps applied in order."""
    v = value
    for st in steps:
        o = _BY_KEY.get(st[0])
        if o is None:
            return None, "unknown step %s" % st[0]
        if v is None and st[0] not in ("default", "coalesce", "col_join"):
            return None, "empty"
        try:
            v = o[4](v, *st[1:], row=row)
        except NotApplicable as e:
            return None, "%s: %s" % (o[2], e)
        except (TypeError, ValueError, OverflowError, ArithmeticError) as e:
            return None, "%s: %s" % (o[2], e)
    return v, None


def columns_needed(steps):
    """Headings of the other columns the steps read (a formula's names included)."""
    out = []
    for st in steps:
        o = _BY_KEY.get(st[0])
        if o is None:
            continue
        if st[0] == "expr" and len(st) > 1:
            from . import formula as fm
            try:
                names = fm.compile_formula(st[1]).names
            except fm.FormulaError:
                names = []
            for nm in names:
                if nm not in out:
                    out.append(nm)
            continue
        for (name, kind, _d), a in zip(o[3], st[1:]):
            if kind == "column" and a and a not in out:
                out.append(a)
    return out


def check_step(step):
    """None when the step's settings are usable, else the reason (a formula that cannot be
    read, say)."""
    if step and step[0] == "expr":
        from . import formula as fm
        return fm.check(step[1] if len(step) > 1 else "")
    return None


def coerce_setting(kind, text):
    """A setting typed in a box, as its kind; raises ValueError with a plain reason."""
    if kind == "number":
        try:
            return _tidy(float(str(text).replace(",", "")))
        except ValueError:
            raise ValueError("'%s' is not a number" % text)
    if kind == "int":
        try:
            return int(str(text).strip())
        except ValueError:
            raise ValueError("'%s' is not a whole number" % text)
    return str(text)
