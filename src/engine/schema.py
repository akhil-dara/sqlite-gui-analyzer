"""Schema model: tables/views read natively from sqlite_master, column metadata,
record-to-column mapping (incl. WITHOUT ROWID storage order) and row locators."""

import re
import sqlite3

from . import sqlsafe
from .fileformat.btree import BTreeReader
from .fileformat.record import InvalidText, decode_record, RecordError

BUILTIN_COLLATIONS = frozenset(("BINARY", "NOCASE", "RTRIM"))
ROWID_ALIASES = ("rowid", "_rowid_", "oid")

HIDDEN_NORMAL, HIDDEN_VTAB, HIDDEN_VIRTUAL_GEN, HIDDEN_STORED_GEN = 0, 1, 2, 3

_CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?(VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?",
    re.IGNORECASE)
_COLLATE_RE = re.compile(
    r"\bCOLLATE\s+(\"(?:[^\"]|\"\")+\"|\[[^\]]+\]|`(?:[^`]|``)+`|'(?:[^']|'')+'|[A-Za-z0-9_\-\.]+)",
    re.IGNORECASE)
_WITHOUT_ROWID_RE = re.compile(r"\)\s*(?:STRICT\s*,\s*)?WITHOUT\s+ROWID\s*(?:,\s*STRICT\s*)?;?\s*$",
                               re.IGNORECASE)


def quote_ident(name):
    if not isinstance(name, str):
        name = text_of_name(name) or ""
    return '"' + name.replace('"', '""') + '"'


def unquote_ident(tok):
    if len(tok) >= 2 and tok[0] in "\"`'[" :
        close = "]" if tok[0] == "[" else tok[0]
        if tok[-1] == close:
            inner = tok[1:-1]
            return inner if close == "]" else inner.replace(close * 2, close)
    return tok


def _skip_identifier(sql, pos):
    """Return the end position of one (possibly quoted) identifier starting at pos."""
    n = len(sql)
    if pos >= n:
        return pos
    ch = sql[pos]
    if ch in "\"`'":
        i = pos + 1
        while i < n:
            if sql[i] == ch:
                if i + 1 < n and sql[i + 1] == ch:
                    i += 2
                    continue
                return i + 1
            i += 1
        return n
    if ch == "[":
        end = sql.find("]", pos)
        return n if end < 0 else end + 1
    i = pos
    while i < n and not sql[i].isspace() and sql[i] not in "(.":
        i += 1
    return i


def rename_create_table(sql, new_name="t"):
    """Rewrite 'CREATE [VIRTUAL] TABLE <schema.>name ...' to use new_name.

    Lets us replay any table definition (even sqlite_* names) in a scratch database.
    Returns None if the statement is not a CREATE TABLE we can parse.
    """
    m = _CREATE_TABLE_RE.match(sql or "")
    if not m:
        return None
    pos = m.end()
    end = _skip_identifier(sql, pos)
    rest = sql[end:]
    stripped = rest.lstrip()
    if stripped.startswith("."):
        after_dot = end + (len(rest) - len(stripped)) + 1
        while after_dot < len(sql) and sql[after_dot].isspace():
            after_dot += 1
        end = _skip_identifier(sql, after_dot)
    if end == pos:
        return None
    return sql[:pos] + quote_ident(new_name) + sql[end:]


def collation_names(sql_texts):
    """Every non-builtin collation name referenced in the given SQL statements."""
    names = set()
    for sql in sql_texts:
        for tok in _COLLATE_RE.findall(sql or ""):
            name = unquote_ident(tok)
            if name.upper() not in BUILTIN_COLLATIONS:
                names.add(name)
    return names


def column_affinity(decl_type):
    """SQLite type-affinity rules (datatype3.html §3.1), in precedence order."""
    t = (text_of_name(decl_type) or "").upper()
    if "INT" in t:
        return "INTEGER"
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return "TEXT"
    if "BLOB" in t or not t:
        return "BLOB"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "REAL"
    return "NUMERIC"


def fallback_collation(a, b):
    """Stand-in for app-defined collations: Unicode case-insensitive, then binary."""
    fa, fb = a.casefold(), b.casefold()
    if fa != fb:
        return -1 if fa < fb else 1
    return (a > b) - (a < b)


