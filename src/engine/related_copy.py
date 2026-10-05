"""Copy with related: rows, and every row a confident link leads to (no Tk here; datamap_ui.py
drives it). Built for tables of millions of rows, and for a case of several databases.

RelatedWalker follows the confident links of engine.relations (declared foreign keys, and links
whose values were found) from rows, `hops` links deep; in a case (CaseSpec) also the trusted
links between databases (engine.crossdb: always 'matched by value'), reading the rows of the
other database through its own Session. Every row says which database it comes from. The walk
works on chunks of CHUNK rows and looks the related rows up in batches, never one query per row:

  key     the target column is a key (rowid, INTEGER PRIMARY KEY, an indexed key column):
          one "WHERE key IN (...)" query per chunk
  seek    the target column has an index: one "IN (...)" query per chunk while the values lead
          to few rows each, else one index seek per distinct value reading at most
          related_rows_per_link + 1 rows; how many more there are is counted up to
          related_count_cap
  small   no index, but the table has at most related_small_table_rows rows: read once, kept
          as a value -> rows map
  scan    no index and at most related_scan_rows rows, for a selection of one chunk only: one
          "WHERE column IN (...)" pass over the table (said in the notes)
  native  a table read without SQLite: the relation map's in-memory or on-disk index
  skip    none of these (an unindexed table of millions of rows): the link is not followed,
          and the notes say so

Every cap is a named limit (engine.limits, changeable in settings.json), and whatever a limit
cuts off is said in the output, with the limit's name. A row reached again is named ("already
listed above"), not repeated, for up to related_seen_rows rows.

related_bundle(...) -> Bundle        in memory: a preview, or a small selection
export_related(..., out, fmt, ...)   streams any number of rows (Row objects, or Locators read
                                     in batches) to a text file object, chunk by chunk
Formats: 'markdown' (a section per row, small tables, the links followed), 'json' (nested:
{table, row, columns, values, related: [{via, table, rows}]}, with "database" in a case), 'sql'
(the CREATE statements of the tables involved, and per chunk a query returning exactly the rows
written: through the links' JOINs inside a database; rows reached by value in another database
are selected there by their keys). Dates are converted beside the raw value; BLOBs are
described (what they decode to, size, SHA-256) and given as hex only on request.
"""

import collections
import io
import json
import sqlite3
import threading
import time

from . import limits as lim
from . import timeline as tl
from .datamap import (Cancelled, Via, _check, _cname, _col_sql, _key_sql, _loc_json, _order_sql,
                      _visible, blob_info, convert_date, database_name, json_value, md_code_block,
                      md_escape, refuse_protected, sql_date, table_schema, utc_now_text)
from .export import TOOL_NAME
from .fileformat.record import InvalidText
from .relations import (NATIVE_INDEX_CAP, ROWID, Link as RelLink, Relation, _value_key, coerce,
                        relation_map, trivial)
from .schema import quote_ident
from .sqltext import comment_lines, create_table_sql, display_sql
from .session import is_interrupt

BUNDLE_FORMAT = "sqlite-gui-analyzer-related-rows"
CHUNK = 500                 # rows handled (and values looked up) together: a batch size only
KEY_BYTES = 256             # a BLOB longer than this is not a key: not followed
FORMATS = ("markdown", "json", "sql")
EXTENSIONS = {"markdown": ".md", "json": ".json", "sql": ".sql"}
LINKS_TEXT = "confident links only: declared foreign keys and links whose values were found"
LINKS_TEXT_CASE = LINKS_TEXT + "; between databases, links matched by value"


class TooLarge(Exception):
    """The text grew past the limit of a LimitedText."""


class LimitedText(object):
    """A text sink that refuses to grow past `limit` characters (for the clipboard)."""

    def __init__(self, limit=None):
        self.parts, self.size = [], 0
        self.limit = lim.get("clipboard_chars") if limit is None else limit

    def write(self, s):
        self.size += len(s)
        if self.size > self.limit:
            raise TooLarge()
        self.parts.append(s)

    def getvalue(self):
        return "".join(self.parts)


def check_options(fmt=None, hops=1, per_link=None, limits=None):
    """(hops, limits) after checking the options; ValueError says what is wrong."""
    L = lim.checked(limits) if limits is not None else lim.current()
    if fmt is not None and fmt not in FORMATS:
        raise ValueError("unknown format %r (use %s)" % (fmt, ", ".join(FORMATS)))
    if isinstance(hops, bool) or not isinstance(hops, int) or not 1 <= hops <= L["related_hops"]:
        raise ValueError("links to follow must be a whole number from 1 to %d (limit "
                         "related_hops), not %r" % (L["related_hops"], hops))
    if per_link is not None:
        if not lim.valid("related_rows_per_link", per_link):
            lo, hi = lim.RANGES["related_rows_per_link"]
            raise ValueError("rows per link must be a whole number from %d to %s, not %r"
                             % (lo, format(hi, ","), per_link))
        L["related_rows_per_link"] = per_link
    return hops, L


# -- the databases -----------------------------------------------------------------------------
class Database(object):
    """One database a walk reads: key (a case member's id; None for a database alone), name
    (shown in a case), its Session and relation map."""
    __slots__ = ("key", "name", "session", "relmap")

    def __init__(self, key, name, session, relmap=None):
        self.key, self.name, self.session = key, name or "", session
        self.relmap = relmap if relmap is not None else relation_map(session)


class CaseSpec(object):
    """The databases of a case, the one the rows start in (home: a key) and the links between
    the databases (engine.crossdb.CrossLink; only the trusted ones are followed)."""

    def __init__(self, home, databases, links=()):
        self.home = home
        self.databases = list(databases)
        self.links = [l for l in links if l.confident]
        if home not in [d.key for d in self.databases]:
            raise ValueError("the rows' database is not one of the case's databases")


def value_relation(link, key, table, column):
    """An engine Relation for a link between databases (CrossLink) seen from (key, table,
    column): its target is the other database's column; kind 'value'."""
    o_key, o_table, o_col = link.other_end(key, table, column)
    out = (link.src_db, link.src_table) == (key, table) and \
        link.src_col.lower() == column.lower()
    rel = Relation(table, column, o_table, o_col, "value", "out" if out else "in",
                   [link.reason()], [RelLink((table, column, o_table, o_col), "value",
                                             link.fraction)])
    rel.links[0].overlap = link.overlap
    return rel


# -- the walk --------------------------------------------------------------------------------
class RowNode(object):
    """One row (of the database `db`, a key) and the groups of rows related to it."""
    __slots__ = ("db", "table", "locator", "columns", "values", "flags", "hop", "related")

    def __init__(self, table, locator, columns, values, flags=(), hop=0, db=None):
        self.db, self.table, self.locator, self.columns = db, table, locator, columns
        self.values, self.flags, self.hop = list(values), set(flags or ()), hop
        self.related = []


