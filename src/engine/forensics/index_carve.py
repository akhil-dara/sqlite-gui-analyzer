"""Carving deleted entries of ordinary indexes.

An index b-tree (leaf pages 0x0A, interior pages 0x02) of a rowid table stores one record
per row: [indexed values..., rowid] (for a WITHOUT ROWID table: [indexed values..., primary
key columns not already indexed]). Deleting a row frees its index entry the same way it frees
the table cell, so entries survive in index-page freeblocks and unallocated space, on freed
index pages and in older WAL / journal copies of index pages - often after the table row
itself was overwritten. This module recovers them.

What makes an index entry:
  * its index comes from sqlite_master: the CREATE INDEX statement (or the table's UNIQUE /
    PRIMARY KEY constraints for autoindexes) is replayed in an in-memory scratch database and
    PRAGMA index_xinfo lists its columns (plain columns, expressions, DESC, collation), with a
    text parser as fallback when the replay fails (e.g. an unknown function in an expression);
  * the record has exactly the index's column count; every value is one the column's
    affinity allows (expressions: anything); the trailing rowid is an integer;
  * entries on a page of a live index are attributed to it; on a freed or unreferenced page
    the page's intact cells decide which index it belonged to (the index they all fit).
Live entries (the same values and rowid are in the index now) are dropped; an entry whose
rowid is in the index now with other values is kept and flagged 'prior_version'.

Records: table = the indexed table, columns = the indexed columns + 'rowid', rowid = the
entry's rowid, index = the index name, flags include 'index_entry'. link_records() then notes
where the same rowid was also recovered as a (partial) table record, or where nothing else of
the row is left.
"""

import re

from .. import sqlsafe
from ..fileformat.btree import BTreeReader, INDEX_INTERIOR, INDEX_LEAF, parse_page_header
from ..fileformat.record import InvalidText, decode_record_lenient, read_varint, to_signed64
from ..fileformat.wal import CURRENT
from ..issues import IssueLog
from ..schema import (column_affinity, quote_ident, register_collations, rename_create_table,
                      replace_collations, unquote_ident, _skip_identifier)
from .carve import INDEX_KIND, IINT_KIND, Carver, _Site
from .cellparse import parse_intact
from .pages import AsOfFrameView, is_btree_page
from .provenance import (FREELIST, JOURNAL, JOURNAL_FILE, MAIN_FILE, ORPHAN, REPLACED, WAL,
                         WAL_FILE, Record, value_key, worst, LOW, MEDIUM)
from .templates import BLOB, INT, NULL, REAL, TEXT, Template, printable, type_class

INDEX_FLAG = "index_entry"
LIVE_ENTRY_CAP = "live_index_entries"     # engine.limits name
_INDEX_TYPES = (INDEX_LEAF, INDEX_INTERIOR)
_CREATE_INDEX_RE = re.compile(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?",
                              re.IGNORECASE)
_ON_RE = re.compile(r"\s*ON\s+", re.IGNORECASE)
_ORDER_RE = re.compile(r"\s+(ASC|DESC)\s*$", re.IGNORECASE)
_COLLATE_TAIL_RE = re.compile(r"\s+COLLATE\s+(\"(?:[^\"]|\"\")+\"|\[[^\]]+\]|`(?:[^`]|``)+`|"
                              r"'(?:[^']|'')+'|[A-Za-z0-9_\-\.]+)\s*$", re.IGNORECASE)
_AUTO_RE = re.compile(r"^sqlite_autoindex_.+_(\d+)$")


class IndexColumn(object):
    __slots__ = ("name", "cid", "expr", "desc", "collation", "key")

    def __init__(self, name, cid, expr=None, desc=False, collation=None, key=True):
        self.name, self.cid, self.expr, self.desc = name, cid, expr, bool(desc)
        self.collation, self.key = collation, bool(key)

    def __repr__(self):
        return "IndexColumn(%r, cid=%r%s)" % (self.name, self.cid, " DESC" if self.desc else "")


class IndexSpec(object):
    """What one index stores: its record columns in order, and where it lives."""

    def __init__(self, name, table, info, root_page, sql, columns, unique=False, where=None,
                 auto=False, dropped=False, source="pragma"):
        self.name, self.table, self.info, self.root_page = name, table, info, root_page
        self.sql = sql or ""
        self.columns = columns              # [IndexColumn] in record order
        self.unique, self.where, self.auto = bool(unique), where, bool(auto)
        self.dropped = dropped
        self.source = source                # 'pragma' | 'parser'

    @property
    def has_rowid(self):
        return bool(self.columns) and self.columns[-1].cid == -1

    @property
    def names(self):
        return [c.name for c in self.columns]

    def describe(self):
        """Short text: 'UNIQUE index idx on t(a DESC, lower(b)) WHERE ...'."""
        keys = ", ".join(c.name + (" DESC" if c.desc else "") for c in self.columns if c.key)
        text = "%sindex %s on %s(%s)" % ("UNIQUE " if self.unique else "", self.name, self.table,
                                          keys)
        if self.where:
            text += " WHERE " + self.where
        return text

    def __repr__(self):
        return "IndexSpec(%s on %s: %s)" % (self.name, self.table, self.names)