def register_collations(conn, names):
    """Register the fallback collation under each name; return the names that could not be.

    Python 3.8-3.10 reject collation names with characters outside [0-9A-Za-z_] (e.g. Windows
    Search's 'UNICODE_en-US_LINGUISTIC_IGNORECASE'). Such names are skipped: SQLite still
    opens the file, and statements that need the collation fail and fall back to native reads.
    """
    failed = set()
    for name in names:
        try:
            conn.create_collation(name, fallback_collation)
        except (sqlite3.Error, ValueError):
            failed.add(name)
    return failed


def unregistrable_collations(names, issues=None):
    """Names this Python cannot register as collations; each is logged once as an Issue."""
    probe = sqlite3.connect(":memory:")
    try:
        failed = register_collations(probe, names)
    finally:
        probe.close()
    for name in sorted(failed):
        if issues is not None:
            issues.add("collation_unregistered",
                       "this Python cannot register a collation with this name; SQL that needs "
                       "it fails and the table is read natively (binary sort order)", name)
    return failed


def replace_collations(sql, names, replacement="NOCASE"):
    """Rewrite 'COLLATE <name>' to 'COLLATE <replacement>' for the given names.

    Used only to replay a CREATE statement in a scratch database: column metadata does not
    depend on the collation, but SQLite refuses a CREATE that names an unknown one.
    """
    if not names:
        return sql

    def sub(m):
        return ("COLLATE " + replacement) if unquote_ident(m.group(1)) in names else m.group(0)
    return _COLLATE_RE.sub(sub, sql)


class ColumnInfo(object):
    __slots__ = ("name", "decl_type", "notnull", "default_sql", "pk_pos", "hidden")

    def __init__(self, name, decl_type="", notnull=False, default_sql=None, pk_pos=0, hidden=0):
        self.name, self.decl_type, self.notnull = name, decl_type or "", bool(notnull)
        self.default_sql, self.pk_pos, self.hidden = default_sql, int(pk_pos or 0), int(hidden or 0)

    def __repr__(self):
        return "ColumnInfo(%r, %r, pk=%d, hidden=%d)" % (self.name, self.decl_type, self.pk_pos, self.hidden)


class Locator(object):
    """Stable identity of a row: ('rowid', int) | ('pk', tuple) | ('ordinal', int).

    Rows read natively also carry `cell` = (page, offset), so a row can be re-read
    exactly even when a damaged tree holds duplicate keys. Ordinal locators (views, virtual
    tables) have no key to re-read by, so they carry `snapshot` = (columns, values): the row
    exactly as it was read (after any sort or filter). Neither is part of equality.
    """
    __slots__ = ("kind", "value", "cell", "snapshot")

    def __init__(self, kind, value, cell=None, snapshot=None):
        self.kind, self.value, self.cell, self.snapshot = kind, value, cell, snapshot

    def display(self):
        if self.kind == "rowid":
            return str(self.value)
        if self.kind == "pk":
            vals = self.value
            return "pk=" + (repr(vals[0]) if len(vals) == 1 else repr(tuple(vals)))
        return "#%d" % self.value

    __str__ = display

    def __eq__(self, other):
        return isinstance(other, Locator) and (self.kind, self.value) == (other.kind, other.value)

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((self.kind, self.value))

    def __repr__(self):
        return "Locator(%r, %r)" % (self.kind, self.value)