class RelGroup(object):
    """The rows of one table a link leads to from one row. count: how many rows it leads to
    (at_least: at least that many; counting stopped at related_count_cap); rows: those kept
    (RowNodes); listed_rows: the locators of rows written for an earlier row (not repeated).
    db: the key of the database the rows are in."""
    __slots__ = ("via", "table", "column", "count", "at_least", "rows", "listed_rows", "db")

    def __init__(self, via, table, column, count, at_least, rows, listed_rows, db=None):
        self.via, self.table, self.column = via, table, column
        self.count, self.at_least, self.rows = count, at_least, rows
        self.listed_rows, self.db = listed_rows, db

    @property
    def listed(self):
        return len(self.listed_rows)

    @property
    def more(self):
        """Rows the link leads to that are neither kept nor already listed."""
        return max(0, self.count - len(self.rows) - self.listed)

    @property
    def capped(self):
        return self.more > 0 or self.at_least

    @property
    def exact(self):
        """True when the link's JOIN from the row returns exactly the kept rows."""
        return not self.listed and not self.capped

    def more_text(self, per_link):
        if not self.capped:
            return ""
        n = format(self.more, ",") + (" or more" if self.at_least else "")
        return "+%s more, not included: limit related_rows_per_link = %d" % (n, per_link)

    def listed_text(self):
        """'already listed above: urls row 5'."""
        if not self.listed_rows:
            return ""
        return "already listed above: %s row%s %s" % (
            self.table, "" if self.listed == 1 else "s",
            ", ".join(l.display() for l in self.listed_rows))


class LinkPlan(object):
    """How one confident link of a table is followed (see the module docstring). db: the key
    of the database the link leads to; heavy: a 'seek' link whose values lead to many rows
    each, so it is looked up value by value."""
    __slots__ = ("rel", "via", "table", "column", "src_column", "strategy", "note", "heavy",
                 "db")

    def __init__(self, rel, via, db, strategy, note=""):
        self.rel, self.via, self.db = rel, via, db
        self.table, self.column, self.src_column = rel.other, rel.other_column, rel.column
        self.strategy, self.note, self.heavy = strategy, note, False

    @property
    def cross(self):
        return self.via.cross


