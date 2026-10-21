"""Conditions on files found in a folder: which database files to list, open or even look at.

A condition is (field, operator, value): the file's name, its path relative to the folder
chosen, the name of the folder holding it, its extension or its size, compared as text
(case-insensitive) or as a number of bytes. Several conditions combine with 'all' or 'any'.
Nothing about any application is presumed: the conditions are the user's.

matches(conditions, mode, name, rel, size) -> bool. size is a number of bytes or a function
returning it (asked only when a size condition is there, so a scan never reads a file's size
it does not need).
"""
import fnmatch
import os

FIELDS = (("name", "File name"), ("path", "Path (in the folder)"),
          ("folder", "Folder name"), ("ext", "Extension"), ("size", "Size"))
TEXT_OPS = (("equals", "is"), ("not_equals", "is not"), ("contains", "contains"),
            ("not_contains", "does not contain"), ("starts", "starts with"),
            ("ends", "ends with"), ("like", "matches (* and ?)"))
SIZE_OPS = (("at_least", "at least"), ("at_most", "at most"))
SIZE_UNITS = (("B", 1), ("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3))
MODES = (("all", "all"), ("any", "any"))


class Condition(object):
    __slots__ = ("field", "op", "value")

    def __init__(self, field, op, value):
        self.field, self.op, self.value = field, op, value

    def __repr__(self):
        return "Condition(%r, %r, %r)" % (self.field, self.op, self.value)

    def complete(self):
        """A condition with something to compare against (an empty value is left out)."""
        if self.field == "size":
            return parse_size(self.value) is not None
        return bool(str(self.value or "").strip())


def parse_size(text):
    """Bytes of '10 MB', '512kb', '2048' (bytes), or None."""
    t = str(text or "").strip().upper().replace(",", "")
    if not t:
        return None
    unit = 1
    for name, mult in sorted(SIZE_UNITS, key=lambda u: -len(u[0])):
        if t.endswith(name):
            unit, t = mult, t[:-len(name)].strip()
            break
    try:
        return int(float(t) * unit)
    except ValueError:
        return None


def _field_text(field, name, rel):
    if field == "name":
        return name
    if field == "path":
        return rel.replace("\\", "/")
    if field == "folder":
        return os.path.basename(os.path.dirname(rel.replace("\\", "/"))) if "/" in \
            rel.replace("\\", "/") else ""
    if field == "ext":
        ext = os.path.splitext(name)[1]
        return ext[1:] if ext.startswith(".") else ext
    return ""


def _text_match(op, have, want):
    have, want = have.lower(), want.strip().lower()
    if op == "equals":
        return have == want
    if op == "not_equals":
        return have != want
    if op == "contains":
        return want in have
    if op == "not_contains":
        return want not in have
    if op == "starts":
        return have.startswith(want)
    if op == "ends":
        return have.endswith(want)
    if op == "like":
        return fnmatch.fnmatchcase(have, want)
    raise ValueError("unknown operator %r" % op)


def one(cond, name, rel, size):
    if cond.field == "size":
        limit = parse_size(cond.value)
        n = size() if callable(size) else size
        if n is None:
            return False
        return n >= limit if cond.op == "at_least" else n <= limit
    return _text_match(cond.op, _field_text(cond.field, name, rel), str(cond.value))


def matches(conditions, mode, name, rel, size=None):
    """True when the file passes the complete conditions (all of them, or any one); no
    complete condition: every file passes."""
    conds = [c for c in conditions if c.complete()]
    if not conds:
        return True
    if mode == "any":
        return any(one(c, name, rel, size) for c in conds)
    return all(one(c, name, rel, size) for c in conds)


def describe(conditions, mode):
    """The conditions in words, for the status line ('name is places.sqlite or name is
    History')."""
    conds = [c for c in conditions if c.complete()]
    if not conds:
        return ""
    fields, ops = dict(FIELDS), dict(TEXT_OPS + SIZE_OPS)
    parts = ["%s %s %s" % (fields.get(c.field, c.field).lower(), ops.get(c.op, c.op),
                           str(c.value).strip()) for c in conds]
    return (" or " if mode == "any" else " and ").join(parts)