class TableInfo(object):
    def __init__(self, name, kind, root_page, sql):
        self.name, self.kind, self.root_page, self.sql = name, kind, root_page, sql or ""
        self.columns = []
        self.without_rowid = False
        self.rowid_alias = None        # column index of INTEGER PRIMARY KEY alias
        self.rowid_name = None         # 'rowid' | '_rowid_' | 'oid' | None (all shadowed)
        self.pk_columns = []           # column indices in PK order
        self.pk_index_order = []       # (descending, collation) per PK column, from the PK index
        self.storage_order = []        # column indices in on-disk record order
        self.defaults = []             # python value per column (for rows older than ADD COLUMN)
        self.real_columns = []         # column indices with REAL affinity (ints read back as float)
        self.collations_needed = set()
        self.metadata_source = "none"  # 'scratch' | 'connection' | 'parser' | 'none'
        self.error = None

    @property
    def column_names(self):
        return [c.name for c in self.columns]

    @property
    def natively_readable(self):
        return self.kind == "table" and self.root_page > 0 and bool(self.columns)

    @property
    def locator_kind(self):
        if self.kind != "table":
            return "ordinal"
        if self.without_rowid:
            return "pk"
        return "rowid" if self.rowid_name else "native_rowid"

    def locator_for(self, rowid, row, ordinal, cell=None):
        kind = self.locator_kind
        if kind in ("rowid", "native_rowid"):
            return Locator("rowid", rowid, cell)
        if kind == "pk":
            return Locator("pk", tuple(row[i] for i in self.pk_columns), cell)
        return Locator("ordinal", ordinal, cell, (self.column_names, row))

    def record_to_row(self, rowid, values, damaged=False):
        """Map decoded record values to declared column order.

        Returns (row, flags) where flags may contain 'pre_alter', 'extra_values',
        'virtual_generated', 'damaged_record'.

        A short record is normally a row written before ALTER TABLE ADD COLUMN, and the
        missing columns take their (constant) DEFAULT, as SQLite does. When the record is
        damaged (damaged=True: the decoder reported a problem), the missing columns are
        unknown and stay None: showing a DEFAULT there would invent a value.
        """
        ncol = len(self.columns)
        row = [None] * ncol
        flags = set(("damaged_record",)) if damaged else set()
        order = self.storage_order
        for pos, ci in enumerate(order):
            if pos < len(values):
                row[ci] = values[pos]
            elif not damaged:
                row[ci] = self.defaults[ci]
                flags.add("pre_alter")
        if len(values) > len(order):
            flags.add("extra_values")
            row.extend(values[len(order):])
        if self.rowid_alias is not None and row[self.rowid_alias] is None:
            row[self.rowid_alias] = rowid
        for ci in self.real_columns:
            v = row[ci]
            if type(v) is int:
                row[ci] = float(v)
        if any(c.hidden == HIDDEN_VIRTUAL_GEN for c in self.columns):
            flags.add("virtual_generated")
        return row, flags


_CONSTANT_DEFAULT_RE = re.compile(
    r"^\s*(?:[-+]\s*)*(?:"
    r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"   # integer / real
    r"|0[xX][0-9a-fA-F]+"                                     # hex integer
    r"|'(?:[^']|'')*'"                                        # string
    r"|[xX]'[0-9a-fA-F]*'"                                    # blob
    r"|NULL|TRUE|FALSE"
    r")\s*$", re.IGNORECASE)


def _eval_default(scratch, default_sql):
    """The value SQLite gives a column missing from an old (pre-ALTER TABLE ADD COLUMN) record.

    Only literal constants are evaluated (ADD COLUMN only allows those). CURRENT_TIMESTAMP,
    CURRENT_DATE, CURRENT_TIME, function calls and other expressions give None: evaluating
    them now would fabricate a value (e.g. today's date) that was never stored. scratch: an
    sqlsafe.Scratch (or None).
    """
    if (scratch is None or not isinstance(default_sql, str)
            or not _CONSTANT_DEFAULT_RE.match(default_sql)):
        return None
    return scratch.literal(default_sql)


def _finish_table(t, cols, has_pk_index, scratch, pk_index=None):
    t.columns = cols
    names_lower = set(c.name.lower() for c in cols)
    t.rowid_name = next((a for a in ROWID_ALIASES if a not in names_lower), None)
    pk = sorted((c.pk_pos, i) for i, c in enumerate(cols) if c.pk_pos > 0)
    t.pk_columns = [i for _, i in pk]
    t.pk_index_order = [(pk_index or {}).get(i, (False, None)) for i in t.pk_columns]
    if t.without_rowid:
        rest = [i for i, c in enumerate(cols)
                if c.pk_pos == 0 and c.hidden != HIDDEN_VIRTUAL_GEN]
        t.storage_order = t.pk_columns + rest
    else:
        t.storage_order = [i for i, c in enumerate(cols) if c.hidden != HIDDEN_VIRTUAL_GEN]
        if (len(t.pk_columns) == 1 and cols[t.pk_columns[0]].decl_type.upper() == "INTEGER"
                and not has_pk_index):
            t.rowid_alias = t.pk_columns[0]
    t.defaults = [_eval_default(scratch, c.default_sql) for c in cols]
    t.real_columns = [i for i, c in enumerate(cols) if column_affinity(c.decl_type) == "REAL"]