class RelatedWalker(object):
    """Follows the confident links from chunks of rows (see the module docstring)."""

    def __init__(self, session, relmap, hops=1, per_link=None, cancel=None, scan_ok=True,
                 limits=None, case=None):
        self.hops, self.limits = check_options(None, hops, per_link, limits)
        L = self.limits
        self.per_link = L["related_rows_per_link"]
        self.count_cap, self.seen_cap = L["related_count_cap"], L["related_seen_rows"]
        if case is None:
            case = CaseSpec(None, [Database(None, "", session, relmap)])
        self.case = case
        self.dbs = collections.OrderedDict((d.key, d) for d in case.databases)
        self.home = case.home
        self.multi = len(self.dbs) > 1
        self.session, self.relmap = self.dbs[self.home].session, self.dbs[self.home].relmap
        self.cancel, self.scan_ok = cancel, scan_ok
        self._plans, self._small, self._colidx, self._cols = {}, {}, {}, {}
        self._selects = {}          # (db, table) -> (info, "SELECT ... FROM t WHERE ", lead)
        self.seen, self.seen_full = set(), False
        self.notes, self._noted = [], set()
        self.missing = self.keyless = 0
        self.starts = self.related = self.groups = 0
        self.links = collections.OrderedDict()      # link key -> [Via, rows kept, groups, more]
        for d in self.dbs.values():
            d.relmap.build()

    def name(self, db):
        """The database's name in the output ('' for a database alone)."""
        return self.dbs[db].name if self.multi else ""

    def label(self, db, table):
        """'table', or in a case 'wa.db › table'."""
        return "%s › %s" % (self.dbs[db].name, table) if self.multi else table

    def note(self, text):
        if text and text not in self._noted:
            self._noted.add(text)
            self.notes.append(text)

    def final_notes(self):
        out = list(self.notes)
        if self.missing:
            out.append("%s selected row%s not found" % (format(self.missing, ","),
                                                        "" if self.missing == 1 else "s"))
        if self.keyless:
            out.append("%s row%s without a key (rowid or PRIMARY KEY) left out"
                       % (format(self.keyless, ","), "" if self.keyless == 1 else "s"))
        if self.seen_full:
            out.append("more than %s rows: rows met again after that are written again "
                       "(limit related_seen_rows)" % format(self.seen_cap, ","))
        return out

    def remember(self, db, table, loc):
        """True for a row not met before (it is remembered, up to related_seen_rows rows)."""
        key = (db, table, loc)
        if key in self.seen:
            return False
        if len(self.seen) < self.seen_cap:
            self.seen.add(key)
        else:
            self.seen_full = True
        return True

    def columns(self, db, table):
        k = (db, table)
        hit = self._cols.get(k)
        if hit is None:
            hit = self._cols[k] = list(self.dbs[db].session.visible_columns(table))
            self._colidx[k] = dict((c, i) for i, c in enumerate(hit))
        return hit

    def _index(self, db, table):
        self.columns(db, table)
        return self._colidx[(db, table)]

    # -- plans ---------------------------------------------------------------------------------
    def plan(self, db, table):
        """The LinkPlans of a table: one per confident relation of each of its columns, and in
        a case one per trusted link to another database."""
        hit = self._plans.get((db, table))
        if hit is not None:
            return hit
        plans = []
        d = self.dbs[db]
        rm = d.relmap
        if table in rm.tables:
            cols = list(self.columns(db, table))
            if rm.key(table) is ROWID:
                cols.append(ROWID)
            for c in cols:
                _check(self.cancel)
                try:
                    rels = rm.confident(table, c, self.cancel)
                except KeyError:
                    continue
                if rels is None:
                    raise Cancelled()
                for rel in rels:
                    # inside one database: the rows' headings name the database
                    plans.append(self._strategy(rel, Via(rel), db))
            for l in self.case.links:
                for c in cols:
                    if c is ROWID or not l.touches(db, table, c):
                        continue
                    o_key = l.other_end(db, table, c)[0]
                    if o_key not in self.dbs or o_key == db:
                        continue
                    rel = value_relation(l, db, table, c)
                    via = Via(rel, self.dbs[db].name, self.dbs[o_key].name)
                    plans.append(self._strategy(rel, via, o_key))
        self._plans[(db, table)] = plans
        return plans

    def link_count(self, table, db=None):
        db = self.home if db is None else db
        return sum(1 for p in self.plan(db, table) if p.strategy != "skip")

    def tables(self, table, db=None):
        """[(db, table)]: the start table and the tables its links lead to, hops deep (in the
        order met)."""
        start = (self.home if db is None else db, table)
        out, frontier = [start], [start]
        for _hop in range(self.hops):
            nxt = []
            for d, t in frontier:
                for p in self.plan(d, t):
                    k = (p.db, p.table)
                    if p.strategy != "skip" and k not in out:
                        out.append(k)
                        nxt.append(k)
            frontier = nxt
        return out

    def _rows_of(self, d, table):
        rm = d.relmap
        n = rm.known_rows(table)
        return rm.table_rows(table) if n is None else n

    def _strategy(self, rel, via, db):
        """The LinkPlan of a relation whose rows are in database db (a key)."""
        L = self.limits
        d = self.dbs[db]
        s, rm = d.session, d.relmap
        u, dcol = rel.other, rel.other_column
        what = via.text().split(", ")[0]
        src = s.source(u)
        if src == "unavailable":
            return LinkPlan(rel, via, db, "skip", "%s not followed: %s cannot be read"
                            % (what, self.label(db, u)))
        if rm.is_rowid(u, dcol):
            return LinkPlan(rel, via, db, "key" if src == "sql" else "native")
        if src != "sql":
            n = self._rows_of(d, u)
            if (n is not None and n <= lim.get(NATIVE_INDEX_CAP)) or \
                    rm._disk_index.get(u, {}).get(dcol.lower()):
                return LinkPlan(rel, via, db, "native")
            return LinkPlan(rel, via, db, "skip", "%s not followed: %s is read natively, has "
                            "no usable index on %s and %s rows (more than the limit %s)"
                            % (what, self.label(db, u), dcol, _n(n), NATIVE_INDEX_CAP))
        if rm.indexed(u, dcol):
            key = rm.key(u)
            if isinstance(key, str) and key.lower() == dcol.lower():
                return LinkPlan(rel, via, db, "key")
            return LinkPlan(rel, via, db, "seek")
        n = self._rows_of(d, u)
        if n is not None and n <= L["related_small_table_rows"]:
            return LinkPlan(rel, via, db, "small")
        if self.scan_ok and n is not None and n <= L["related_scan_rows"]:
            return LinkPlan(rel, via, db, "scan", "%s: %s.%s has no index, so %s (%s rows) is "
                            "read once" % (what, self.label(db, u), dcol, u, _n(n)))
        return LinkPlan(rel, via, db, "skip", "%s not followed: %s.%s has no index and %s has "
                        "%s rows, more than the limits related_small_table_rows (%s) and, for "
                        "more than %d rows, related_scan_rows (%s)"
                        % (what, self.label(db, u), dcol, u, _n(n),
                           _n(L["related_small_table_rows"]), CHUNK,
                           _n(L["related_scan_rows"])))

    # -- reading rows --------------------------------------------------------------------------
    def _rows_sql(self, db, table, where, params, chunk_size=2000):
        s = self.dbs[db].session
        hit = self._selects.get((db, table))
        if hit is None:
            info = s.info(table)
            select, lead = s._select(info)
            hit = self._selects[(db, table)] = (info, "SELECT %s FROM %s WHERE " % (
                select, quote_ident(table)), lead)
        info, head, lead = hit
        return s._sql_rows(info, lead, head + where, params, chunk_size)

    def start_nodes(self, table, items):
        """RowNodes for a chunk of start rows (of the home database): Row objects as they are,
        Locators read here (rowids in batches). Rows without a key, rows not found and rows
        already met are left out (and counted)."""
        db = self.home
        s = self.dbs[db].session
        cols = self.columns(db, table)
        got = {}
        rowids = [x.value for x in items if not hasattr(x, "values")
                  and getattr(x, "kind", None) == "rowid" and isinstance(x.value, int)]
        if rowids and s.source(table) == "sql":
            name = s.info(table).rowid_name
            try:
                for i in range(0, len(rowids), CHUNK):
                    part = rowids[i:i + CHUNK]
                    rows = self._rows_sql(db, table, "%s IN (%s)" % (name, ",".join(
                        "?" * len(part))), part)
                    try:
                        for r in rows:
                            got[r.locator] = r
                    finally:
                        rows.close()
            except sqlite3.Error as e:
                if is_interrupt(e):
                    raise Cancelled()
                got = {}
        out = []
        for i, x in enumerate(items):
            if i % 100 == 0:
                _check(self.cancel)
            if hasattr(x, "values"):
                row = x
            else:
                if getattr(x, "kind", None) not in ("rowid", "pk"):
                    self.keyless += 1
                    continue
                row = got.get(x)
                if row is None:
                    row = s.row(table, x)
                if row is None:
                    self.missing += 1
                    continue
            if getattr(row.locator, "kind", None) not in ("rowid", "pk"):
                self.keyless += 1
                continue
            if not self.remember(db, table, row.locator):
                continue
            out.append(RowNode(table, row.locator, cols, row.values, row.flags, 0, db))
        self.starts += len(out)
        return out

    def walk(self, roots):
        """Fill the related groups of these rows (and of their related rows, hops deep)."""
        frontier = roots
        for hop in range(1, self.hops + 1):
            by_table = collections.OrderedDict()
            for n in frontier:
                by_table.setdefault((n.db, n.table), []).append(n)
            nxt = []
            for (db, table), nodes in by_table.items():
                for i in range(0, len(nodes), CHUNK):
                    self._expand(db, table, nodes[i:i + CHUNK], hop)
                for n in nodes:
                    for g in n.related:
                        nxt.extend(g.rows)
            frontier = nxt

    def _expand(self, db, table, nodes, hop):
        K = self.per_link
        idx = self._index(db, table)
        for p in self.plan(db, table):
            if p.strategy == "skip":
                self.note(p.note)
                continue
            _check(self.cancel)
            trm = self.dbs[p.db].relmap
            aff = trm.affinity(p.table, p.column)
            rowid_target = trm.is_rowid(p.table, p.column)
            keys, wanted = [], {}
            for n in nodes:
                if p.src_column is ROWID:
                    v = n.locator.value if n.locator.kind == "rowid" else None
                else:
                    i = idx.get(p.src_column)
                    v = n.values[i] if i is not None and i < len(n.values) else None
                if trivial(v) or isinstance(v, InvalidText) or \
                        (isinstance(v, (bytes, bytearray)) and len(v) > KEY_BYTES):
                    keys.append(None)
                    continue
                cv = coerce(v, aff)
                if rowid_target and (not isinstance(cv, int) or isinstance(cv, bool)):
                    keys.append(None)       # only an integer can equal a rowid
                    continue
                k = _value_key(cv)
                keys.append(k)
                wanted[k] = cv
            if not wanted:
                continue
            if p.note:
                self.note(p.note)
            found = self._fetch(p, wanted)
            cols = self.columns(p.db, p.table)
            for n, k in zip(nodes, keys):
                hit = found.get(k) if k is not None else None
                if not hit:
                    continue
                rows, count, at_least = hit
                kept, listed = [], []
                for r in rows[:K]:
                    if getattr(r.locator, "kind", None) not in ("rowid", "pk") or \
                            not self.remember(p.db, p.table, r.locator):
                        listed.append(r.locator)
                        continue
                    kept.append(RowNode(p.table, r.locator, cols, r.values, r.flags, hop, p.db))
                g = RelGroup(p.via, p.table, p.column, count, at_least, kept, listed, p.db)
                n.related.append(g)
                self.groups += 1
                self.related += len(kept)
                lk = (db, p.via.table, _cname(p.via.column), p.db, p.table, _cname(p.column))
                ent = self.links.get(lk)
                if ent is None:
                    ent = self.links[lk] = [p.via, 0, 0, 0]
                ent[1] += len(kept)
                ent[2] += 1
                ent[3] += g.more

    # -- lookups -------------------------------------------------------------------------------
    def _fetch(self, p, wanted):
        """{value key: (rows, count, at_least)} of the rows of p.table (in database p.db)
        whose column holds one of the wanted values (at most related_rows_per_link + 1 rows
        each)."""
        try:
            if p.strategy in ("key", "scan"):
                return self._fetch_in(p, wanted)
            if p.strategy == "seek":
                return self._fetch_seek(p, wanted)
            if p.strategy == "small":
                return self._fetch_small(p, wanted)
        except sqlite3.Error as e:
            if is_interrupt(e):
                raise Cancelled()
            self.note("%s: SQLite failed (%s); read natively" % (self.label(p.db, p.table), e))
        return self._fetch_native(p, wanted)

    def _target_value(self, p, row, ci):
        if p.column is ROWID:
            return row.locator.value
        return row.values[ci] if ci is not None and ci < len(row.values) else None

    def _fetch_in(self, p, wanted, budget=None):
        """One 'IN (...)' query per CHUNK values. budget: give up (None) once more rows than
        this come back (a one-to-many link whose values lead to many rows each)."""
        K = self.per_link
        s = self.dbs[p.db].session
        u, d = p.table, p.column
        col = s.info(u).rowid_name if d is ROWID else quote_ident(d)
        ci = None if d is ROWID else self._index(p.db, u).get(d)
        items = list(wanted.items())
        out = {}
        n = 0
        for i in range(0, len(items), CHUNK):
            part = items[i:i + CHUNK]
            _check(self.cancel)
            rows = self._rows_sql(p.db, u, "%s IN (%s)" % (col, ",".join("?" * len(part))),
                                  [v for _k, v in part])
            try:
                for row in rows:
                    n += 1
                    if n % 2000 == 0:
                        _check(self.cancel)
                    if budget is not None and n > budget:
                        return None
                    k = _value_key(self._target_value(p, row, ci))
                    ent = out.get(k)
                    if ent is None:
                        ent = out[k] = [[], 0]
                    ent[1] += 1
                    if len(ent[0]) <= K:
                        ent[0].append(row)
            finally:
                rows.close()        # the cursor is closed now, on this thread
        cap = self.count_cap
        return dict((k, (e[0], min(e[1], cap), e[1] > cap)) for k, e in out.items()
                    if k in wanted)

    def _fetch_seek(self, p, wanted):
        K = self.per_link
        if not p.heavy:
            # most one-to-many links lead to a few rows per value: one query for the chunk,
            # given up for value-by-value seeks once it reads far more rows than it keeps
            got = self._fetch_in(p, wanted, budget=4 * (K + 1) * len(wanted))
            if got is not None:
                return got
            p.heavy = True
        s = self.dbs[p.db].session
        u, d = p.table, p.column
        ci = self._index(p.db, u).get(d)
        q = quote_ident(d)
        count_sql = "SELECT count(*) FROM (SELECT 1 FROM %s WHERE %s = ? LIMIT %d)" % (
            quote_ident(u), q, self.count_cap + 1)
        out = {}
        for n, (k, v) in enumerate(wanted.items()):
            if n % 50 == 0:
                _check(self.cancel)
            rows = [r for r in self._rows_sql(p.db, u, "%s = ? LIMIT %d" % (q, K + 1), [v],
                                              K + 1)
                    if _value_key(self._target_value(p, r, ci)) == k]
            if not rows:
                continue
            if len(rows) > K:
                c = s.conn().execute(count_sql, [v]).fetchone()[0]
                out[k] = (rows, min(c, self.count_cap), c > self.count_cap)
            else:
                out[k] = (rows, len(rows), False)
        return out

    def _fetch_small(self, p, wanted):
        K = self.per_link
        s = self.dbs[p.db].session
        u, d = p.table, p.column
        key = (p.db, u, d)
        index = self._small.get(key)
        if index is None:
            index = {}
            ci = self._index(p.db, u).get(d)
            for i, row in enumerate(s.iter_rows(u)):
                if i % 2000 == 0:
                    _check(self.cancel)
                k = _value_key(self._target_value(p, row, ci))
                if k is not None:
                    index.setdefault(k, []).append(row.locator)
            self._small[key] = index
        out = {}
        for k in wanted:
            locs = index.get(k)
            if not locs:
                continue
            rows = [r for r in (s.row(u, loc, scan=False) for loc in locs[:K + 1])
                    if r is not None]
            if rows:
                out[k] = (rows, len(locs), False)
        return out

    def _fetch_native(self, p, wanted):
        out = {}
        rm = self.dbs[p.db].relmap
        for n, (k, v) in enumerate(wanted.items()):
            if n % 20 == 0:
                _check(self.cancel)
            res = rm.rows_for(p.rel, v, self.per_link + 1, self.cancel)
            if res.count and res.rows:
                out[k] = (list(res.rows), res.count, False)
        return out