# -- reading the index definitions ---------------------------------------------------------------
def _matching_paren(sql, pos):
    """Index of the ')' closing the '(' at sql[pos] (quotes respected), or -1."""
    depth, i, n = 0, pos, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "\"'`[":
            i = _skip_identifier(sql, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top(text):
    """Split at top-level commas (quotes and parentheses respected)."""
    parts, depth, start, i, n = [], 0, 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`[":
            i = _skip_identifier(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts]


def parse_create_index(sql):
    """(unique, index_name, table_name, [(term, desc, collation)], where) of a CREATE INDEX
    statement, or None when it does not parse."""
    m = _CREATE_INDEX_RE.match(sql or "")
    if not m:
        return None
    pos = m.end()
    end = _skip_identifier(sql, pos)
    if end < len(sql) and sql[end] == ".":
        pos, end = end + 1, _skip_identifier(sql, end + 1)
    name = unquote_ident(sql[pos:end].strip())
    on = _ON_RE.match(sql, end)
    if not on:
        return None
    tpos = on.end()
    tend = _skip_identifier(sql, tpos)
    table = unquote_ident(sql[tpos:tend].strip())
    open_at = sql.find("(", tend)
    if open_at < 0:
        return None
    close = _matching_paren(sql, open_at)
    if close < 0:
        return None
    terms = []
    for part in _split_top(sql[open_at + 1:close]):
        if not part:
            return None
        desc = False
        om = _ORDER_RE.search(part)
        if om:
            desc = om.group(1).upper() == "DESC"
            part = part[:om.start()].rstrip()
        coll = None
        cm = _COLLATE_TAIL_RE.search(part)
        if cm:
            coll = unquote_ident(cm.group(1))
            part = part[:cm.start()].rstrip()
        terms.append((part, desc, coll))
    rest = sql[close + 1:].strip().rstrip(";").strip()
    where = None
    if rest[:5].upper() == "WHERE":
        where = rest[5:].strip() or None
    return bool(m.group(1)), name, table, terms, where


def _rename_create_index(sql, table="t", index="i"):
    """The CREATE INDEX statement on scratch names, or None."""
    m = _CREATE_INDEX_RE.match(sql or "")
    if not m:
        return None
    pos = m.end()
    end = _skip_identifier(sql, pos)
    if end < len(sql) and sql[end] == ".":
        end = _skip_identifier(sql, end + 1)
    on = _ON_RE.match(sql, end)
    if not on:
        return None
    tend = _skip_identifier(sql, on.end())
    return "%s%s ON %s%s" % (sql[:m.end()], quote_ident(index), quote_ident(table), sql[tend:])


def _pragma_xinfo(info, entry, collations):
    """index_xinfo rows [(cid, name, desc, collation, key)] from a scratch replay, and the
    index's unique flag, or (None, None)."""
    table_sql = rename_create_table(info.sql)
    if not table_sql:
        return None, None
    with sqlsafe.Scratch() as scratch:
        try:
            bad = register_collations(scratch.conn, collations)
            scratch.replay(replace_collations(table_sql, bad))
            auto = _AUTO_RE.match(entry.name or "")
            if entry.sql:
                isql = _rename_create_index(entry.sql)
                if not isql:
                    return None, None
                scratch.replay(replace_collations(isql, bad))
                target = "i"
            elif auto:
                target = "sqlite_autoindex_t_%s" % auto.group(1)
            else:
                return None, None
            unique = False
            for r in scratch.pragma("PRAGMA index_list(\"t\")"):
                if r[1] == target:
                    unique = bool(r[2])
                    break
            else:
                return None, None
            rows = scratch.pragma("PRAGMA index_xinfo(%s)" % quote_ident(target))
            return [(r[1], r[2], r[3], r[4], r[5]) for r in rows], unique
        except sqlsafe.SQL_ERRORS:
            return None, None


def _parsed_columns(info, parsed):
    """[IndexColumn] from a parsed CREATE INDEX (fallback when the replay fails)."""
    _unique, _name, _table, terms, _where = parsed
    names = [c.name.lower() for c in info.columns]
    out = []
    for term, desc, coll in terms:
        ident = unquote_ident(term)
        if ident.lower() in names and (term == ident or term[:1] in "\"`'["):
            cid = names.index(ident.lower())
            out.append(IndexColumn(info.columns[cid].name, cid, None, desc, coll))
        else:
            out.append(IndexColumn(term, -2, term, desc, coll))
    return _with_tail(info, out)


def _with_tail(info, keys):
    """Key columns + what the index stores after them: the rowid, or the primary key columns
    of a WITHOUT ROWID table that are not key columns already."""
    out = list(keys)
    if info.without_rowid:
        have = set(c.cid for c in keys if c.cid >= 0)
        for cid in info.pk_columns:
            if cid not in have:
                out.append(IndexColumn(info.columns[cid].name, cid, None, False, None, False))
    else:
        out.append(IndexColumn("rowid", -1, None, False, None, False))
    return out


def index_specs(schema, entries, issues=None, tables=None, dropped=()):
    """IndexSpec for every index of a table the schema describes (and the dropped indexes
    given as DroppedObject whose table is known). tables limits them to those tables."""
    by_table = {}
    for d in dropped or ():
        if getattr(d, "type", None) == "table" and d.info is not None:
            by_table.setdefault(d.name, d.info)
    wanted = set(tables) if tables is not None else None
    todo = [(e, False) for e in entries if e.type == "index"]
    todo += [(_Entry(d), True) for d in (dropped or ())
             if getattr(d, "type", None) == "index" and d.status == "dropped"]
    out, seen = [], set()
    for e, is_dropped in todo:
        if not isinstance(e.rootpage, int) or e.rootpage <= 0 or not e.name:
            continue
        info = schema.get(e.tbl_name) if not is_dropped else \
            (by_table.get(e.tbl_name) or schema.get(e.tbl_name))
        if info is None or info.kind != "table" or not info.columns:
            continue
        if wanted is not None and info.name not in wanted:
            continue
        key = (e.name, e.rootpage)
        if key in seen:
            continue
        seen.add(key)
        try:
            spec = _spec_for(info, e, schema.collations, is_dropped)
        except Exception as ex:             # an odd definition must not stop the carve
            spec = None
            if issues is not None:
                issues.add("index_schema", "%s: %s" % (type(ex).__name__, ex), e.name, "info")
        if spec is None:
            if issues is not None:
                issues.add("index_schema", "columns of this index could not be worked out",
                           e.name, "info")
            continue
        out.append(spec)
    return out


class _Entry(object):
    """A dropped index as a sqlite_master-like entry."""

    def __init__(self, d):
        self.type, self.name, self.tbl_name = "index", d.name, d.tbl_name
        self.rootpage, self.sql = d.rootpage, d.sql


def _spec_for(info, e, collations, is_dropped):
    parsed = parse_create_index(e.sql) if e.sql else None
    where = parsed[4] if parsed else None
    rows, unique = _pragma_xinfo(info, e, collations)
    auto = not e.sql
    if rows:
        exprs = [t[0] for t in parsed[3]] if parsed else []
        cols, k = [], 0
        for cid, name, desc, coll, key in rows:
            if key:
                expr = exprs[k] if k < len(exprs) else None
                k += 1
            else:
                expr = None
            if cid == -1:
                cols.append(IndexColumn("rowid", -1, None, desc, coll, key))
            elif cid == -2 or cid is None or cid >= len(info.columns):
                text = expr or "expr%d" % len(cols)
                cols.append(IndexColumn(text, -2, text, desc, coll, key))
            else:
                cols.append(IndexColumn(info.columns[cid].name, cid, None, desc,
                                        coll if coll != "BINARY" else None, key))
        return IndexSpec(e.name, info.name, info, e.rootpage, e.sql, cols, unique, where, auto,
                         is_dropped, "pragma")
    if parsed is None:
        return None
    return IndexSpec(e.name, info.name, info, e.rootpage, e.sql, _parsed_columns(info, parsed),
                     parsed[0], where, auto, is_dropped, "parser")


# -- templates -------------------------------------------------------------------------------
class _IndexInfo(object):
    """The parts of TableInfo the carver reads, for an index record."""

    def __init__(self, spec):
        self.name = spec.name
        self.columns = spec.columns
        self.column_names = spec.names
        self.storage_order = list(range(len(spec.columns)))
        self.rowid_alias = None
        self.pk_columns = []
        self.without_rowid = True
        self.sql = spec.sql

    @staticmethod
    def record_to_row(rowid, values, damaged=False):
        return list(values), set()


class IndexTemplate(Template):
    """On-disk shape of one index's records."""

    def __init__(self, spec):
        self.spec = spec
        self.info = _IndexInfo(spec)
        self.name = spec.name
        self.table = spec.table
        self.kind = "index"
        self.dropped = spec.dropped
        self.index_tree = True
        self.n = len(spec.columns)
        cols = spec.info.columns
        self.affinity, self.notnull = [], []
        for c in spec.columns:
            if c.cid == -1:
                self.affinity.append("INTEGER")
                self.notnull.append(True)
            elif c.cid is not None and 0 <= c.cid < len(cols):
                col = cols[c.cid]
                self.affinity.append(column_affinity(col.decl_type))
                pk = spec.info.without_rowid and c.cid in spec.info.pk_columns
                alias = c.cid == spec.info.rowid_alias
                self.notnull.append(bool(col.notnull) or pk or alias)
            else:
                self.affinity.append("BLOB")        # an expression has no affinity
                self.notnull.append(False)
        self.rowid_pos = self.n - 1 if spec.has_rowid else None
        self.alias = None
        self.strict = False
        self.length_prior = [set() for _ in range(self.n)]
        self.int_range = [None] * self.n
        self.seen = [set() for _ in range(self.n)]     # storage classes live entries hold
        self.free_variable = True       # see cellparse._assign
        self.encoding = "utf-8"         # the database's (set by the carver)
        self.row_lookup = None          # (table, rowid) -> {column index: value} or None
        self._options = {}

    @property
    def columns(self):
        return self.info.column_names

    def __repr__(self):
        return "IndexTemplate(%s on %s, n=%d)" % (self.name, self.table, self.n)

    def type_ok(self, pos, st):
        if pos == self.rowid_pos:
            return type_class(st) == INT, True
        ok, typical = Template.type_ok(self, pos, st)
        seen = self.seen[pos]
        if ok and typical and self.affinity[pos] == "BLOB" and seen and \
                type_class(st) not in seen:
            typical = False             # e.g. a number where live entries hold only text
        return ok, typical

    def _size_options(self, pos, varint_len):
        out = Template._size_options(self, pos, varint_len)
        if pos == self.rowid_pos:
            out = [o for o in out if o[1] not in (TEXT, BLOB, 0, 7)]
            if varint_len == 1 and (0, 9) not in out:
                out.append((0, 9))
        elif self.affinity[pos] == "BLOB" and self.seen[pos]:
            # a lost value of an untyped column (an expression) is only guessed to be of a
            # storage class its live entries hold
            seen = self.seen[pos]
            out = [o for o in out if (o[1] if o[1] in (TEXT, BLOB) else type_class(o[1])) in seen]
        return out

    def learn_lengths(self, rows):
        Template.learn_lengths(self, rows)
        for row in rows:
            for pos in range(min(self.n, len(row))):
                v = row[pos]
                if v is None:
                    self.seen[pos].add(NULL)
                elif isinstance(v, bytes):
                    self.seen[pos].add(BLOB)
                elif isinstance(v, str):
                    self.seen[pos].add(TEXT)
                elif isinstance(v, float):
                    self.seen[pos].add(REAL)
                elif isinstance(v, int):
                    self.seen[pos].add(INT)
        self._options = {}

    def check_values(self, values, types, solved=()):
        ok, atypical, notes = Template.check_values(self, values, types, solved)
        if ok and self.rowid_pos is not None and self.rowid_pos < len(values):
            rowid = values[self.rowid_pos]
            if isinstance(rowid, int) and rowid <= 0:
                atypical += 1
                notes = list(notes) + ["rowid %d is not positive" % rowid]
            elif isinstance(rowid, int) and self.plausible_int(self.rowid_pos, rowid) is False:
                atypical += 1
                notes = list(notes) + ["rowid %d is far outside the table's rowids" % rowid]
        return ok, atypical, notes

    def plausible(self, cell, values, atypical):
        """Evidence rules for an index entry (stricter than for table records: entries are
        short, so chance fits are common): some content, text that is valid and printable,
        and no unusual value at all in a cell whose first bytes were rebuilt."""
        rp = self.rowid_pos
        if cell.payload_len <= cell.header_len or \
                not any(v is not None and v != "" and v != b"" for i, v in enumerate(values)
                        if i != rp):
            return False                # an entry of NULL keys says nothing but a rowid
        for v in values:
            if isinstance(v, InvalidText) or (isinstance(v, str) and not printable(v)):
                return False
        if rp is not None and rp < len(values) and (not isinstance(values[rp], int)
                                                    or values[rp] <= 0):
            return False                # SQLite only gives out positive rowids by itself
        if cell.rebuilt:
            if cell.payload_len - cell.header_len <= 0:
                return False            # every value was guessed: nothing survives to read
            if not any(v and not (isinstance(v, bytes) and not v.strip(b"\x00"))
                       for i, v in enumerate(values) if i != rp):
                return False            # zeroed bytes read as zeros: no evidence of an entry
            for pos in range(min(cell.lost, len(values))):
                v = values[pos]
                if pos != self.rowid_pos and isinstance(v, int) and \
                        self.plausible_int(pos, v) is False:
                    return False        # a lost integer far outside what the index holds
            return atypical == 0
        return atypical < 2

    def adjust_score(self, score, row):
        """Reorder the carver's score for index entries: after completeness, rebuilt bytes and
        unusual values, prefer a reading that matches the row its rowid points to (a live row
        or a recovered table record), then lengths and ranges seen in live entries, and only
        then fewer inferred sizes. The first five items decide which readings tie."""
        complete, rebuilt, atypical, solved, uncertain, prior, other, master = score
        ref = 0
        lookup = self.row_lookup
        if lookup is not None and self.rowid_pos is not None and self.rowid_pos < len(row) \
                and isinstance(row[self.rowid_pos], int):
            got = lookup(self.table, row[self.rowid_pos])
            if got is not None and _content_match(self.spec, row, got):
                ref = -1
        return (complete, rebuilt, atypical, ref, prior, solved, uncertain, other, master)

    def compare(self, a, b):
        """SQLite's order of two entries' key columns (-1, 0, 1), or None when a collation
        is not known here."""
        for pos, col in enumerate(self.spec.columns):
            if not col.key:
                break
            if pos >= len(a) or pos >= len(b):
                return 0
            c = _cmp_value(a[pos], b[pos], col.collation, self.encoding)
            if c is None:
                return None
            if c:
                return -c if col.desc else c
        return 0


def _class_rank(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return 1
    if isinstance(v, str) and not isinstance(v, InvalidText):
        return 2
    return 3


def _cmp_value(a, b, collation, encoding="utf-8"):
    """SQLite's comparison of two stored values under a collation (None: BINARY; text is
    compared as bytes in the database encoding), or None when the collation is unknown."""
    ra, rb = _class_rank(a), _class_rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if ra == 0:
        return 0
    if ra == 2:
        coll = (collation or "BINARY").upper()
        if coll == "NOCASE":
            a, b = _ascii_lower(a), _ascii_lower(b)
        elif coll == "RTRIM":
            a, b = a.rstrip(" "), b.rstrip(" ")
        elif coll != "BINARY":
            return None
        a, b = a.encode(encoding, "surrogatepass"), b.encode(encoding, "surrogatepass")
    elif ra == 3:
        a, b = bytes(a), bytes(b)
    return (a > b) - (a < b)


def _ascii_lower(s):
    return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in s)


def index_templates(specs):
    return [IndexTemplate(s) for s in specs]


# -- live entries ------------------------------------------------------------------------------
class LiveIndexEntries(object):
    """The entries every live index holds now (hashed once per index, up to the limit
    live_index_entries: a larger index is listed in `capped`, its entries are not compared,
    and the carve result says so)."""

    def __init__(self, session, cap=None):
        from .. import limits
        self.session = session
        self.cap = limits.get(LIVE_ENTRY_CAP) if cap is None else cap
        self.cancel = None
        self.issues = IssueLog()
        self._reader = BTreeReader(session.pager, self.issues)
        self._sets = {}
        self._rows = {}
        self.capped = set()
        self._current = dict((e.name, (e.rootpage, e.tbl_name))
                             for e in session.schema.entries if e.type == "index")

    def _live_spec(self, tpl):
        """True when the index still exists as described (same root page, same table)."""
        spec = tpl.spec
        return not spec.dropped and \
            self._current.get(spec.name) == (spec.root_page, spec.table)

    def table_row(self, table, rowid):
        """{column index: stored value} of the live row, or None when it is not live (or the
        table cannot be read by rowid). The rowid alias column holds the rowid."""
        key = (table, rowid)
        if key in self._rows:
            return self._rows[key]
        info = self.session.schema.get(table)
        row = None
        if info is not None and info.kind == "table" and not info.without_rowid \
                and info.root_page:
            try:
                hit = self._reader.find_rowid(info.root_page, rowid)
            except Exception:
                hit = None
            if hit is not None:
                raw, _problem = decode_record_lenient(hit[1], self.session.pager.encoding)
                row = dict((cid, raw[pos]) for pos, cid in enumerate(info.storage_order)
                           if pos < len(raw))
                if info.rowid_alias is not None:
                    row[info.rowid_alias] = rowid
        if len(self._rows) > 100000:
            self._rows.clear()
        self._rows[key] = row
        return row

    def entries(self, tpl):
        """(set of value keys, {rowid: True}) of the live index, or None."""
        name = tpl.name
        if name in self._sets:
            return self._sets[name]
        keys, rowids = set(), {}
        entry = (keys, rowids)
        rp = tpl.rowid_pos
        enc = self.session.pager.encoding
        try:
            for n, (payload, _ref) in enumerate(self._reader.iter_index(tpl.spec.root_page)):
                if n % 1024 == 0 and self.cancel is not None and self.cancel():
                    return None             # not remembered: a later call retries
                if len(keys) >= self.cap:
                    self.capped.add(name)
                    entry = None
                    break
                values, _problem = decode_record_lenient(payload, enc)
                keys.add(tuple(value_key(v) for v in values))
                if rp is not None and rp < len(values) and isinstance(values[rp], int):
                    rowids[values[rp]] = True
        except Exception as e:
            self.issues.add("live_index_failed", str(e), name, "info")
            entry = None
        self._sets[name] = entry
        return entry

    def sample(self, tpl, count=256):
        """The first live entries of an index (value lists); may be short when a read
        error stops the scan early."""
        out = []
        if not self._live_spec(tpl):
            return out
        enc = self.session.pager.encoding
        try:
            for payload, _ref in self._reader.iter_index(tpl.spec.root_page):
                out.append(decode_record_lenient(payload, enc)[0])
                if len(out) >= count:
                    break
        except Exception:
            pass
        return out

    def status(self, tpl, values):
        """'live', 'prior_version' or None (see module docstring)."""
        if self._live_spec(tpl):
            entry = self.entries(tpl)
            if entry is not None:
                keys, rowids = entry
                if tuple(value_key(v) for v in values) in keys:
                    return "live"
                rp = tpl.rowid_pos
                if rp is not None and rp < len(values) and values[rp] in rowids \
                        and not tpl.spec.where:
                    return "prior_version"
                if not tpl.spec.where:
                    return None
        return self.table_status(tpl, values)

    def table_status(self, tpl, values):
        """Compare with the table row of the same rowid (dropped or partial indexes, or an
        index too large to hash): 'live' when every plain indexed column holds these values,
        'prior_version' when the row is live with other values, else None."""
        spec = tpl.spec
        rp = tpl.rowid_pos
        if rp is None or rp >= len(values) or not isinstance(values[rp], int):
            return None
        info = self.session.schema.get(spec.table)
        if info is None or info.root_page != spec.info.root_page:
            return None
        row = self.table_row(spec.table, values[rp])
        if row is None:
            return None
        return "live" if plain_match(spec, values, row) else "prior_version"


def plain_match(spec, values, row):
    """True when every plain (non-expression) indexed column of an entry holds the value the
    table row {column index: value} stores. False on the first difference."""
    for pos, col in enumerate(spec.columns):
        if col.cid is None or col.cid < 0 or pos >= len(values):
            continue
        stored = row.get(col.cid)
        if value_key(stored) != value_key(values[pos]):
            return False
    return True


def _same(a, b):
    if value_key(a) == value_key(b):
        return True
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and \
        not isinstance(a, bool) and not isinstance(b, bool) and float(a) == float(b)


def _content_match(spec, values, row):
    """plain_match() that also takes a REAL column's integral value read back as a float,
    and needs at least one plain column to compare."""
    compared = 0
    for pos, col in enumerate(spec.columns):
        if col.cid is None or col.cid < 0 or pos >= len(values) or col.cid not in row:
            continue
        if not _same(row[col.cid], values[pos]):
            return False
        compared += 1
    return compared > 0


def reference_rows(records, schema, specs=()):
    """{(table, rowid): {column index: value}} of recovered table records with a rowid (of
    the schema's tables, or the tables the IndexSpecs describe, e.g. dropped ones)."""
    infos = dict((s.table, s.info) for s in specs or ())
    out = {}
    for r in records:
        if r.index is not None or not r.table or r.rowid is None:
            continue
        info = schema.get(r.table) or infos.get(r.table)
        if info is None:
            continue
        names = [c.name for c in info.columns]
        if list(r.columns[:len(names)]) != names:
            continue
        out.setdefault((r.table, r.rowid), dict(enumerate(r.values[:len(names)])))
    return out


# -- the carver --------------------------------------------------------------------------------
class IndexCarver(Carver):
    """Carver for index b-tree pages only; see the module docstring."""

    def __init__(self, fx, templates, live, sources=None, cancel=None, progress=None,
                 deadline=None, max_records=None, page_hints=None, reference=None):
        """live: LiveIndexEntries; reference: reference_rows() of the table records carved
        before (used to tell apart indexes of the same shape on freed pages)."""
        kw = {} if max_records is None else {"max_records": max_records}
        Carver.__init__(self, fx, templates, sources, cancel, progress, deadline,
                        include_schema=False, unattributed=False, page_hints=page_hints, **kw)
        self.live_entries = live
        self.reference = reference or {}
        self.by_name = dict((t.name, t) for t in templates)
        self.stats.update({"index_pages": 0, "index_entries": 0})
        self._guessed = {}
        self._attributed = set()        # (file, page, frame) whose index was worked out
        self._bounds = None             # key range of the page being carved
        for t in templates:
            t.encoding = self.encoding
            t.row_lookup = self._row_for

    def _row_for(self, table, rowid):
        """The row a rowid points to: a recovered table record, else the live row."""
        row = self.reference.get((table, rowid))
        return row if row is not None else self.live_entries.table_row(table, rowid)

    # -- units ---------------------------------------------------------------------------------
    def _index_page(self, data, n):
        if not is_btree_page(data, n, self.usable):
            return False
        return data[100 if n == 1 else 0] in _INDEX_TYPES

    def _main_page(self, n, deferred):
        fx = self.fx
        if n == fx.lock_byte_page:
            return
        wal = fx.session.wal
        replaced = wal is not None and n in wal.overlay
        pmap = fx.main_map if replaced else fx.eff_map
        status = pmap.status(n) if n <= pmap.view.page_count else "beyond"
        if status in ("trunk", "ptrmap"):
            return
        if status == "tree":
            owner = pmap.owner[n]
            if owner[1] != "index" or owner[0] not in self.by_name:
                return
        if self.page_filter is not None and not self.page_filter("main", n, status):
            return
        data = fx.main.page(n)
        if not self._index_page(data, n):
            return
        self.stats["pages"] += 1
        self.stats["index_pages"] += 1
        src = self.sources
        if status == "tree":
            site = _Site(MAIN_FILE, n, pmap.owner[n][0], fx.main, pmap,
                         intact_source=REPLACED if replaced else None)
            self._btree(data, site, replaced and REPLACED in src)
        elif status == "freelist":
            if FREELIST not in src:
                return
            site = _Site(MAIN_FILE, n, self._owner(data, n, MAIN_FILE), fx.main, pmap,
                         FREELIST, FREELIST)
            self._btree(data, site, True)
        elif ORPHAN in src:
            site = _Site(MAIN_FILE, n, self._owner(data, n, MAIN_FILE), fx.main, pmap,
                         ORPHAN, ORPHAN)
            self._btree(data, site, True)

    def _page_owner(self, n, data, file, frame=None):
        fx = self.fx
        owner = fx.eff_map.owner.get(n) or fx.main_map.owner.get(n)
        if owner is not None and owner[1] == "index" and owner[0] in self.by_name:
            return owner[0]
        return self._owner(data, n, file, frame)

    def _wal_frame(self, index):
        fx = self.fx
        wal = fx.session.wal
        fr = wal.frames[index]
        n = fr.page_no
        if n < 1 or (self.page_filter is not None and not self.page_filter("wal", n, None)):
            return
        data = wal.page_data(index)
        if not self._index_page(data, n):
            return
        self.stats["frames"] += 1
        self.stats["index_pages"] += 1
        want_intact = fr.state != CURRENT and WAL in self.sources
        site = _Site(WAL_FILE, n, self._page_owner(n, data, WAL_FILE, index), None, None, intact_source=WAL,
                     frame=index, frame_state=fr.state, commit_group=fr.commit_group)
        if self._duplicate(data, want_intact, site):
            return
        site.view = AsOfFrameView(fx.main, fx.timeline, index)
        self._btree(data, site, want_intact)

    def _journal_record(self, journal, rec):
        fx = self.fx
        n = rec.page_no
        if self.page_filter is not None and not self.page_filter("journal", n, None):
            return
        data = journal.record_page(rec)
        if not self._index_page(data, n):
            return
        self.stats["journal_pages"] += 1
        self.stats["index_pages"] += 1
        want_intact = JOURNAL in self.sources
        site = _Site(JOURNAL_FILE, n, self._page_owner(n, data, JOURNAL_FILE), fx.journal_view(), None,
                     intact_source=JOURNAL)
        if self._duplicate(data, want_intact, site):
            return
        self._btree(data, site, want_intact)

    def _owner(self, data, n, file, frame=None):
        """The index a freed or unreferenced index page belonged to: the one every intact
        cell fits (a unique best fit covering at least half of them), else None."""
        guess = self._guess(data, n)
        if guess is not None:
            self._attributed.add((file, n, frame))
        return guess

    def _guess(self, data, n):
        key = hash(data)
        if key in self._guessed:
            return self._guessed[key]
        guess = None
        try:
            h = parse_page_header(data, n)
            interior = h.type == INDEX_INTERIOR
            fits, cells, entries = {}, 0, []
            for i in range(min(h.cell_count, 512)):
                off = (data[h.ptr_start + 2 * i] << 8) | data[h.ptr_start + 2 * i + 1]
                pos = off + 4 if interior else off
                if pos >= self.usable:
                    continue
                c = parse_intact(data, pos, self.usable, self.usable, True, self.max_cols)
                if c is None:
                    continue
                cells += 1
                fitting = [t for t in self.by_n.get((len(c.types), True), ())
                           if t.check_types(c.types)[0]]
                for t in fitting:
                    fits[t.name] = fits.get(t.name, 0) + 1
                if len(fitting) > 1 and c.ovfl is None and len(entries) < 32:
                    entries.append((fitting, decode_record_lenient(
                        bytes(data[c.pstart:c.pstart + c.payload_len]), self.encoding)[0]))
            if fits:
                best = max(fits.values())
                top = [name for name, k in fits.items() if k == best]
                if len(top) > 1 and best * 2 >= cells:
                    top = self._by_content(top, entries) or top
                if len(top) == 1 and best * 2 >= cells:
                    guess = top[0]
        except Exception:
            guess = None
        self._guessed[key] = guess
        return guess

    def _by_content(self, names, entries):
        """Of several indexes of the same shape, the one whose entries agree with the rows
        they point to (live rows, or table records recovered earlier): [name] or []."""
        score = dict((name, 0) for name in names)
        for fitting, values in entries:
            for t in fitting:
                if t.name not in score or t.rowid_pos is None or t.rowid_pos >= len(values):
                    continue
                rowid = values[t.rowid_pos]
                if not isinstance(rowid, int):
                    continue
                row = self.live_entries.table_row(t.table, rowid)
                if row is None:
                    row = self.reference.get((t.table, rowid))
                if row is None:
                    continue
                if _content_match(t.spec, values, row):
                    score[t.name] += 1
                else:
                    score[t.name] -= 1
        best = max(score.values())
        top = [name for name, k in score.items() if k == best]
        return top if best > 0 and len(top) == 1 else []

    def _btree(self, data, site, want_intact):
        h = parse_page_header(data, site.page)
        if h.type not in _INDEX_TYPES:
            return
        self._bounds = self._key_bounds(data, h, site)
        try:
            Carver._btree(self, data, site, want_intact)
        finally:
            self._bounds = None

    def _key_bounds(self, data, h, site):
        """(template, smallest, largest) key of the entries the page holds, or None. A cell
        freed on this page (a freeblock) once sat among them."""
        tpl = self.by_name.get(site.owner)
        if tpl is None or not h.cell_count:
            return None
        interior = h.type == INDEX_INTERIOR
        lo = hi = None
        try:
            for i in range(min(h.cell_count, 1024)):
                off = (data[h.ptr_start + 2 * i] << 8) | data[h.ptr_start + 2 * i + 1]
                pos = off + 4 if interior else off
                if pos >= self.usable:
                    continue
                c = parse_intact(data, pos, self.usable, self.usable, True, self.max_cols)
                if c is None or c.ovfl is not None or len(c.types) != tpl.n:
                    continue
                values = decode_record_lenient(bytes(data[c.pstart:c.pstart + c.payload_len]),
                                               self.encoding)[0]
                if lo is None:
                    lo = hi = values
                    continue
                a, b = tpl.compare(values, lo), tpl.compare(values, hi)
                if a is None or b is None:
                    return None
                if a < 0:
                    lo = values
                if b > 0:
                    hi = values
        except Exception:
            return None
        return (tpl, lo, hi) if lo is not None else None

    def _outside_page_keys(self, cand):
        """True when a rebuilt entry's key lies outside the keys its page holds."""
        b = self._bounds
        if b is None or b[0] is not cand.tpl or not cand.cell.rebuilt:
            return False
        tpl, lo, hi = b
        a, c = tpl.compare(cand.row, lo), tpl.compare(cand.row, hi)
        return a is not None and c is not None and (a < 0 or c > 0)

    def _area(self, data, start, end, head_clobbered, kinds, site, default_source=None,
              raw=False):
        kinds = tuple(k for k in kinds if k in (INDEX_KIND, IINT_KIND))
        if kinds:
            Carver._area(self, data, start, end, head_clobbered, kinds, site, default_source,
                         raw)

    # -- accepting --------------------------------------------------------------------------------
    def _accept(self, cand, site, source, data):
        cell = cand.cell
        if self._stop():
            return cell.end
        self.stats["candidates"] += 1
        key = (bytes(data[cell.start:cell.end]), site.owner, site.file) \
            if cell.ovfl is None and cell.rebuilt == 0 else None
        if cell.rebuilt and cand.atypical:
            # a rebuilt index entry is short: an unusual value means a chance fit
            if key is not None:
                self._seen_cells[key] = "skip"
            return cell.end
        settled = "ambiguous_table" in cand.flags and self._settle(cand)
        tpl = cand.tpl
        rp = tpl.rowid_pos
        if rp is not None and rp < len(cand.row) and isinstance(cand.row[rp], int):
            cand.rowid = cand.row[rp]
            if "ambiguous_reading" in cand.flags:
                row = self.reference.get((tpl.table, cand.rowid))
                if row is not None and _content_match(tpl.spec, cand.row, row):
                    cand.flags.discard("ambiguous_reading")
                    cand.flags.discard("uncertain_value")
                    cand.uncertain = []
                    cand.notes.append("this reading matches the recovered table record of "
                                      "row %s" % cand.rowid)
        live = self.live_entries.status(tpl, cand.row)
        if live == "live":
            self.stats["live_skipped"] += 1
            if key is not None:
                self._seen_cells[key] = "skip"
            return cell.end
        outside = self._outside_page_keys(cand)
        if live == "prior_version":
            if outside:
                # a live row, and a rebuilt key its page could not have held: a chance fit
                if key is not None:
                    self._seen_cells[key] = "skip"
                return cell.end
            cand.flags.add("prior_version")
            cand.notes.append("rowid %s is in the index now with other values: an older entry"
                              % cand.rowid)
        self._confidence(cand, site)
        if "uncertain_value" in cand.flags:
            cand.conf = LOW             # a guessed value in a short key: its neighbours shift
        if outside:
            cand.conf = LOW
            cand.flags.add("key_out_of_page_range")
            cand.notes.append("its key lies outside the keys its page holds")
        spec = tpl.spec
        cand.notes.insert(0, "entry of %s" % spec.describe())
        if site.owner is None and not settled:
            cand.conf = worst(cand.conf, MEDIUM)
            cand.notes.append("the page's index is not known; attributed by the record's shape")
        elif (site.file, site.page, site.frame) in self._attributed:
            cand.notes.append("page attributed to %s by its intact entries" % site.owner)
        if spec.dropped:
            cand.notes.append("the index was dropped")
        if key is not None:
            self._seen_cells[key] = cand
        self._emit(cand, source, site)
        return cell.end

    def _settle(self, cand):
        """An entry several indexes fit: keep the one whose plain columns agree with the row
        its rowid points to (live, or a recovered table record)."""
        fitting = [t for t in self.by_n.get((len(cand.cell.types), True), ())
                   if t.check_types(cand.cell.types)[0]]
        agree = []
        for t in fitting:
            rp = t.rowid_pos
            if rp is None or rp >= len(cand.row) or not isinstance(cand.row[rp], int):
                continue
            row = self.live_entries.table_row(t.table, cand.row[rp])
            if row is None:
                row = self.reference.get((t.table, cand.row[rp]))
            if row is not None and _content_match(t.spec, cand.row, row):
                agree.append(t)
        if len(agree) == 1:
            if agree[0] is not cand.tpl:
                cand.tpl = agree[0]
            cand.flags.discard("ambiguous_table")
            cand.notes = [n for n in cand.notes if not n.startswith("also fits: ")]
            cand.notes.append("attributed to %s: its values match row %s of %s" % (
                agree[0].name, cand.row[agree[0].rowid_pos], agree[0].table))
            return True
        return False

    def _emit(self, cand, source, site):
        tpl = cand.tpl
        spec = tpl.spec
        also = [n for note in cand.notes if note.startswith("also fits: ")
                for n in note[len("also fits: "):].split(", ")]
        flags = set(cand.flags)
        flags.add(INDEX_FLAG)
        rec = Record(spec.table, spec.names, cand.row, site.prov(source, cand.cell, cand.chain),
                     cand.conf, cand.notes, cand.rowid, flags=flags, index=spec.name)
        if also:
            tables = [spec.table]
            for name in also:
                t = self.by_name.get(name)
                if t is not None and t.table not in tables:
                    tables.append(t.table)
            rec.candidates = tables
        ident = ("index", spec.name, tuple(value_key(v) for v in rec.values))
        prev = self._identity.get(ident)
        if prev is not None:
            self._merge(prev, rec)
            site.records.append(prev)
            return
        self._identity[ident] = rec
        self.records.append(rec)
        self.stats["index_entries"] += 1
        site.records.append(rec)


def learn_index_lengths(templates, live, table_rows=None, reference=None):
    """Teach each template the value lengths, storage classes and integer ranges of its
    live entries (and of the table rows given as {table: [row in declared order]} for its
    plain columns), and the rowid range of its table (used to weigh rebuilt readings): the
    live table's, else that of the table records recovered before (reference_rows())."""
    bounds = {}
    for table, rowid in (reference or {}):
        lo, hi = bounds.get(("ref", table), (rowid, rowid))
        bounds[("ref", table)] = (min(lo, rowid), max(hi, rowid))
    for t in templates:
        try:
            t.learn_lengths(live.sample(t))
            for row in (table_rows or {}).get(t.table, ()):
                for pos, col in enumerate(t.spec.columns):
                    v = row[col.cid] if col.cid is not None and 0 <= col.cid < len(row) \
                        else None
                    if isinstance(v, (str, bytes)) and len(t.length_prior[pos]) < 64:
                        t.length_prior[pos].add(len(v.encode("utf-8") if isinstance(v, str)
                                                    else v))
            if t.rowid_pos is None:
                continue
            info = live.session.schema.get(t.table)
            if t.table not in bounds:
                live_bounds = None
                if info is not None and not info.without_rowid and info.root_page and \
                        info.root_page == t.spec.info.root_page:
                    live_bounds = rowid_bounds(live.session.pager, info.root_page)
                ref = bounds.get(("ref", t.table))
                if live_bounds is not None and ref is not None:
                    live_bounds = (min(live_bounds[0], ref[0]), max(live_bounds[1], ref[1]))
                bounds[t.table] = live_bounds or ref
            if bounds[t.table] is not None:
                t.int_range[t.rowid_pos] = bounds[t.table]
        except Exception:
            pass


def rowid_bounds(pager, root, max_depth=64):
    """(smallest, largest) rowid of a table b-tree, or None (empty or unreadable)."""
    out = []
    for last in (False, True):
        page_no = root
        for _ in range(max_depth):
            data = pager.page(page_no)
            h = parse_page_header(data, page_no)
            if h.type == 0x0D:
                if not h.cell_count:
                    return None
                i = h.cell_count - 1 if last else 0
                off = (data[h.ptr_start + 2 * i] << 8) | data[h.ptr_start + 2 * i + 1]
                _plen, p = read_varint(data, off)
                out.append(to_signed64(read_varint(data, p)[0]))
                break
            if h.type != 0x05:
                return None
            if last:
                page_no = h.right_child
            else:
                if not h.cell_count:
                    page_no = h.right_child
                else:
                    off = (data[h.ptr_start] << 8) | data[h.ptr_start + 1]
                    page_no = int.from_bytes(bytes(data[off:off + 4]), "big")
        else:
            return None
    return (min(out), max(out)) if len(out) == 2 else None


def link_records(records, session=None):
    """Note, on each recovered index entry, whether its row was also recovered as a table
    record (and whether the values agree), is live now, or left no other trace."""
    tables = {}
    for r in records:
        if r.index is None and r.table and r.rowid is not None:
            tables.setdefault((r.table, r.rowid), []).append(r)
    reader = BTreeReader(session.pager, IssueLog()) if session is not None else None
    live_cache = {}
    for r in records:
        if r.index is None or r.rowid is None:
            continue
        found = tables.get((r.table, r.rowid))
        if found:
            agree = [t for t in found if _agrees(r, t)]
            best = (agree or found)[0]
            r.flags.add("table_record_found")
            r.reasons.append("row %s of %s is also recovered as a table record (%s, %s)%s" % (
                r.rowid, r.table, best.source, best.prov.where(),
                "" if agree else "; the indexed values differ: another version of the row"))
            for t in found:
                note = "index %s also holds an entry for this rowid" % r.index
                if note not in t.reasons:
                    t.reasons.append(note)
            continue
        live = _row_live(session, reader, r.table, r.rowid, live_cache)
        if live:
            r.flags.add("row_live")
            r.reasons.append("row %s of %s is live now (with other indexed values)"
                             % (r.rowid, r.table))
        elif live is False:
            r.flags.add("row_gone")
            r.reasons.append("row %s of %s is gone and was not recovered elsewhere: this index "
                             "entry is what is left of it" % (r.rowid, r.table))


def _agrees(entry, rec):
    cols = dict(zip(rec.columns, rec.values))
    for c, v in zip(entry.columns[:-1], entry.values[:-1]):
        if c in cols and value_key(cols[c]) != value_key(v):
            if isinstance(v, int) and isinstance(cols[c], float) and float(v) == cols[c]:
                continue                # a REAL column's integral value read back as float
            return False
    return True


def _row_live(session, reader, table, rowid, cache):
    """True / False whether rowid is in the table now; None when that cannot be told."""
    if session is None or reader is None:
        return None
    key = (table, rowid)
    if key in cache:
        return cache[key]
    info = session.schema.get(table)
    result = None
    if info is not None and info.kind == "table" and not info.without_rowid and info.root_page:
        try:
            result = reader.find_rowid(info.root_page, rowid) is not None
        except Exception:
            result = None
    cache[key] = result
    return result