def describe_table(t, collations, connection=None, issues=None):
    """Fill in column metadata for TableInfo t (kind 'table' or 'virtual').

    The CREATE statement is replayed in an isolated scratch database (engine.sqlsafe: only
    its first statement, never a query, within a step budget); when that fails, the evidence
    connection's PRAGMA is asked. Nothing here raises for hostile text: t.error and an Issue
    say why a table has no columns."""
    t.without_rowid = t.kind == "table" and bool(_WITHOUT_ROWID_RE.search(
        sqlsafe.first_statement(t.sql)[0].strip() + ";"))
    t.collations_needed = collation_names([t.sql])
    with sqlsafe.Scratch() as scratch:
        try:
            unregistered = register_collations(scratch.conn, collations)
        except sqlsafe.SQL_ERRORS:
            unregistered = set(collations)
        renamed = rename_create_table(t.sql)
        if renamed:
            renamed = replace_collations(renamed, unregistered)
            try:
                note = scratch.replay(renamed, virtual=t.kind == "virtual")
                cols, pk_index = _pragma_columns(scratch.pragma, "t")
                t.metadata_source = "scratch"
                _finish_table(t, cols, pk_index is not None, scratch, pk_index)
                if note and issues is not None:
                    issues.add("schema_text_ignored", "%s: the stored CREATE statement has "
                               "more after its end, which was not run" % note, t.name)
                return
            except sqlsafe.ReplayError as e:
                t.error = "scratch replay failed: %s" % e
                if issues is not None:
                    issues.add("schema_replay_failed", str(e), t.name, "warning")
            except sqlsafe.SQL_ERRORS as e:
                t.error = "scratch replay failed: %s" % sqlsafe._short(e)
        if connection is not None:
            try:
                cols, pk_index = _pragma_columns(
                    lambda q: connection.execute(q).fetchall(), t.name)
                if cols:
                    t.metadata_source = "connection"
                    _finish_table(t, cols, pk_index is not None, scratch, pk_index)
                    return
            except sqlsafe.SQL_ERRORS as e:
                t.error = "PRAGMA on evidence failed: %s" % sqlsafe._short(e)