def _n(v):
    return "?" if v is None else format(v, ",")


# -- the context every writer uses -------------------------------------------------------------
class DateCol(object):
    __slots__ = ("table", "column", "kind", "confidence", "reason", "text_numbers")

    def __init__(self, tc):
        self.table, self.column, self.kind = tc.table, tc.column, tc.effective_kind
        self.confidence, self.reason, self.text_numbers = tc.confidence, tc.reason, \
            tc.text_numbers

    def as_dict(self):
        return {"table": self.table, "column": self.column, "kind": self.kind,
                "label": tl.LABELS.get(self.kind, self.kind), "confidence": self.confidence,
                "reason": self.reason}


_DATES_LOCK = threading.Lock()


def date_columns(session, tables, cancel=None):
    """{(table, column): DateCol} of the detected date columns of these tables; each table is
    sampled once per session (kept with the session)."""
    with _DATES_LOCK:
        cache = session.__dict__.setdefault("_related_dates", {})
        todo = [t for t in tables if t not in cache]
    if todo:
        det = tl.detect(session, tables=todo, cancel=cancel)
        if det.cancelled:
            raise Cancelled()
        found = dict((t, []) for t in todo)
        for tc in det.detected():
            found.setdefault(tc.table, []).append(DateCol(tc))
        with _DATES_LOCK:
            cache.update(found)
    out = {}
    with _DATES_LOCK:
        for t in tables:
            for dc in cache.get(t, ()):
                out[(dc.table, dc.column)] = dc
    return out


