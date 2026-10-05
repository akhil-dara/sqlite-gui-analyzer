"""Schema templates: what a record of a given table can look like on disk.

A carved record is only attributed to a table when every value is one SQLite could have
stored in that table. The rules follow SQLite's type affinity (datatype3.html):
  * TEXT affinity converts numbers to text, so an INTEGER/REAL value there is impossible;
  * INTEGER / REAL / NUMERIC affinity converts well-formed numeric text to a number, so such
    text is impossible there, and INTEGER / NUMERIC store integral reals as integers;
  * an INTEGER PRIMARY KEY (rowid alias) is always stored as NULL in the record;
  * a NOT NULL column never holds NULL.
Values that are possible but unusual for the declared type (a BLOB in a TEXT column, text in
an INTEGER column) are allowed but lower the confidence ("atypical").
"""

import re

from ..fileformat.record import InvalidText
from ..schema import TableInfo, column_affinity, describe_table

MASTER_SQL = "CREATE TABLE sqlite_master(type text, name text, tbl_name text, rootpage int, sql text)"

_NUMERIC_TEXT = re.compile(r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$")
_CONTROL = re.compile(u"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

NULL, INT, REAL, TEXT, BLOB, BAD = "null", "int", "real", "text", "blob", "bad"
_FIXED = {0: NULL, 1: INT, 2: INT, 3: INT, 4: INT, 5: INT, 6: INT, 7: REAL, 8: INT, 9: INT,
          10: BAD, 11: BAD}
_INT_TYPES_BY_SIZE = {1: 1, 2: 2, 3: 3, 4: 4, 6: 5, 8: 6}
# storage classes a column of each affinity can hold: True = typical, False = possible but atypical
_ALLOWED = {
    "INTEGER": {NULL: True, INT: True, REAL: False, TEXT: False, BLOB: False},
    "REAL": {NULL: True, INT: True, REAL: True, TEXT: False, BLOB: False},
    "NUMERIC": {NULL: True, INT: True, REAL: True, TEXT: False, BLOB: False},
    "TEXT": {NULL: True, TEXT: True, BLOB: False},
    "BLOB": {NULL: True, INT: True, REAL: True, TEXT: True, BLOB: True},
}


def type_class(st):
    if st < 12:
        return _FIXED[st]
    return TEXT if st & 1 else BLOB


def looks_numeric(text):
    return bool(_NUMERIC_TEXT.match(text))


def printable(text):
    """Text without control characters other than tab / newline / carriage return."""
    return not _CONTROL.search(text)


class Template(object):
    """On-disk shape of one table's records (storage order)."""

    def __init__(self, info, kind="table", dropped=False):
        self.info = info
        self.name = info.name
        self.kind = kind                     # 'table' | 'master'
        self.dropped = dropped
        self.index_tree = bool(info.without_rowid)
        cols = info.columns
        order = info.storage_order
        self.n = len(order)
        self.affinity = [column_affinity(cols[i].decl_type) for i in order]
        # WITHOUT ROWID primary key columns are NOT NULL whether declared so or not
        pk = set(info.pk_columns) if info.without_rowid else set()
        self.notnull = [bool(cols[i].notnull) or i in pk for i in order]
        self.alias = order.index(info.rowid_alias) if info.rowid_alias is not None else None
        self.strict = bool(re.search(r"\)\s*(?:WITHOUT\s+ROWID\s*,\s*)?STRICT\b", info.sql or "",
                                     re.IGNORECASE))
        self.length_prior = [set() for _ in range(self.n)]
        self.int_range = [None] * self.n
        self._options = {}

    @classmethod
    def for_table(cls, info, dropped=False):
        """A template for a natively readable table, or None."""
        if info is None or info.kind != "table" or not info.columns or not info.storage_order:
            return None
        return cls(info, "table", dropped)

    @classmethod
    def master(cls):
        t = TableInfo("sqlite_master", "table", 1, MASTER_SQL)
        describe_table(t, set())
        return cls(t, "master")

    @property
    def columns(self):
        return self.info.column_names

    def __repr__(self):
        return "Template(%s, n=%d)" % (self.name, self.n)

    # -- serial types -----------------------------------------------------
    def type_ok(self, pos, st):
        """(allowed, typical) for serial type st in storage position pos."""
        cls = type_class(st)
        if cls == BAD:
            return False, False
        if pos == self.alias:
            return cls == NULL, True
        if cls == NULL:
            return not self.notnull[pos], True
        allowed = _ALLOWED[self.affinity[pos]]
        if cls not in allowed:
            return False, False
        return True, allowed[cls]

    def check_types(self, types):
        """(ok, atypical_count) for a full list of serial types."""
        if len(types) != self.n:
            return False, 0
        atypical = 0
        for pos, st in enumerate(types):
            ok, typical = self.type_ok(pos, st)
            if not ok:
                return False, 0
            if not typical:
                atypical += 1
        return True, atypical

    def size_options(self, pos, varint_len):
        """Possible (size, serial type) pairs for an unknown serial type in storage position
        `pos` whose varint used `varint_len` bytes. Variable-length text/blob types are given
        as (None, 'text') / (None, 'blob') and sized by the caller. Only storage classes
        typical for the column are offered: a lost value is never guessed to be unusual."""
        key = (pos, varint_len)
        cached = self._options.get(key)
        if cached is None:
            cached = self._options[key] = self._size_options(pos, varint_len)
        return cached

    def _size_options(self, pos, varint_len):
        out = []
        if pos == self.alias:
            return [(0, 0)] if varint_len == 1 else []
        aff = self.affinity[pos]
        allowed = _ALLOWED[aff]
        if varint_len == 1:
            # a lost zero-size value is NULL, 0 or 1: offered once (NULL unless NOT NULL) and
            # reported as uncertain by the caller
            if not self.notnull[pos]:
                out.append((0, 0))
            elif INT in allowed:
                out.append((0, 8))
            if allowed.get(INT):
                out.extend((size, st) for size, st in sorted(_INT_TYPES_BY_SIZE.items()))
            if allowed.get(REAL):
                out.append((8, 7))
        if allowed.get(TEXT):
            out.append((None, TEXT))
        if allowed.get(BLOB):
            out.append((None, BLOB))
        return out

    # -- decoded values ---------------------------------------------------
    def check_values(self, values, types, solved=()):
        """(ok, atypical, notes) for decoded values. `solved` lists storage positions whose
        size was inferred (not read): their text must be clean UTF-8 without control chars."""
        atypical, notes = 0, []
        for pos, (v, st) in enumerate(zip(values, types)):
            aff = self.affinity[pos] if pos < self.n else "BLOB"
            if isinstance(v, InvalidText):
                if pos in solved:
                    return False, 0, ["invalid text in an inferred column"]
                atypical += 1
                notes.append("column %s: text not valid in the database encoding (damaged)"
                             % self.columns[self.info.storage_order[pos]])
                continue
            if isinstance(v, str):
                if aff in ("INTEGER", "REAL", "NUMERIC") and looks_numeric(v):
                    return False, 0, ["numeric text in a %s column" % aff]
                if not printable(v):
                    if pos in solved:
                        return False, 0, ["control characters in an inferred column"]
                    atypical += 1
                continue
            if isinstance(v, float) and aff in ("INTEGER", "NUMERIC") and st == 7:
                if v == v and abs(v) < 9.2e18 and v == int(v):
                    return False, 0, ["integral real in a %s column" % aff]
        return True, atypical, notes

    def learn_lengths(self, rows):
        """Remember text/blob lengths and integer ranges seen in live rows (storage order),
        used to choose between otherwise equal reconstructions."""
        order = self.info.storage_order
        for row in rows:
            for pos, ci in enumerate(order):
                v = row[ci] if ci < len(row) else None
                if isinstance(v, (str, bytes)) and len(self.length_prior[pos]) < 64:
                    self.length_prior[pos].add(len(v.encode("utf-8") if isinstance(v, str) else v))
                elif isinstance(v, int) and not isinstance(v, bool):
                    lo, hi = self.int_range[pos] or (v, v)
                    self.int_range[pos] = (min(lo, v), max(hi, v))

    def plausible_int(self, pos, v):
        """True / False when live rows give a range for this column, None otherwise. The range
        is widened generously: it only separates likely readings from unlikely ones."""
        rng = self.int_range[pos]
        if rng is None:
            return None
        lo, hi = rng
        span = max(hi - lo, abs(hi), abs(lo), 16)
        return lo - span <= v <= hi + span


def templates_for(schema, dropped_infos=()):
    """Templates for every natively readable table of the schema, the dropped tables given
    as TableInfo, and sqlite_master."""
    out = []
    for name in schema.names("table"):
        t = Template.for_table(schema.get(name))
        if t is not None:
            out.append(t)
    for info in dropped_infos:
        t = Template.for_table(info, dropped=True)
        if t is not None:
            out.append(t)
    out.append(Template.master())
    return out