def text_of_name(v):
    """A name, declared type or default read from SQLite as text: InvalidText (bytes that
    are not valid UTF-8) is decoded with U+FFFD for the bad bytes, None stays None."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def _pragma_columns(run, name):
    """(columns, pk_index): pk_index is None when the table has no separate PRIMARY KEY index,
    else {column index: (descending, collation)} for the key columns of that index. run(sql)
    returns the rows of one PRAGMA."""
    if not isinstance(name, str):
        raise ValueError("table name is not text")
    q = quote_ident(name)
    try:
        rows = run("PRAGMA table_xinfo(%s)" % q)
    except sqlsafe.SQL_ERRORS:
        rows = [tuple(r) + (0,) for r in run("PRAGMA table_info(%s)" % q)]
    cols = [ColumnInfo(text_of_name(r[1]), text_of_name(r[2]) or "", r[3],
                       text_of_name(r[4]), r[5], r[6]) for r in rows]
    pk_index = None
    try:
        for r in run("PRAGMA index_list(%s)" % q):
            if len(r) > 3 and r[3] == "pk" and isinstance(r[1], str):
                pk_index = {}
                for x in run("PRAGMA index_xinfo(%s)" % quote_ident(r[1])):
                    if x[5] and isinstance(x[1], int) and x[1] >= 0:
                        pk_index[x[1]] = (bool(x[3]), text_of_name(x[4]))
    except sqlsafe.SQL_ERRORS:
        pass
    return cols, pk_index


class SchemaEntry(object):
    __slots__ = ("type", "name", "tbl_name", "rootpage", "sql")

    def __init__(self, type_, name, tbl_name, rootpage, sql):
        self.type, self.name, self.tbl_name = type_, name, tbl_name
        self.rootpage, self.sql = rootpage, sql


class SchemaModel(object):
    def __init__(self, entries, issues=None):
        self.entries = entries
        self.issues = issues
        self._tables = {}
        self.collations = collation_names(e.sql for e in entries)
        self.unregistered_collations = unregistrable_collations(self.collations, issues)

    @classmethod
    def read_master(cls, pager, issues=None):
        """Decode sqlite_master (table b-tree rooted at page 1) through the pager."""
        reader = BTreeReader(pager, issues)
        entries = []
        for rowid, payload, ref in reader.iter_table(1):
            try:
                v = decode_record(payload, pager.encoding, issues, "sqlite_master")
            except RecordError as e:
                if issues is not None:
                    issues.add("bad_schema_record", str(e), "sqlite_master rowid %d" % rowid)
                continue
            v = (list(v) + [None] * 5)[:5]
            for k in (0, 1, 2, 4):
                if isinstance(v[k], InvalidText):
                    # text that is not valid in the database encoding: kept readable (U+FFFD
                    # for the bad bytes) so the object is still listed and read natively
                    if issues is not None:
                        issues.add("schema_invalid_text",
                                   "field %s is not valid text; bad bytes shown as U+FFFD"
                                   % ("type", "name", "tbl_name", "", "sql")[k],
                                   "sqlite_master rowid %d" % rowid)
                    v[k] = bytes(v[k]).decode("utf-8", "replace")
            root = v[3] if isinstance(v[3], int) else 0
            if not isinstance(v[3], int) and issues is not None:
                issues.add("bad_schema_record",
                          "field rootpage has type %s; used 0" % type(v[3]).__name__,
                          "sqlite_master rowid %d" % rowid)
            sql = v[4] if isinstance(v[4], str) else ""
            if not isinstance(v[4], (str, type(None))) and issues is not None:
                issues.add("bad_schema_record",
                          "field sql has type %s; used ''" % type(v[4]).__name__,
                          "sqlite_master rowid %d" % rowid)
            type_val = str(v[0] or "")
            if not isinstance(v[0], (str, type(None))) and issues is not None:
                issues.add("bad_schema_record",
                          "field type has type %s; used ''" % type(v[0]).__name__,
                          "sqlite_master rowid %d" % rowid)
            name = str(v[1] or "")
            if not isinstance(v[1], (str, type(None))) and issues is not None:
                issues.add("bad_schema_record",
                          "field name has type %s; used ''" % type(v[1]).__name__,
                          "sqlite_master rowid %d" % rowid)
            tbl_name = str(v[2] or "")
            if not isinstance(v[2], (str, type(None))) and issues is not None:
                issues.add("bad_schema_record",
                          "field tbl_name has type %s; used ''" % type(v[2]).__name__,
                          "sqlite_master rowid %d" % rowid)
            entries.append(SchemaEntry(type_val, name, tbl_name, root, sql))
        return entries

    @classmethod
    def load(cls, pager, connection=None, issues=None):
        model = cls(cls.read_master(pager, issues), issues)
        model.describe_all(connection)
        return model

    def describe_all(self, connection=None):
        for e in self.entries:
            if e.type == "table":
                upper = e.sql.lstrip().upper()
                kind = "virtual" if upper.startswith("CREATE VIRTUAL") else "table"
                t = TableInfo(e.name, kind, e.rootpage, e.sql)
                describe_table(t, self.collations, connection, self.issues)
                self._tables[e.name] = t
            elif e.type == "view":
                t = TableInfo(e.name, "view", 0, e.sql)
                if connection is not None:
                    try:
                        cols = _pragma_columns(lambda q: connection.execute(q).fetchall(),
                                               e.name)[0]
                        t.columns, t.metadata_source = cols, "connection"
                    except sqlsafe.SQL_ERRORS as ex:
                        t.error = sqlsafe._short(ex)
                self._tables[e.name] = t

    def get(self, name):
        return self._tables.get(name)

    def names(self, kind=None):
        return sorted(n for n, t in self._tables.items() if kind is None or t.kind == kind)

    def of_type(self, type_):
        return [e for e in self.entries if e.type == type_]