class Context(object):
    """What the writers need besides the rows: the schema of every table the links can reach
    (keyed (database key, table)), their date columns (keyed (database key, table, column)),
    the database names in a case, the options and limits."""

    def __init__(self, session, relmap, walker, table, include_hex=False, title="",
                 tool_version=None):
        self.walker = walker
        self.multi = walker.multi
        home = walker.dbs[walker.home]
        self.database = database_name(home.session)
        self.databases = [d.name for d in walker.dbs.values()] if self.multi else []
        self.tool = "%s %s" % (TOOL_NAME, tool_version) if tool_version else TOOL_NAME
        # (database, main file path, its SHA-256 or None while it is being computed)
        self.evidence = []
        for d in walker.dbs.values():
            ev = getattr(d.session, "evidence", None)
            fp = ev.fingerprints.get("main") if ev is not None else None
            if fp is not None:
                self.evidence.append((d.name if self.multi else self.database, fp.path,
                                      fp.sha256))
        self.table, self.title = table, title or table
        self.hops, self.per_link = walker.hops, walker.per_link
        self.limits = walker.limits
        self.cell_chars = walker.limits["markdown_cell_chars"]
        self.include_hex = bool(include_hex)
        self.generated = utc_now_text()
        self.schemas = collections.OrderedDict()
        for db, t in walker.tables(table):
            d = walker.dbs[db]
            self.schemas[(db, t)] = table_schema(d.session, d.relmap, t)
        self.dates = {}
        by_db = collections.OrderedDict()
        for db, t in self.schemas:
            by_db.setdefault(db, []).append(t)
        for db, tables in by_db.items():
            for (t, c), dc in date_columns(walker.dbs[db].session, tables,
                                           walker.cancel).items():
                self.dates[(db, t, c)] = dc

    def evidence_text(self):
        """'x.db SHA-256 <hex>' per database ('still being computed' when not known yet: the
        export's manifest has it)."""
        return "; ".join("%s SHA-256 %s" % (name, sha or "still being computed (the export's "
                                                        "manifest has it)")
                         for name, _p, sha in self.evidence)

    def name(self, db):
        return self.walker.name(db)

    def label(self, db, table):
        return self.walker.label(db, table)

    def schema(self, db, table):
        return self.schemas[(db, table)]

    def date_of(self, db, table, column, value):
        dc = self.dates.get((db, table, column))
        if dc is None or value is None:
            return None
        text = convert_date(value, dc.kind, dc.text_numbers)
        return (dc.kind, text) if text else None


def node_dict(ctx, n):
    """A row and its related rows as JSON data."""
    values, dates, blobs = [], {}, {}
    for c, v in zip(n.columns, n.values):
        values.append(json_value(v))
        if isinstance(v, (bytes, bytearray)) and not isinstance(v, InvalidText):
            blobs[c] = blob_info(v, ctx.include_hex)
        d = ctx.date_of(n.db, n.table, c, v)
        if d:
            dates[c] = {"kind": d[0], "utc": d[1]}
    out = {}
    if ctx.multi:
        out["database"] = ctx.name(n.db)
    out.update({"table": n.table, "row": n.locator.display(), "locator": _loc_json(n.locator),
                "columns": n.columns, "values": values})
    if dates:
        out["dates"] = dates
    if blobs:
        out["blobs"] = blobs
    if n.flags:
        out["flags"] = sorted(n.flags)
    rel = []
    for g in n.related:
        d = {"via": g.via.as_dict()}
        if ctx.multi:
            d["database"] = ctx.name(g.db)
        d.update({"table": g.table, "column": _cname(g.column), "count": g.count,
                  "count_is_minimum": g.at_least, "shown": len(g.rows),
                  "already_listed": [l.display() for l in g.listed_rows], "more": g.more,
                  "rows": [node_dict(ctx, r) for r in g.rows]})
        rel.append(d)
    out["related"] = rel
    return out


# -- writers -----------------------------------------------------------------------------------
class _Writer(object):
    def __init__(self, out, ctx):
        self.out, self.ctx = out, ctx
        self.rows = 0

    def begin(self):
        pass

    def chunk(self, nodes):
        pass

    def end(self, walker, complete):
        pass


def cell_text(ctx, node, column, v, limit=None):
    """One value of a row in a line: the raw value, with the date or BLOB description beside."""
    limit = ctx.cell_chars if limit is None else limit
    if v is None:
        return "NULL"
    if isinstance(v, InvalidText):
        return "invalid text, hex %s" % bytes(v).hex()
    if isinstance(v, (bytes, bytearray)):
        info = blob_info(v, ctx.include_hex)
        text = "BLOB %s bytes: %s" % (format(info["size"], ","), info["decodes_to"])
        if ctx.include_hex:
            text += "; hex %s" % info["hex"]
        return text
    s = v if isinstance(v, str) else (repr(v) if isinstance(v, float) else str(v))
    if limit and len(s) > limit:
        s = "%s… (%s more characters, not shown: limit markdown_cell_chars)" % (
            s[:limit], format(len(s) - limit, ","))
    d = ctx.date_of(node.db, node.table, column, v)
    if d:
        s += " (UTC %s, %s)" % (d[1], tl.SHORT.get(d[0], d[0]))
    return s


class MarkdownWriter(_Writer):
    def begin(self):
        c = self.ctx
        where = ("Case of %d databases (%s), rows starting in `%s`" % (
            len(c.databases), ", ".join(c.databases), c.database.replace("`", "'"))
            if c.multi else "Database `%s`" % c.database.replace("`", "'"))
        self.out.write("\n".join([
            "# Copy with related: %s" % md_escape(c.title), "",
            "%s · %d link%s deep · at most %d related rows per link and row "
            "(limit related_rows_per_link) · %s"
            % (where, c.hops, "" if c.hops == 1 else "s", c.per_link, c.generated), "",
            "Confident links only: declared foreign keys and links whose values were found%s. "
            "Dates are shown converted beside the raw value; BLOBs by what they decode to%s."
            % ("; between databases, links matched by value" if c.multi else "",
               " and their bytes as hex" if c.include_hex else ""), "",
            "Made with %s, read-only · evidence: %s" % (md_escape(c.tool),
                                                       md_escape(c.evidence_text())), "", ""]))

    def _table(self, nodes):
        cols = nodes[0].columns
        lines = ["| row | %s |" % " | ".join(md_escape(c) for c in cols),
                 "|%s|" % "|".join(["---"] * (len(cols) + 1))]
        for n in nodes:
            lines.append("| %s | %s |" % (md_escape(n.locator.display()), " | ".join(
                md_escape(cell_text(self.ctx, n, c, v)) for c, v in zip(n.columns, n.values))))
        return lines

    def chunk(self, nodes):
        for n in nodes:
            lines = ["## %s row %s" % (md_escape(self.ctx.label(n.db, n.table)),
                                       md_escape(n.locator.display())), ""]
            lines += self._table([n]) + [""]
            if not n.related:
                lines += ["No related rows.", ""]
            self._related(n, lines, 3)
            self.out.write("\n".join(lines) + "\n")
            self.rows += 1

    def _related(self, node, lines, level):
        for g in node.related:
            lines += ["%s %s (%d of %s%s row%s), via %s" % (
                "#" * min(level, 6), md_escape(self.ctx.label(g.db, g.table)), len(g.rows),
                format(g.count, ","), "+" if g.at_least else "", "" if g.count == 1 else "s",
                md_escape(g.via.text())), ""]
            if g.rows:
                lines += self._table(g.rows) + [""]
            extra = []
            if g.listed:
                extra.append(md_escape(g.listed_text()))
            if g.capped:
                extra.append(g.more_text(self.ctx.per_link))
            lines += ["(%s)" % "; ".join(extra), ""] if extra else []
            for r in g.rows:
                if r.related:
                    self._related(r, lines, level + 1)

    def end(self, walker, complete):
        c = self.ctx
        lines = []
        if not complete:
            lines += ["**Stopped after %s row%s: the rest was not written.**" % (
                format(self.rows, ","), "" if self.rows == 1 else "s"), ""]
        lines += ["## Links followed", ""]
        if walker.links:
            for via, kept, groups, more in walker.links.values():
                lines.append("- %s: %s row%s from %s row%s%s" % (
                    md_escape(via.text()), format(kept, ","), "" if kept == 1 else "s",
                    format(groups, ","), "" if groups == 1 else "s",
                    ("; %s more not included (limit related_rows_per_link = %d)"
                     % (format(more, ","), c.per_link)) if more else ""))
        else:
            lines.append("No related rows: no confident link leads from these rows to a row of "
                         "another table.")
        lines += ["", "## Schema", ""]
        for (db, t), s in c.schemas.items():
            lines += ["### %s" % md_escape(c.label(db, t)), ""] + md_code_block(
                display_sql(s["sql"]) or "-- no CREATE statement") + [""]
        if c.dates:
            lines += ["## Date columns", ""]
            for (db, _t, _c), d in c.dates.items():
                lines.append("- %s.%s: %s (%s confidence) - %s" % (
                    md_escape(c.label(db, d.table)), md_escape(d.column),
                    tl.LABELS.get(d.kind, d.kind), d.confidence, md_escape(d.reason)))
            lines.append("")
        notes = walker.final_notes()
        if notes:
            lines += ["## Notes", ""] + ["- " + md_escape(n) for n in notes] + [""]
        self.out.write("\n".join(lines))


class JsonWriter(_Writer):
    def begin(self):
        c = self.ctx
        d = {"format": BUNDLE_FORMAT, "database": c.database, "generated_utc": c.generated,
             "tool": c.tool, "evidence": [{"database": n, "path": p, "sha256": s}
                                          for n, p, s in c.evidence],
             "hops": c.hops, "rows_per_link": c.per_link, "blob_hex": c.include_hex,
             "links": LINKS_TEXT_CASE if c.multi else LINKS_TEXT}
        if c.multi:
            d["databases"] = c.databases
        head = json.dumps(d, ensure_ascii=False)
        self.out.write(head[:-1] + ', "rows": [')

    def chunk(self, nodes):
        for n in nodes:
            self.out.write(("\n" if not self.rows else ",\n") +
                           json.dumps(node_dict(self.ctx, n), ensure_ascii=False))
            self.rows += 1

    def end(self, walker, complete):
        c = self.ctx
        schema = collections.OrderedDict()
        for (db, t), s in c.schemas.items():
            schema[c.label(db, t)] = dict(s, database=c.name(db)) if c.multi else s
        dates = []
        for (db, _t, _c), d in c.dates.items():
            dd = d.as_dict()
            if c.multi:
                dd["database"] = c.name(db)
            dates.append(dd)
        tail = {"schema": schema, "date_columns": dates,
                "links_followed": [dict(v.as_dict(), rows=kept, from_rows=groups, more=more)
                                   for v, kept, groups, more in walker.links.values()],
                "limits": dict((k, v) for k, v in c.limits.items() if k.startswith("related")),
                "notes": walker.final_notes(), "complete": complete, "rows_written": self.rows}
        text = json.dumps(tail, ensure_ascii=False, indent=1)
        self.out.write("\n],\n" + text[1:])


def select_list(ctx, db, table, alias):
    s = ctx.schema(db, table)
    cols = _visible(s)
    parts = ["%s.%s" % (alias, quote_ident(c)) for c in cols]
    for c in cols:
        dc = ctx.dates.get((db, table, c))
        if dc is not None:
            parts.append("%s AS %s" % (sql_date("%s.%s" % (alias, quote_ident(c)), dc.kind),
                                       quote_ident(c + " (UTC)")))
    return ", ".join(parts)


def _key_cols(schema, alias):
    if schema["rowid"]:
        return ["%s.%s" % (alias, schema["rowid"])]
    return ["%s.%s" % (alias, quote_ident(c)) for c in schema["primary_key"]]


def chunk_queries(ctx, nodes):
    """[(title, sql, rows, database)] for a chunk of start rows: the rows' own SELECT, and per
    link path a query returning exactly the related rows written for these rows. Inside one
    database their keys are found through the links' JOINs from the rows (pinned to the rows
    kept when a link was capped or a row was already listed); rows reached by value in another
    database are selected there by their keys. database: the name of the database the query
    runs in ('' alone)."""
    if not nodes:
        return []
    out = []
    by_table = collections.OrderedDict()
    for n in nodes:
        by_table.setdefault((n.db, n.table), []).append(n)
    for (db, table), ns in by_table.items():
        s = ctx.schema(db, table)
        out.append(("the %d row%s of %s" % (len(ns), "" if len(ns) == 1 else "s",
                                           ctx.label(db, table)),
                    "SELECT %s\nFROM %s AS x\nWHERE %s\nORDER BY %s;" % (
                        select_list(ctx, db, table, "x"), quote_ident(table),
                        _key_sql(s, "x", [n.locator for n in ns]), _order_sql(s, "x")), ns,
                    ctx.name(db)))
    paths = collections.OrderedDict()

    def visit(node, chain, groups, key):
        for g in node.related:
            k = key + ((node.db, g.via.table, _cname(g.via.column), g.db, g.table,
                        _cname(g.column)),)
            ent = paths.get(k)
            level = chain + [node]
            if ent is None:
                ent = paths[k] = {"nodes": [(a.db, a.table) for a in level] + [(g.db, g.table)],
                                  "vias": [x.via for x in groups] + [g.via],
                                  "pins": [collections.OrderedDict() for _a in level],
                                  "rows": [], "exact": True}
            for i, a in enumerate(level):
                ent["pins"][i][a.locator] = True
            ent["rows"].extend(g.rows)
            ent["exact"] = ent["exact"] and g.exact
            for r in g.rows:
                visit(r, level, groups + [g], k)
    for n in nodes:
        visit(n, [], [], ())
    for ent in paths.values():
        if not ent["rows"]:
            continue                # every row of the link was written for an earlier row
        steps, vias = ent["nodes"], ent["vias"]
        leaf_db, leaf = steps[-1]
        ls = ctx.schema(leaf_db, leaf)
        title = " → ".join(ctx.label(d, t) for d, t in steps) + ": " + \
            "; ".join(v.text() for v in vias)
        # the part of the path inside the leaf's database: after its last link between
        # databases (a JOIN cannot cross two files)
        first = 0
        for i, via in enumerate(vias):
            if via.cross:
                first = i + 1
        keys = _key_cols(ls, "x")
        if first == len(vias):
            # the rows were reached by value from another database: selected by their keys
            cond = _key_sql(ls, "x", [r.locator for r in ent["rows"]])
            title += " (matched by value: the rows are selected by their keys in %s)" % (
                ctx.name(leaf_db) or "the database")
        else:
            part = steps[first:]
            last = "t%d" % (len(part) - 1)
            joins, where = [], []
            for i, via in enumerate(vias[first:]):
                a, b = "t%d" % i, "t%d" % (i + 1)
                (da, ta), (dbk, tb) = part[i], part[i + 1]
                joins.append("JOIN %s AS %s ON %s = %s" % (
                    quote_ident(tb), b, _col_sql(ctx.schema(dbk, tb), b, via.other_column),
                    _col_sql(ctx.schema(da, ta), a, via.column)))
            for i, pins in enumerate(ent["pins"][first:]):
                d_, t_ = part[i]
                where.append(_key_sql(ctx.schema(d_, t_), "t%d" % i, list(pins)))
            inner = "SELECT %s\n    FROM %s AS t0\n    %s\n    WHERE %s" % (
                ", ".join(_key_cols(ls, last)), quote_ident(part[0][1]), "\n    ".join(joins),
                "\n      AND ".join(where))
            cond = "%s IN (\n    %s)" % (keys[0] if len(keys) == 1 else
                                         "(%s)" % ", ".join(keys), inner)
            if not ent["exact"]:
                cond += "\n  AND %s" % _key_sql(ls, "x", [r.locator for r in ent["rows"]])
        sql = "SELECT %s\nFROM %s AS x\nWHERE %s\nORDER BY %s;" % (
            select_list(ctx, leaf_db, leaf, "x"), quote_ident(leaf), cond, _order_sql(ls, "x"))
        out.append((title, sql, ent["rows"], ctx.name(leaf_db)))
    return out


def _one_line(text):
    """text on one line (any line break becomes a space), for a '-- ' comment."""
    return " ".join(str(text).splitlines())


class SqlWriter(_Writer):
    def begin(self):
        c = self.ctx
        lines = comment_lines("Copy with related: %s" % _one_line(c.title))
        lines += comment_lines("%s, %s. Run against a copy of the database%s, never the "
                               "evidence." % (
                                   ("Case of %s" % ", ".join(c.databases)) if c.multi else
                                   "Database %s" % c.database, c.generated,
                                   "s" if c.multi else ""))
        lines += ["-- Each query returns exactly the rows written for it; date columns are "
                  "converted in the", "-- extra '(UTC)' columns.",
                  "-- The CREATE TABLE statements are rebuilt from the parsed columns (names, "
                  "declared types, NOT NULL,", "-- primary key, WITHOUT ROWID); the stored "
                  "statements and the indexes are in the Markdown and", "-- JSON copies and "
                  "the Database Map."]
        lines += comment_lines("Made with %s, read-only; evidence: %s" % (
            c.tool, _one_line(c.evidence_text())))
        if c.multi:
            lines.append("-- In a case each query runs in the database named above it.")
        last = object()
        for (db, t), s in c.schemas.items():
            if db != last:
                lines += [""] + comment_lines(
                    "Tables involved" + (" in %s" % _one_line(c.name(db)) if c.multi else ""))
                last = db
            sql, notes = create_table_sql(s, t)
            lines += [n for note in notes for n in comment_lines(_one_line(note))]
            if sql is None:
                lines += comment_lines("Stored statement of %s (not rebuilt):" % _one_line(t))
                lines += comment_lines(s["sql"] or "(none)", "--   ")
            else:
                lines.append(sql)
        self.out.write("\n".join(lines) + "\n\n")

    def chunk(self, nodes):
        for title, sql, _rows, db in chunk_queries(self.ctx, nodes):
            head = comment_lines(_one_line(title))
            if self.ctx.multi:
                head += comment_lines("in %s" % _one_line(db))
            self.out.write("%s\n%s\n\n" % ("\n".join(head), sql))
        self.rows += len(nodes)

    def end(self, walker, complete):
        lines = []
        if not complete:
            lines.append("-- Stopped after %s rows: the rest was not written."
                         % format(self.rows, ","))
        for via, _kept, _groups, more in walker.links.values():
            if more:
                lines += comment_lines("%s: %s more rows not included (limit "
                                       "related_rows_per_link = %d)" % (
                                           _one_line(via.text()), format(more, ","),
                                           self.ctx.per_link))
        lines += [ln for n in walker.final_notes() for ln in comment_lines(_one_line(n))]
        self.out.write("\n".join(lines) + ("\n" if lines else ""))


WRITERS = {"markdown": MarkdownWriter, "json": JsonWriter, "sql": SqlWriter}


# -- entry points ------------------------------------------------------------------------------
def title_for(table, locators, total=None):
    locs = [l.display() for l in locators[:3]]
    n = total if total is not None else len(locators)
    if n == 1 and locs:
        return "%s row %s" % (table, locs[0])
    if locs and n <= 3:
        return "%s rows %s" % (table, ", ".join(locs))
    return "%s, %s rows" % (table, format(n, ",") if n is not None else "all filtered")


class Bundle(object):
    """related_bundle()'s result: the start rows (roots) with their related rows. schemas and
    dates are those of the start database, keyed by table and (table, column)."""

    def __init__(self, ctx, walker, roots, seconds):
        self.ctx, self.walker, self.roots, self.seconds = ctx, walker, roots, seconds
        self.table, self.database = ctx.table, ctx.database
        home = walker.home
        self.schemas = collections.OrderedDict((t, s) for (db, t), s in ctx.schemas.items()
                                               if db == home)
        self.dates = dict(((t, c), dc) for (db, t, c), dc in ctx.dates.items() if db == home)
        self.hops, self.limit = ctx.hops, ctx.per_link
        self.include_hex = ctx.include_hex
        self.rows = len(walker.seen)
        self.notes = walker.final_notes()

    def tables(self):
        """The tables the rows come from, in the order met ('db › table' for another
        database of a case)."""
        out = []

        def add(db, t):
            name = t if db == self.walker.home else self.walker.label(db, t)
            if name not in out:
                out.append(name)
        for n in self.roots:
            add(n.db, n.table)
        for _p, g in self.groups():
            if g.rows:
                add(g.db, g.table)
        return out

    def groups(self):
        """[(path, group)]: every RelGroup with the rows leading to it ([root, g1, row, ...])."""
        out = []

        def walk(node, path):
            for g in node.related:
                out.append((path + [node], g))
                for r in g.rows:
                    walk(r, path + [node, g])
        for root in self.roots:
            walk(root, [])
        return out

    def date_of(self, table, column, value, db=None):
        return self.ctx.date_of(self.walker.home if db is None else db, table, column, value)

    def render(self, fmt, out=None):
        """The bundle as text ('markdown', 'json' or 'sql'), or written to `out`."""
        check_options(fmt)
        sink = out if out is not None else io.StringIO()
        w = WRITERS[fmt](sink, self.ctx)
        w.begin()
        for i in range(0, len(self.roots), CHUNK):
            w.chunk(self.roots[i:i + CHUNK])
        w.end(self.walker, True)
        return None if out is not None else sink.getvalue()


def related_bundle(session, relmap, table, rows, hops=1, per_link=None, include_hex=False,
                   cancel=None, total=None, limits=None, case=None, tool_version=None):
    """The Bundle, in memory, of these start rows of `table` (Row objects or Locators; use it
    for a preview or a small selection). case: a CaseSpec (the rows are in its home database;
    session and relmap may then be None). tool_version: named with the tool in the output.
    Raises Cancelled when cancel() stops it, ValueError for a bad option."""
    t0 = time.perf_counter()
    rows = list(rows)
    n = total if total is not None else len(rows)
    walker = RelatedWalker(session, relmap, hops, per_link, cancel, scan_ok=n <= CHUNK,
                           limits=limits, case=case)
    try:
        locs = [getattr(r, "locator", r) for r in rows]
        ctx = Context(session, relmap, walker, table, include_hex, title_for(table, locs, n),
                      tool_version)
        roots = []
        for i in range(0, len(rows), CHUNK):
            nodes = walker.start_nodes(table, rows[i:i + CHUNK])
            walker.walk(nodes)
            roots.extend(nodes)
    except sqlite3.Error as e:
        if is_interrupt(e):
            raise Cancelled()
        raise
    return Bundle(ctx, walker, roots, time.perf_counter() - t0)


def render_bundle(b, fmt):
    return b.render(fmt)


class ExportResult(object):
    def __init__(self):
        self.starts = self.related = self.groups = 0
        self.complete, self.seconds, self.notes = False, 0.0, []


def export_related(session, relmap, table, rows, out, fmt, hops=1, per_link=None,
                   include_hex=False, cancel=None, progress=None, total=None, title=None,
                   limits=None, case=None, tool_version=None):
    """Write the start rows (an iterable of Row objects or Locators, read chunk by chunk) and
    their related rows to the text file object `out` in `fmt`, holding one chunk at a time.
    progress(rows done, total) after each chunk. Stopping (cancel) after the output began ends
    it properly, saying where it stopped; before that, Cancelled is raised. case: see
    related_bundle(). Returns an ExportResult; a LimitedText sink raises TooLarge through it;
    ValueError for a bad option."""
    check_options(fmt, hops, per_link, limits)
    t0 = time.perf_counter()
    res = ExportResult()
    walker = RelatedWalker(session, relmap, hops, per_link, cancel,
                           scan_ok=total is not None and total <= CHUNK, limits=limits,
                           case=case)
    try:
        ctx = Context(session, relmap, walker, table, include_hex, title or title_for(
            table, [], total), tool_version)
    except sqlite3.Error as e:
        if is_interrupt(e):
            raise Cancelled()
        raise
    w = WRITERS[fmt](out, ctx)
    w.begin()
    done = 0
    complete = False
    it = iter(rows)
    try:
        while True:
            _check(cancel)
            chunk = []
            for x in it:
                chunk.append(x)
                if len(chunk) >= CHUNK:
                    break
            if not chunk:
                complete = True
                break
            nodes = walker.start_nodes(table, chunk)
            walker.walk(nodes)
            w.chunk(nodes)
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    except Cancelled:
        pass
    except sqlite3.Error as e:
        if not is_interrupt(e):
            raise
    finally:
        close = getattr(it, "close", None)
        if close is not None:
            try:
                close()             # a generator over an SQL cursor: let it go now
            except Exception:       # noqa: BLE001 - the connection may be closed already
                pass
    w.end(walker, complete)
    res.starts, res.related, res.groups = walker.starts, walker.related, walker.groups
    res.complete, res.notes = complete, walker.final_notes()
    res.seconds = time.perf_counter() - t0
    return res


def export_related_file(session, relmap, table, rows, path, fmt, is_protected=None, **kw):
    """export_related() into a UTF-8 file; refuses (ValueError, nothing written) a path
    is_protected() rejects."""
    refuse_protected(path, is_protected)
    check_options(fmt, kw.get("hops", 1), kw.get("per_link"), kw.get("limits"))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        return export_related(session, relmap, table, rows, f, fmt, **kw)
