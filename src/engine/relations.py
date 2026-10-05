"""Column relationships: where else does a column's value live?

For a column C of table T the map lists the columns D of other tables U that hold the same kind
of value, each with a score (0..1) and the reasons for it:

  declared FOREIGN KEY        C REFERENCES U(D), or U.D REFERENCES T(C)            1.0
  name                        <x>_row_id, <x>_id, <x>id -> table x (plural or      0.8
                              singular) on its key: INTEGER PRIMARY KEY, a column
                              _id / id, a one-column PRIMARY KEY, or the rowid.
                              A table name's common prefix is left out: moz_,
                              tbl_, tb_, t_, and one most tables of the database
                              share (zen_, ZZ): place_id -> moz_places
  name (last words)           sender_jid_row_id -> table jid                       0.65
  parent                      parent, parent_id, parentid, parent_row_id -> the    0.8
                              key of its own table (a self-reference), unless a
                              table parent(s) exists
  fk                          a column named fk -> the key of the table its        0.65
                              values fit best, among the tables other columns
                              refer to (Firefox moz_bookmarks.fk -> moz_places.id)
  Core Data (Apple)           an INTEGER column ZFOO -> table ZFOO's key Z_PK;     0.8
                              ZROOTFOLDER -> ZFOLDER (last word; an indexed        0.65
                              column, as Core Data indexes relationships)
                              Z_ENT -> Z_PRIMARYKEY.Z_ENT; in a many-to-many       0.8
                              join table Z_1NOTES -> ZNOTE, Z_4TAGS -> ZTAG
  same column name            an id-like name in another table                     0.6
                              (any other name: 0.3)
  same target                 both columns name the same key (message_row_id and   0.85 x the
                              parent_message_row_id both -> message._id)           weaker link
  types                       a number column against a TEXT column                x 0.6

A column whose name exactly names its own table (node.node_id) is that table's own identifier,
not a reference to itself; a self-reference comes from the parent rule or the last words of a
name (parent_node_id in table node).

The directions are 'out' (C refers to U.D), 'in' (U.D refers to C, which is T's key) and
'peer' (both hold the same kind of value). verify() samples up to SAMPLE distinct non-NULL
values on the referring side of each link and measures the fraction f the referred column
holds (a 'same target' relation checks both columns' links to that key). The checked score is
score + 0.7 x w x (f - score): 0.3 x score + 0.7 x f for a full sample, moving less (w < 1)
when the column has fewer than LOW_CARDINALITY distinct values, and halved when those are a
few small integers (a 0/1 flag or a status code). A declared FOREIGN KEY keeps at least 0.7.
A relation scores as its weakest link.

A link found by a name is trusted (is_confident) only when at least relations_min_name_values
distinct values were found (engine.limits, 3 by default); a same-name link only with
relations_min_same_name_values (10 by default). With fewer it is kept as a weaker link and its
reason says so ('weaker: too few values (2 distinct values match; 3 needed)').

related_rows(T, C, value) reads, for every related column, the rows whose D equals the value
(converted to D's type affinity as SQLite would store it). It goes through Session.lookup(), so
SQL-served and natively read tables give the same rows.

Everything reads the evidence through the Session. Declared keys and indexes come from the
schema, replayed in a scratch in-memory database (never the evidence): the map works the same
when SQLite cannot open the file.
"""

import collections
import re
import sqlite3
import threading
import time

from .filters import Expr, _number, real_text, value_expr
from . import sqlsafe, uiyield
from .limits import get as limit
from .fileformat.btree import BTreeReader
from .fileformat.record import InvalidText, decode_record_lenient
from .session import is_interrupt
from .schema import Locator, column_affinity, quote_ident, register_collations, \
    rename_create_table, replace_collations

FK_SCORE = 1.0
NAME_SCORE = 0.8
NAME_TAIL_SCORE = 0.65
SAME_NAME_SCORE = 0.6
SAME_NAME_WEAK_SCORE = 0.3
SHARED_FACTOR = 0.85
TYPE_MISMATCH_FACTOR = 0.6
MIN_SCORE = 0.5             # related_rows() default: weaker links are left out
SAMPLE = 200                # distinct values verify() checks
LOW_CARDINALITY = 10        # fewer distinct values than this: weak evidence
# engine.limits names (Limits…, settings.json); a cut says which one:
SAMPLE_SCAN_CAP = "relations_sample_scan_rows"      # rows read at most to find the sample
TARGET_SCAN_CAP = "relations_target_scan_rows"      # rows of an unindexed target column read
NATIVE_INDEX_CAP = "relations_native_index_rows"    # rows of a natively read table indexed
#                                                     at most (a larger one is scanned)
CHEAP_SCAN_ROWS = "relations_cheap_scan_rows"       # an unindexed table this small is counted
#                                                     at once for a menu
KEY_SETS = "relations_key_sets"                     # value sets of unindexed columns kept
KEY_SET_CAP = "relations_key_set_values"            # and the most values one of them keeps
ROW_LIMIT = 200             # rows kept per related column by related_rows()
CONFIDENT_OVERLAP = 0.5     # a name link is trusted when this share of its values is found
SAME_NAME_GROUP = 12        # links() pairs same-named columns of groups this small

ROWID = None                # the column name of a table's rowid when no column aliases it

MIN_NAME_VALUES = "relations_min_name_values"          # engine.limits names
MIN_SAME_NAME_VALUES = "relations_min_same_name_values"

_SUFFIXES = ("_row_id", "_rowid", "_id", "id")
_GENERIC = frozenset(("id", "_id", "rowid", "_rowid_", "oid", "docid", "key", "value", "type",
                      "name", "data", "uuid", "guid", "hash", "z_pk", "z_ent", "z_opt"))
_ID_LIKE_RE = re.compile(r"(?:^|_)(?:row_?id|id|uuid|guid|jid|key|hash)$|[a-z]id$")
_TABLE_PREFIXES = ("moz_", "tbl_", "tb_", "t_")     # always left out of a table name
_WORD_PREFIX_RE = re.compile(r"[a-z]{1,4}_")        # zen_, wa_: a prefix most tables may share
_PARENT_NAMES = frozenset(("parent", "parent_id", "parentid", "parent_row_id", "parent_rowid"))
_CORE_DATA_COLUMN_RE = re.compile(r"Z([A-Z][A-Z0-9]*?)\d*$")        # ZFOLDER, ZFOLDER1
_CORE_DATA_JOIN_RE = re.compile(r"Z_?\d+([A-Z][A-Z0-9]*)$")         # Z_1NOTES, Z3TAGS


def display_column(table_info, column):
    """How a related column is written: its name, or the rowid alias for ROWID."""
    if column is not ROWID:
        return column
    return table_info.rowid_name or "rowid"


def _class(affinity):
    if affinity in ("INTEGER", "REAL", "NUMERIC"):
        return "number"
    if affinity == "TEXT":
        return "text"
    return "any"


def coerce(value, affinity):
    """The value as a column of this type affinity would store it (text that reads as a number
    becomes a number in a numeric column, a number becomes text in a TEXT column)."""
    if value is None or isinstance(value, bytes):
        return value
    if isinstance(value, bool):
        value = int(value)
    cls = _class(affinity)
    if cls == "number" and isinstance(value, str):
        n = _number(value.strip())
        if n is None:
            return value
        if affinity == "REAL":
            return float(n)
        if isinstance(n, float) and n.is_integer() and abs(n) < 2 ** 63:
            return int(n)
        return n
    if cls == "number" and affinity == "REAL" and isinstance(value, int):
        return float(value)
    if cls == "text" and isinstance(value, (int, float)):
        return real_text(value) if isinstance(value, float) else str(value)
    return value


def equals_expr(value):
    """A filters.Expr selecting exactly this value ('=' comparison), or None for NULL and
    invalid text (which no comparison can select)."""
    if value is None or isinstance(value, InvalidText):
        return None
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, bytes):
        operand = ("blob", bytes(value))
    elif isinstance(value, (int, float)):
        if value != value:
            return None
        operand = ("num", value)
    else:
        operand = ("text", str(value))
    return Expr("cmp", value_expr(value) or "=<value>", op="=", operand=operand)


def _value_key(v):
    """Equality key for a stored value, as an '=' filter compares (5 equals 5.0)."""
    if v is None:
        return None
    if isinstance(v, InvalidText):
        return ("x", bytes(v))
    if isinstance(v, bytes):
        return ("b", v)
    if isinstance(v, (int, float)):
        return None if v != v else ("n", v)
    return ("t", v)


def name_base(column):
    """'message' for message_row_id / message_id / messageid, else None."""
    n = column.lower()
    for suf in _SUFFIXES:
        if n.endswith(suf):
            base = n[:-len(suf)].strip("_")
            if suf == "id" and n[:-2].endswith("_"):
                continue
            if len(base) >= 2:
                return base
    return None


def _forms(stem):
    out = [stem, stem + "s", stem + "es"]
    if stem.endswith("s"):
        out.append(stem[:-1])
    if stem.endswith("y"):
        out.append(stem[:-1] + "ies")
    if stem.endswith("ies"):
        out.append(stem[:-3] + "y")
    return out


def id_like(column):
    n = column.lower()
    return n not in _GENERIC and bool(_ID_LIKE_RE.search(n))


def table_prefixes(names):
    """The prefixes left out of table names (lower case, longest first) when a column name is
    matched to a table: moz_, tbl_, tb_, t_, and any prefix more than half of the tables share
    (at least two): a short word and '_' (zen_, wa_), or the first one or two letters of
    upper-case names (ZZNOTE, ZZFOLDER)."""
    out = set(_TABLE_PREFIXES)
    counts = collections.Counter()
    seen = set()
    for name in names:
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        cands = set()
        m = _WORD_PREFIX_RE.match(low)
        if m:
            cands.add(m.group(0))
        if name.isupper():
            cands.update(low[:k] for k in (1, 2) if len(low) > k + 2)
        counts.update(cands)
    for p, n in counts.items():
        if n >= 2 and 2 * n > len(seen):
            out.add(p)
    return sorted(out, key=lambda p: (-len(p), p))


def min_values(kind):
    """Distinct values a checked link of this kind must have found to be trusted: the limits
    relations_min_name_values ('name') and relations_min_same_name_values ('same_name');
    0 for a declared key (and for links between databases, which have their own limit)."""
    if kind == "same_name":
        return limit(MIN_SAME_NAME_VALUES)
    if kind == "name":
        return limit(MIN_NAME_VALUES)
    return 0


def too_few(link):
    """'weaker: too few values (2 distinct values match; 3 needed)' for a checked link that
    found fewer distinct values than min_values() asks, else None."""
    ov = link.overlap
    if ov is None or ov.error or not ov.sampled:
        return None
    need = min_values(link.kind)
    if ov.found >= need:
        return None
    return "weaker: too few values (%d distinct value%s match; %d needed)" % (
        ov.found, "" if ov.found == 1 else "s", need)


def trivial(value):
    """NULL, 0 and empty values: they are in too many places to relate anything."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value == 0
    return len(value) == 0


def _operand_kind(v):
    if isinstance(v, bytes) and not isinstance(v, InvalidText):
        return "blob"
    return "num" if isinstance(v, (int, float)) else "text"


def is_confident(rel):
    """A relation to trust: a declared FOREIGN KEY, or links whose values were checked and
    found (at least CONFIDENT_OVERLAP of the sample and at least min_values() distinct values,
    not a flag-like column) with a score of at least MIN_SCORE."""
    if rel.kind == "fk":
        return True
    if rel.score < MIN_SCORE:
        return False
    for link in rel.links:
        ov = link.overlap
        if link.kind == "fk":
            continue
        if ov is None or ov.error or not ov.sampled or ov.flag_like or \
                ov.fraction < CONFIDENT_OVERLAP or ov.found < min_values(link.kind):
            return False
    return True


def plain_reason(rel):
    """Why a relation holds, in plain words: 'declared foreign key', 'same values found in
    97% of samples', with the name rule it came from."""
    parts = []
    if rel.kind == "fk" or any(l.kind == "fk" for l in rel.links):
        parts.append("declared foreign key")
    checked = [l.overlap for l in rel.links if l.overlap is not None and l.overlap.sampled
               and not l.overlap.error]
    if checked:
        pct = min(int(round(100 * ov.fraction)) for ov in checked)
        parts.append("same values found in %d%% of samples" % pct)
    if rel.kind == "name":
        col = rel.column if rel.direction == "out" else rel.other_column
        target = rel.other if rel.direction == "out" else rel.table
        parts.append("name %s → %s%s" % ("rowid" if col is ROWID else col, target,
                                               " (same table)" if rel.table == rel.other
                                               else ""))
    elif rel.kind == "shared" and rel.via is not None:
        parts.append("both refer to %s" % rel.via[0])
    elif rel.kind == "same_name":
        parts.append("same column name")
    few = [t for t in (too_few(l) for l in rel.links) if t]
    if few:
        parts.append(few[0])
    return "; ".join(parts)


class Overlap(object):
    """check()'s result for one (referring column, referred column) pair.

    exhausted: the column has no other distinct values than those sampled; flag_like: they
    are fewer than LOW_CARDINALITY small integers (-1..9), as in a 0/1 flag or a status code.
    """
    __slots__ = ("found", "sampled", "exhausted", "flag_like", "capped", "seconds", "error",
                 "sample_capped")

    def __init__(self, found, sampled, exhausted, flag_like=False, capped=False, seconds=0.0,
                 error=None, sample_capped=False):
        self.found, self.sampled, self.exhausted = found, sampled, exhausted
        self.flag_like, self.capped, self.seconds, self.error = flag_like, capped, seconds, error
        self.sample_capped = sample_capped      # the sample stopped at SAMPLE_SCAN_CAP rows

    @property
    def fraction(self):
        return float(self.found) / self.sampled if self.sampled else 0.0

    @property
    def weight(self):
        """How much the fraction counts: 1, less for a column with few distinct values."""
        if self.exhausted and self.sampled < LOW_CARDINALITY:
            return float(self.sampled) / LOW_CARDINALITY
        return 1.0

    def text(self):
        if self.error:
            return "could not check the values: %s" % self.error
        if not self.sampled:
            return "no non-NULL values to check"
        s = "%d%% of %d sampled values found" % (round(100.0 * self.fraction), self.sampled)
        if self.flag_like:
            s += " (only %d distinct small number%s: looks like a flag or status code)" % (
                self.sampled, "" if self.sampled == 1 else "s")
        elif self.weight < 1:
            s += " (only %d distinct value%s: weak evidence)" % (
                self.sampled, "" if self.sampled == 1 else "s")
        if self.capped:
            s += " (the other column was read up to %s rows: limit %s)" % (
                format(limit(TARGET_SCAN_CAP), ","), TARGET_SCAN_CAP)
        if self.sample_capped:
            s += " (values sampled from the first %s rows: limit %s)" % (
                format(limit(SAMPLE_SCAN_CAP), ","), SAMPLE_SCAN_CAP)
        return s


def _flag_like(values, exhausted):
    return exhausted and 0 < len(values) < LOW_CARDINALITY and all(
        isinstance(v, int) and not isinstance(v, bool) and -1 <= v <= 9 for v in values)


class Link(object):
    """One reference verify() can check: values of from_column should occur in to_column.
    check = (from_table, from_column, to_table, to_column).

    Checked, the score moves from the name score towards the fraction of values found (70 %
    of the way for a full sample, less for few distinct values) and is halved for a flag-like
    column; a declared FOREIGN KEY keeps at least 0.7."""
    __slots__ = ("check", "kind", "base", "overlap")

    def __init__(self, check, kind, base):
        self.check, self.kind, self.base = check, kind, base
        self.overlap = None

    @property
    def score(self):
        ov = self.overlap
        if ov is None or ov.error or not ov.sampled:
            return self.base
        f, w = ov.fraction, ov.weight
        if self.kind == "fk":
            return 1.0 - 0.3 * w * (1.0 - f)
        score = self.base + 0.7 * w * (f - self.base)
        return score * 0.5 if ov.flag_like else score


class Relation(object):
    """One related column, seen from the queried column (table, column).

    kind: 'fk' | 'name' | 'same_name' | 'shared'; direction: 'out' | 'in' | 'peer'.
    links: the references the relation rests on (one; two for 'shared': each column's
    reference to the key `via`); the score is `factor` x the weakest link's score.
    """
    __slots__ = ("table", "column", "other", "other_column", "kind", "direction", "reasons",
                 "links", "factor", "via")

    def __init__(self, table, column, other, other_column, kind, direction, reasons, links,
                 factor=1.0, via=None):
        self.table, self.column, self.other, self.other_column = table, column, other, other_column
        self.kind, self.direction = kind, direction
        self.reasons, self.links, self.factor, self.via = list(reasons), links, factor, via

    @property
    def base(self):
        """The score before any value check."""
        return round(self.factor * min(l.base for l in self.links), 3)

    @property
    def score(self):
        return round(self.factor * min(l.score for l in self.links), 3)

    @property
    def verified(self):
        return all(l.overlap is not None for l in self.links)

    def why(self):
        """Every reason, then the value checks."""
        out = list(self.reasons)
        for l in self.links:
            if l.overlap is not None:
                t, c, u, d = l.check
                prefix = ("%s.%s → %s.%s: " % (t, c if c is not ROWID else "rowid", u,
                                                   d if d is not ROWID else "rowid")) \
                    if len(self.links) > 1 else ""
                few = too_few(l)
                out.append(prefix + l.overlap.text() + ("; " + few if few else ""))
        return out

    def arrow(self):
        return {"out": "→", "in": "←", "peer": "="}[self.direction]

    def __repr__(self):
        return "Relation(%s.%s %s %s.%s %s %.2f)" % (
            self.table, self.column if self.column is not ROWID else "rowid", self.arrow(),
            self.other, self.other_column if self.other_column is not ROWID else "rowid",
            self.kind, self.score)


class RelatedResult(object):
    """related_rows()' answer for one related column: `count` rows of `table` hold the value in
    `column`; `rows` are the first of them (engine Row objects, in `columns` order)."""
    __slots__ = ("relation", "table", "column", "count", "rows", "columns", "source", "note",
                 "value", "seconds")

    def __init__(self, relation, count, rows, columns, source, note="", value=None, seconds=0.0):
        self.relation = relation
        self.table, self.column = relation.other, relation.other_column
        self.count, self.rows, self.columns = count, rows, columns
        self.source, self.note, self.value, self.seconds = source, note, value, seconds


_LOCK = threading.Lock()


def relation_map(session):
    """The session's RelationMap (one per session, built on first use)."""
    with _LOCK:
        rm = session.__dict__.get("_relations")
        if rm is None:
            rm = session.__dict__["_relations"] = RelationMap(session)
        return rm


class RelationMap(object):
    def __init__(self, session):
        self.session = session
        self._lock = threading.RLock()
        self._built = False
        self.build_seconds = None
        self.tables = []
        self._by_name = {}      # lower table name -> table
        self._alias = {}        # lower table name without its prefix -> (table, prefix)
        self._core_data = False  # the database is an Apple Core Data store (Z_PRIMARYKEY)
        self._cols = {}         # table -> {lower column name: column name}
        self._keys = {}         # table -> key column name, ROWID, or False (none usable)
        self._aff = {}          # (table, lower column) -> affinity
        self._out = {}          # (table, lower column) -> [(U, D, kind, base, reasons)]
        self._in = {}           # (U, lower D) -> [(T, C, kind, base, reasons)]
        self._same = {}         # lower column name -> [(table, column)]
        self._indexed = {}      # table -> lower names of the first column of each index
        self._overlap = {}      # check tuple -> Overlap
        self._native_idx = {}   # (table, lower column) -> _native_index() of a native table
        self._disk_index = {}   # table -> {lower column: root page of an ascending, full index
        #                         whose first column it is}
        self._links = None      # links(), once built
        self._key_sets = collections.OrderedDict()  # (table, lower column) -> (value keys,
        #                         capped) of an unindexed column read by check()
        self._confident = {}    # (table, lower column) -> confident relations
        self._row_counts = {}   # table -> row count (None: cannot be counted)
        self.mapped = False     # map_links() has checked every link
        self.problems = []      # (table, why) the schema replay could not use

    # -- building ----------------------------------------------------------------------------
    def build(self):
        """Index every table's columns, keys, declared foreign keys and name links (cheap:
        the schema only). Safe to call again; runs once."""
        with self._lock:
            if self._built:
                return self
            t0 = time.perf_counter()
            s = self.session
            self.tables = [n for n in s.schema.names("table") if not n.lower().startswith("sqlite_")
                           and s.info(n).columns]
            for t in self.tables:
                info = s.info(t)
                self._by_name.setdefault(t.lower(), t)
                cols = [c for c in info.columns if c.hidden != 1]
                self._cols[t] = dict((c.name.lower(), c.name) for c in cols)
                for c in cols:
                    self._aff[(t, c.name.lower())] = column_affinity(c.decl_type)
                    self._same.setdefault(c.name.lower(), []).append((t, c.name))
                self._keys[t] = self._key_of(info)
            self._alias = self._aliases()
            self._core_data = "z_primarykey" in self._by_name
            fks = self._replay_schema()
            fk_columns = []
            for t in self.tables:
                for frm, parent, to in fks.get(t, ()):
                    self._add_fk(t, frm, parent, to)
                for c in self._cols[t].values():
                    if not self._add_name_link(t, c) and c.lower() == "fk":
                        fk_columns.append((t, c))
            for t, c in fk_columns:     # after the other links: they name the candidates
                self._add_fk_column(t, c)
            self.build_seconds = time.perf_counter() - t0
            self._built = True
            return self

    def _aliases(self):
        """{table name without a prefix of table_prefixes(): (table, prefix)}; a name that is
        also a real table's, or that two tables would share, is left out."""
        out = {}
        prefixes = table_prefixes(self.tables)
        for t in self.tables:
            low = t.lower()
            for p in prefixes:
                if not low.startswith(p):
                    continue
                rest = low[len(p):].lstrip("_")
                if len(rest) < 3 or rest in self._by_name:
                    continue
                if rest in out and out[rest][0] != t:
                    out[rest] = (None, None)        # two tables: no alias
                else:
                    out.setdefault(rest, (t, p))
        return dict((k, v) for k, v in out.items() if v[0] is not None)

    @staticmethod
    def _key_of(info):
        """The column a reference to this table names: INTEGER PRIMARY KEY, a column _id or
        id, a one-column PRIMARY KEY, else the rowid (ROWID); False when there is none."""
        cols = info.columns
        if info.rowid_alias is not None:
            return cols[info.rowid_alias].name
        for c in cols:
            if c.name.lower() in ("_id", "id") and c.hidden != 1:
                return c.name
        if len(info.pk_columns) == 1:
            return cols[info.pk_columns[0]].name
        if not info.without_rowid and info.rowid_name:
            return ROWID
        return False

    def _replay_schema(self):
        """{table: [(from column, parent table, parent column or None)]} and the indexed
        columns, from the CREATE statements replayed in a scratch in-memory database."""
        s = self.session
        fks = {}
        roots = dict((e.name, e.rootpage) for e in s.schema.of_type("index"))
        with sqlsafe.Scratch() as scratch:
            unregistered = register_collations(scratch.conn, s.schema.collations)
            for t in self.tables:
                sql = rename_create_table(s.info(t).sql, t)
                if not sql:
                    self.problems.append((t, "CREATE TABLE statement not understood"))
                    continue
                try:
                    scratch.replay(replace_collations(sql, unregistered))
                except sqlsafe.SQL_ERRORS as e:
                    self.problems.append((t, sqlsafe._short(e)))
            for e in s.schema.of_type("index"):
                if e.sql and e.tbl_name in self._cols:
                    try:
                        scratch.replay(replace_collations(e.sql, unregistered))
                    except sqlsafe.SQL_ERRORS:
                        pass        # the index only helps choosing a lookup strategy
            for t in self.tables:
                q = quote_ident(t)
                try:
                    rows = scratch.pragma("PRAGMA foreign_key_list(%s)" % q)
                except sqlsafe.SQL_ERRORS:
                    rows = []
                fks[t] = [(r[3], r[2], r[4]) for r in rows]
                first = set()
                disk = self._disk_index[t] = {}
                try:
                    for r in scratch.pragma("PRAGMA index_list(%s)" % q):
                        info = scratch.pragma("PRAGMA index_xinfo(%s)" % quote_ident(r[1]))
                        lead = [x for x in info if x[0] == 0]
                        if lead and lead[0][2]:
                            first.add(lead[0][2].lower())
                            partial = len(r) > 4 and r[4]
                            root = roots.get(r[1], 0)
                            if not partial and not lead[0][3] and root > 0:
                                disk.setdefault(lead[0][2].lower(), root)
                except sqlsafe.SQL_ERRORS:
                    pass
                key = self._keys.get(t)
                if isinstance(key, str) and self.session.info(t).rowid_alias is not None:
                    first.add(key.lower())
                self._indexed[t] = first
        return fks

    def _table(self, name):
        return self._by_name.get((name or "").lower())

    def _column(self, table, name):
        if name is ROWID:
            return ROWID
        return self._cols.get(table, {}).get(name.lower())

    def affinity(self, table, column):
        if column is ROWID:
            return "INTEGER"
        return self._aff.get((table, column.lower()), "BLOB")

    def _type_check(self, t, c, u, d, base, reasons):
        a, b = self.affinity(t, c), self.affinity(u, d)
        ca, cb = _class(a), _class(b)
        if ca != "any" and cb != "any" and ca != cb:
            reasons.append("types differ: %s %s vs %s %s (values are converted to compare)"
                           % (display_column(self.session.info(t), c), a,
                              display_column(self.session.info(u), d), b))
            return base * TYPE_MISMATCH_FACTOR
        return base

    def _edge(self, t, c, u, d, kind, base, reasons):
        base = self._type_check(t, c, u, d, base, reasons)
        lc = c.lower() if c is not ROWID else ROWID
        ld = d.lower() if d is not ROWID else ROWID
        out = self._out.setdefault((t, lc), [])
        back = self._in.setdefault((u, ld), [])
        for i, (ou, od, _k, ob, oreasons) in enumerate(out):
            if ou == u and (od.lower() if od is not ROWID else ROWID) == ld:
                oreasons.extend(r for r in reasons if r not in oreasons)
                if base > ob:       # e.g. a declared key that the name also gives
                    out[i] = (u, d, kind, base, oreasons)
                    for j, entry in enumerate(back):
                        if entry[0] == t and entry[1] == c:
                            back[j] = (t, c, kind, base, oreasons)
                return
        out.append((u, d, kind, base, reasons))
        back.append((t, c, kind, base, reasons))

    def _add_fk(self, t, frm, parent, to):
        c = self._column(t, frm) if frm else None
        u = self._table(parent)
        if c is None or u is None:
            return
        d = self._column(u, to) if to else self._keys.get(u)
        if d is None or d is False:
            return
        self._edge(t, c, u, d, "fk", FK_SCORE,
                   ["declared FOREIGN KEY: %s REFERENCES %s(%s)"
                    % (c, u, display_column(self.session.info(u), d))])

    def name_target(self, column):
        """(table, exact) the name of a column refers to, or None: exact is False when only
        its last words name the table (sender_jid_row_id -> jid). A table's name also counts
        without its prefix (table_prefixes(): place_id -> moz_places)."""
        hit = self._name_hit(column)
        return None if hit is None else hit[:2]

    def _name_hit(self, column):
        """(table, exact, prefix left out of the table's name or None), or None."""
        base = name_base(column)
        if base is None:
            return None
        words = [w for w in base.split("_") if w]
        for i in range(len(words)):
            stem = "_".join(words[i:])
            if i and len(stem) < 3:
                continue
            forms = _forms(stem)
            for form in forms:
                u = self._by_name.get(form)
                if u is not None:
                    return u, i == 0, None
            for form in forms:
                hit = self._alias.get(form)
                if hit is not None:
                    return hit[0], i == 0, hit[1]
        return None

    def _add_name_link(self, t, c):
        """Add the link the column's name gives, if any; True when one was added."""
        if self._core_data and self._add_core_data_link(t, c):
            return True
        hit = self._name_hit(c)
        bare = False
        if hit is None and _class(self.affinity(t, c)) == "number" and len(c) >= 3:
            # a number column named after a table: visits.url -> urls
            u = next((self._by_name[f] for f in _forms(c.lower()) if f in self._by_name), None)
            if u is not None and u != t:
                hit, bare = (u, False, None), True
        if hit is None:
            return c.lower() in _PARENT_NAMES and self._add_parent_link(t, c)
        u, exact, prefix = hit
        d = self._keys.get(u)
        if d is False or (u == t and (exact or (d is not ROWID and d.lower() == c.lower()))):
            return False            # node.node_id is the table's own identifier
        dname = display_column(self.session.info(u), d)
        without = " (its name without the prefix %s)" % prefix if prefix else ""
        if bare:
            reasons = ["name: the number column %s is named after table %s (key %s)"
                       % (c, u, dname)]
        elif exact:
            reasons = ["name: %s refers to table %s%s (key %s)" % (c, u, without, dname)]
        elif u == t:
            reasons = ["name: %s ends in a reference to its own table %s%s (key %s): a "
                       "self-reference" % (c, u, without, dname)]
        else:
            reasons = ["name: %s ends in a reference to table %s%s (key %s)"
                       % (c, u, without, dname)]
        self._edge(t, c, u, d, "name", NAME_SCORE if exact else NAME_TAIL_SCORE, reasons)
        return True

    def _add_parent_link(self, t, c):
        """parent, parent_id, parentid, parent_row_id: a row of the same table (its key)."""
        d = self._keys.get(t)
        if d is False or (d is not ROWID and d.lower() == c.lower()):
            return False
        self._edge(t, c, t, d, "name", NAME_SCORE,
                   ["name: %s refers to a parent row of the same table %s (key %s): a "
                    "self-reference" % (c, t, display_column(self.session.info(t), d))])
        return True

    def _add_core_data_link(self, t, c):
        """Apple Core Data's names (the database has Z_PRIMARYKEY): an INTEGER column ZFOO ->
        table ZFOO's key Z_PK (ZROOTFOLDER -> ZFOLDER by its last word); Z_ENT ->
        Z_PRIMARYKEY.Z_ENT; in a many-to-many join table Z_1NOTES -> ZNOTE. True when a
        link was added."""
        low = c.lower()
        if low == "z_ent":
            u = self._by_name["z_primarykey"]
            d = self._column(u, "Z_ENT")
            if u == t or d is None:
                return False
            self._edge(t, c, u, d, "name", NAME_SCORE,
                       ["Core Data: %s is the entity number %s.%s lists" % (c, u, d)])
            return True
        if not c.isupper() or _class(self.affinity(t, c)) != "number":
            return False
        m = _CORE_DATA_JOIN_RE.match(c)
        if m:
            stem = m.group(1).lower()
            u = next((self._by_name["z" + f] for f in _forms(stem) if "z" + f in self._by_name),
                     None)
            if u is None or not self._core_data_key(u):
                return False
            self._edge(t, c, u, self._keys[u], "name", NAME_SCORE,
                       ["Core Data: %s holds keys of %s (a many-to-many join table)" % (c, u)])
            return True
        m = _CORE_DATA_COLUMN_RE.match(c)
        if "_" in c or not m:
            return False
        name = m.group(1).lower()
        u, exact = self._by_name.get("z" + name), True
        if u is None:
            # the last word names the table: ZROOTFOLDER -> ZFOLDER (the longest such name);
            # in an inverse relationship XBEINGY the row is an X (ZHIGHLIGHTBEINGKEYASSET
            # names a highlight, not an asset). Only for an indexed column, as Core Data
            # indexes its to-one relationships (a count such as ZNUMBEROFPICKUPS...USAGE is not)
            if low not in self._indexed.get(t, ()):
                return False
            tail = name.split("being")[0] if "being" in name else name
            ends = sorted((n for n in self._by_name if n.startswith("z") and "_" not in n and
                           len(n) >= 5 and tail.endswith(n[1:]) and name != n[1:]),
                          key=lambda n: (-len(n), n))
            u, exact = (self._by_name[ends[0]], False) if ends else (None, False)
        if u is None or (u == t and exact) or not self._core_data_key(u):
            return False
        d = self._keys[u]
        if exact:
            why = "Core Data: %s refers to table %s (key %s)" % (c, u, d)
        else:
            why = "Core Data: %s ends in the name of table %s (key %s)%s" % (
                c, u, d, ": a self-reference" if u == t else "")
        self._edge(t, c, u, d, "name", NAME_SCORE if exact else NAME_TAIL_SCORE, [why])
        return True

    def _core_data_key(self, table):
        d = self._keys.get(table)
        return isinstance(d, str) and d.lower() == "z_pk"

    def _add_fk_column(self, t, c):
        """A column named fk (Firefox moz_bookmarks.fk): the key of the table whose key holds
        its values best, among the tables other columns refer to by name or declared key (a
        tie goes to the table referred to by more columns, then by name). Its values are read
        to choose (the checks are kept for verify()); nothing is linked when none is found."""
        refs = collections.Counter()
        for (u, ld), back in self._in.items():
            key = self._keys.get(u)
            if u == t or key is False:
                continue
            kl = key.lower() if key is not ROWID else ROWID
            if ld == kl:
                refs[u] += len(set((t2, c2 if c2 is ROWID else c2.lower())
                                   for t2, c2, _k, _b, _r in back if t2 != u))
        cands = sorted(u for u in refs if refs[u] and self.is_rowid(u, self._keys[u]))
        best = None
        for u in cands:
            ov = self.check((t, c, u, self._keys[u]))
            if ov is None or ov.error or not ov.found:
                continue
            rank = (-ov.found, -refs[u], u)
            if best is None or rank < best[0]:
                best = (rank, u, ov)
        if best is None:
            return False
        _rank, u, ov = best
        d = self._keys[u]
        self._edge(t, c, u, d, "name", NAME_TAIL_SCORE,
                   ["name: fk holds keys of %s (key %s): its values fit it best among the %d "
                    "table%s other columns refer to" % (u, display_column(self.session.info(u),
                                                                         d),
                                                        len(cands), "" if len(cands) == 1
                                                        else "s")])
        return True

    # -- queries -----------------------------------------------------------------------------
    def key(self, table):
        """The column references to this table name (ROWID for the rowid; False: none)."""
        self.build()
        return self._keys.get(table, False)

    def indexed(self, table, column):
        """True when SQLite can seek the column (rowid, INTEGER PRIMARY KEY or the first
        column of an index)."""
        self.build()
        return column is ROWID or column.lower() in self._indexed.get(table, ())

    def is_rowid(self, table, column):
        """True for ROWID and for the INTEGER PRIMARY KEY column (which is the rowid)."""
        if column is ROWID:
            return True
        info = self.session.info(table)
        return info.rowid_alias is not None and \
            info.columns[info.rowid_alias].name.lower() == column.lower()

    def resolve(self, table, column):
        """The column's name as the schema writes it (ROWID stays ROWID); KeyError if unknown."""
        self.build()
        if table not in self._cols:
            raise KeyError(table)
        if column is ROWID:
            if self.session.info(table).without_rowid:
                raise KeyError("rowid")
            return ROWID
        c = self._column(table, column)
        if c is None:
            raise KeyError(column)
        return c

    def for_column(self, table, column, min_score=0.0):
        """Every Relation of table.column, best first. ROWID (or the name of the rowid alias
        or key) also lists the columns that refer to the table's rows."""
        self.build()
        c = self.resolve(table, column)
        lc = c.lower() if c is not ROWID else ROWID
        found = {}

        def add(rel):
            k = (rel.other, rel.other_column.lower() if rel.other_column is not ROWID else ROWID)
            if k == (table, lc):
                return
            old = found.get(k)
            if old is None:
                found[k] = rel
            elif rel.base > old.base:
                rel.reasons.extend(r for r in old.reasons if r not in rel.reasons)
                found[k] = rel
            else:
                old.reasons.extend(r for r in rel.reasons if r not in old.reasons)

        key = self._keys.get(table)
        keys = set([lc])
        if key is ROWID or (isinstance(key, str) and key.lower() == lc) or c is ROWID:
            keys.update([ROWID, key.lower() if isinstance(key, str) else ROWID])
        outs = []
        for k in keys:
            for u, d, kind, base, reasons in self._out.get((table, k), ()):
                link = self._link((table, c, u, d), kind, base)
                outs.append((u, d, link))
                add(Relation(table, c, u, d, kind, "out", reasons, [link]))
        for k in keys:
            for t2, c2, kind, base, reasons in self._in.get((table, k), ()):
                add(Relation(table, c, t2, c2, kind, "in", reasons,
                             [self._link((t2, c2, table, c), kind, base)]))
        # columns that name the same key as this one hold the same kind of value
        for u, d, link in outs:
            ld = d.lower() if d is not ROWID else ROWID
            for t2, c2, kind2, base2, _r in self._in.get((u, ld), ()):
                if t2 == table and c2.lower() == lc:
                    continue
                add(Relation(table, c, t2, c2, "shared", "peer",
                             ["both refer to %s.%s" % (u, display_column(self.session.info(u), d))],
                             [link, self._link((t2, c2, u, d), kind2, base2)],
                             SHARED_FACTOR, via=(u, d)))
        if c is not ROWID and c.lower() not in _GENERIC:
            strong = id_like(c)
            for t2, c2 in self._same.get(lc, ()):
                if t2 == table:
                    continue
                score = SAME_NAME_SCORE if strong else SAME_NAME_WEAK_SCORE
                reasons = ["same column name %s" % c2]
                score = self._type_check(table, c, t2, c2, score, reasons)
                add(Relation(table, c, t2, c2, "same_name", "peer", reasons,
                             [self._link((table, c, t2, c2), "same_name", score)]))
        rels = [r for r in found.values() if r.score >= min_score]
        rels.sort(key=lambda r: (-r.score, r.other, str(r.other_column)))
        return rels

    def _link(self, check, kind, base):
        link = Link(check, kind, base)
        with self._lock:
            link.overlap = self._overlap.get(check)
        return link

    def summary(self):
        """(tables, columns, links) of the whole map: a link per declared or named reference."""
        self.build()
        cols = sum(len(v) for v in self._cols.values())
        links = sum(len(v) for v in self._out.values())
        return len(self.tables), cols, links

    def all_relations(self, min_score=0.0):
        """{(table, column): [Relation]} for every column of every table (the whole map)."""
        self.build()
        out = {}
        for t in self.tables:
            for c in self._cols[t].values():
                out[(t, c)] = self.for_column(t, c, min_score)
        return out

    # -- the whole map: links, confident relations -------------------------------------------
    def links(self):
        """Every link of the database, best candidates first: each declared or named reference
        (direction 'out'), and pairs of id-like columns sharing a name where neither names a
        table (direction 'peer', groups of at most SAME_NAME_GROUP columns)."""
        self.build()
        with self._lock:
            if self._links is not None:
                return list(self._links)
        out = []
        referring = set()
        for (t, lc), edges in self._out.items():
            c = self._cols[t].get(lc, ROWID) if lc is not ROWID else ROWID
            for u, d, kind, base, reasons in edges:
                referring.add((t, lc))
                out.append(Relation(t, c, u, d, kind, "out", reasons,
                                    [self._link((t, c, u, d), kind, base)]))
        for name, members in self._same.items():
            if name in _GENERIC or not id_like(name) or not 1 < len(members) <= SAME_NAME_GROUP:
                continue
            if any((t, c.lower()) in referring for t, c in members):
                continue
            for i, (t, c) in enumerate(members):
                for u, d in members[i + 1:]:
                    reasons = ["same column name %s" % d]
                    score = self._type_check(t, c, u, d, SAME_NAME_SCORE, reasons)
                    out.append(Relation(t, c, u, d, "same_name", "peer", reasons,
                                        [self._link((t, c, u, d), "same_name", score)]))
        out.sort(key=lambda r: (-r.base, r.table, str(r.column)))
        with self._lock:
            self._links = out
        return list(out)

    def map_links(self, cancel=None, progress=None):
        """Check every link against the values, best candidates first (see verify()), after
        which confident() answers from the cache. progress(tables_done, tables_total) is
        called as the tables whose links are all checked grow. Returns False when cancelled."""
        links = self.links()
        tables = sorted(set(r.table for r in links))
        left = dict((t, 0) for t in tables)
        for r in links:
            left[r.table] += 1
        done = len([t for t in self.tables if t not in left])
        total = len(self.tables)
        if progress is not None:
            progress(done, total)
        for r in links:
            if cancel is not None and cancel():
                return False
            uiyield.pause()
            if not self.verify(r, cancel=cancel):
                return False
            left[r.table] -= 1
            if not left[r.table]:
                done += 1
                if progress is not None:
                    progress(done, total)
        for t in set(r.other for r in links) | set(tables):
            if cancel is not None and cancel():
                return False
            self.table_rows(t)
        with self._lock:
            self.mapped = True
        return True

    def known_rows(self, table):
        """table_rows() when already counted, else None (never reads the database)."""
        with self._lock:
            return self._row_counts.get(table)

    def table_rows(self, table):
        """The table's row count (cached; None when it cannot be counted)."""
        with self._lock:
            if table in self._row_counts:
                return self._row_counts[table]
        try:
            n = self.session.count(table)
        except sqlite3.Error as e:
            if is_interrupt(e):
                raise
            n = None
        except Exception:           # noqa: BLE001 - a table that cannot be counted
            n = None
        with self._lock:
            self._row_counts[table] = n
        return n

    def confident(self, table, column, cancel=None):
        """The relations of table.column that can be trusted (is_confident), best first, after
        checking every relation against the values. Cached per column; None when cancelled."""
        c = self.resolve(table, column)
        key = (table, c.lower() if c is not ROWID else ROWID)
        with self._lock:
            hit = self._confident.get(key)
        if hit is not None:
            return list(hit)
        rels = self.for_column(table, c)
        for rel in rels:
            if not self.verify(rel, cancel=cancel):
                return None
        good = [r for r in rels if is_confident(r)]
        for r in good:              # quick_counts() then knows which tables are small
            if cancel is not None and cancel():
                return None
            self.table_rows(r.other)
        with self._lock:
            self._confident[key] = good
        return list(good)

    def known_confident(self, table, column):
        """confident() when it is already known (cached, or every link is checked), else None:
        never reads the database."""
        try:
            c = self.resolve(table, column)
        except KeyError:
            return []
        key = (table, c.lower() if c is not ROWID else ROWID)
        with self._lock:
            hit = self._confident.get(key)
        if hit is not None:
            return list(hit)
        rels = self.for_column(table, c)
        if not self.mapped and not all(r.verified for r in rels):
            return None
        # once every link is checked, a relation still unchecked (a same-name pair of columns
        # that each refer elsewhere) is simply not trusted
        good = [r for r in rels if is_confident(r)]
        with self._lock:
            self._confident[key] = good
        return list(good)

    def cheap(self, rel, value):
        """True when rows_for(rel, value) answers at once: a rowid seek, an index SQLite can
        seek, an in-memory or on-disk index of a natively read table, or a small table."""
        s = self.session
        u, d = rel.other, rel.other_column
        if self.is_rowid(u, d):
            return True
        v = coerce(value, self.affinity(u, d))
        if s.source(u) == "sql":
            if self.indexed(u, d) and s._seekable(s.info(u), d, _operand_kind(v)):
                return True
        else:
            with self._lock:
                if self._native_idx.get((u, d.lower())) is not None:
                    return True
            if self._disk_index.get(u, {}).get(d.lower()) and isinstance(v, (int, float)):
                return True
        with self._lock:
            n = self._row_counts.get(u)
        return n is not None and n <= limit(CHEAP_SCAN_ROWS)

    def quick_counts(self, table, column, value, cancel=None):
        """[(Relation, count)] of the confident relations of table.column holding the value,
        best first, counting only those cheap() answers at once; None while the column's
        confident relations are not known yet. Never for a trivial value (NULL, 0, empty)."""
        if trivial(value):
            return []
        rels = self.known_confident(table, column)
        if rels is None:
            return None
        out = []
        for rel in rels:
            if cancel is not None and cancel():
                break
            if not self.cheap(rel, value):
                continue
            res = self.rows_for(rel, value, limit=1)
            if res.count:
                out.append((rel, res.count))
        return out

    # -- verification ------------------------------------------------------------------------
    def verify(self, rel, sample=SAMPLE, cancel=None):
        """Check every link of a Relation against the data (see check()); returns False when
        cancel() stopped it."""
        for link in rel.links:
            if link.overlap is None:
                link.overlap = self.check(link.check, sample, cancel)
                if link.overlap is None:
                    return False
        return True

    def check(self, pair, sample=SAMPLE, cancel=None):
        """Overlap for pair = (from_table, from_column, to_table, to_column): sample distinct
        values of from_column and count those to_column holds. Cached per pair; None when
        cancel() -> True stopped it (nothing is cached then)."""
        with self._lock:
            hit = self._overlap.get(pair)
        if hit is not None:
            return hit
        t, c, u, d = pair
        t0 = time.perf_counter()
        try:
            cut = []
            values, exhausted = self.sample(t, c, sample, cancel, cut)
            if cancel is not None and cancel():
                return None
            found, capped = self._count_found(u, d, values, cancel)
        except sqlite3.Error as e:
            if cancel is not None and cancel():
                return None
            ov = Overlap(0, 0, False, error=str(e))
        else:
            if cancel is not None and cancel():
                return None
            ov = Overlap(found, len(values), exhausted, _flag_like(values, exhausted), capped,
                         time.perf_counter() - t0, sample_capped=bool(cut))
        with self._lock:
            self._overlap[pair] = ov
        return ov

    def sample(self, table, column, n=SAMPLE, cancel=None, cut=None):
        """(values, exhausted): up to n distinct non-NULL values of the column, and whether
        the column has no others. A natively read column is read up to the limit
        relations_sample_scan_rows rows; stopping there appends True to the list `cut`."""
        s = self.session
        if column is ROWID:
            got = []
            for i, row in enumerate(s.iter_rows(table)):
                if len(got) >= n or (cancel is not None and i % 1000 == 0 and cancel()):
                    return got, False
                if row.locator.kind == "rowid":
                    got.append(row.locator.value)
            return got, True
        if s.source(table) == "sql":
            q = quote_ident(column)
            try:
                cur = s.conn().execute("SELECT DISTINCT %s FROM %s WHERE %s IS NOT NULL LIMIT ?"
                                       % (q, quote_ident(table), q), (n + 1,))
                try:
                    rows = [r[0] for r in cur.fetchall() if r[0] is not None]
                finally:
                    cur.close()
                return rows[:n], len(rows) <= n
            except sqlite3.Error as e:
                if is_interrupt(e):
                    raise
        cols = s.visible_columns(table)
        ci = [x.lower() for x in cols].index(column.lower())
        seen, got = set(), []
        scan_cap = limit(SAMPLE_SCAN_CAP)
        for i, row in enumerate(s.iter_rows(table)):
            if i >= scan_cap:
                if cut is not None:
                    cut.append(True)
                return got, False
            if cancel is not None and i % 1000 == 0 and cancel():
                return got, False
            v = row.values[ci] if ci < len(row.values) else None
            k = _value_key(v)
            if k is None or k in seen:
                continue
            if len(got) >= n:
                return got, False
            seen.add(k)
            got.append(v)
        return got, True

    def _count_found(self, table, column, values, cancel):
        """(found, capped): how many of the values the column holds."""
        s = self.session
        aff = self.affinity(table, column)
        wanted = [coerce(v, aff) for v in values]
        if self.is_rowid(table, column):
            # only an integer can equal a rowid: other values are not there
            n = 0
            for v in wanted:
                if cancel is not None and cancel():
                    break
                if isinstance(v, int) and not isinstance(v, bool) and \
                        s.row(table, Locator("rowid", v), scan=False) is not None:
                    n += 1
            return n, False
        seek = _class(aff) if _class(aff) != "any" else None
        if s.source(table) == "sql" and self.indexed(table, column) and all(
                seek is None or isinstance(v, bytes) or
                (seek == "number") == isinstance(v, (int, float)) for v in wanted):
            n = 0
            for v in wanted:
                if cancel is not None and cancel():
                    break
                expr = equals_expr(v)
                if expr is not None and s.lookup(table, column, expr, limit=1, count=False)[0]:
                    n += 1
            return n, False
        # no index (or read natively): read the column once (kept for the next links to it)
        with self._lock:
            hit = self._key_sets.get((table, column.lower()))
        if hit is not None:
            return sum(1 for v in wanted if _value_key(v) in hit[0]), hit[1]
        keys = set()
        capped = False
        cols = s.visible_columns(table)
        ci = [x.lower() for x in cols].index(column.lower())
        target_cap = limit(TARGET_SCAN_CAP)
        if s.source(table) == "sql":
            try:
                cur = s.conn().execute("SELECT %s FROM %s LIMIT ?" % (
                    quote_ident(column), quote_ident(table)), (target_cap + 1,))
                try:
                    i = 0
                    while True:
                        uiyield.pause()
                        chunk = cur.fetchmany(5000)
                        if not chunk or (cancel is not None and cancel()):
                            break
                        for r in chunk:
                            i += 1
                            keys.add(_value_key(r[0]))
                finally:
                    cur.close()
                capped = i > target_cap
                if not (cancel is not None and cancel()) and len(keys) <= limit(KEY_SET_CAP):
                    with self._lock:
                        if len(self._key_sets) >= limit(KEY_SETS):
                            self._key_sets.pop(next(iter(self._key_sets)))
                        self._key_sets[(table, column.lower())] = (keys, capped)
                return sum(1 for v in wanted if _value_key(v) in keys), capped
            except sqlite3.Error as e:
                if is_interrupt(e):
                    raise
                keys = set()
        if s.source(table) == "native":
            index = self._native_index(table, column, cancel)
            if index is not None:
                return sum(1 for v in wanted if _value_key(v) in index), False
            if self._disk_index.get(table, {}).get(column.lower()) and \
                    all(isinstance(v, (int, float)) for v in wanted):
                n = 0
                for v in wanted:
                    if cancel is not None and cancel():
                        break
                    hit = self._disk_rows(table, column, v)
                    if hit is None:
                        break
                    n += bool(hit)
                else:
                    return n, False
        for i, row in enumerate(s.iter_rows(table)):
            if i >= target_cap:
                capped = True
                break
            if cancel is not None and i % 1000 == 0 and cancel():
                break
            keys.add(_value_key(row.values[ci] if ci < len(row.values) else None))
        return sum(1 for v in wanted if _value_key(v) in keys), capped

    def _native_index(self, table, column, cancel=None):
        """{value key: [row locators, in natural order]} of a natively read column, built by
        one scan and kept for the session: without it every lookup would read the whole
        table. None for a table larger than the limit relations_native_index_rows (it is then
        read through its own index or scanned: slower, nothing left out), or when cancelled."""
        key = (table, column.lower())
        with self._lock:
            if key in self._native_idx:
                return self._native_idx[key]
        s = self.session
        ci = [x.lower() for x in s.visible_columns(table)].index(column.lower())
        index = {}
        cap = limit(NATIVE_INDEX_CAP)
        try:
            big = s.count(table) > cap
        except Exception:           # noqa: BLE001 - an uncountable table is simply scanned
            big = False
        for i, row in enumerate(() if big else s.iter_rows(table)):
            if i >= cap:
                big = True
                break
            if cancel is not None and i % 1000 == 0 and cancel():
                return None
            k = _value_key(row.values[ci] if ci < len(row.values) else None)
            if k is not None:
                index.setdefault(k, []).append(row.locator)
        if big:
            index = None
        with self._lock:
            self._native_idx[key] = index
        return index

    def _disk_rows(self, table, column, value):
        """The rows of a natively read rowid table whose column equals a number, found through
        the file's own index on the column (its B-tree read natively), or None when there is
        no usable one. Each row is read again and kept only when its value really equals the
        number, so a stale or damaged index cannot add rows (it can still miss some: used only
        for tables too large to index in memory)."""
        s = self.session
        info = s.info(table)
        root = self._disk_index.get(table, {}).get(column.lower())
        if not root or info.without_rowid or not isinstance(value, (int, float)) \
                or isinstance(value, bool):
            return None
        enc = s.encoding

        def compare(payload):
            v = decode_record_lenient(payload, enc)[0]
            first = v[0] if v else None
            if first is None:
                return -1                       # NULL sorts first
            if isinstance(first, (int, float)):
                return (first > value) - (first < value)
            return 1                            # text and BLOB sort after numbers
        ci = [x.lower() for x in s.visible_columns(table)].index(column.lower())
        want = _value_key(value)
        rows = []
        try:
            for payload, _ref in BTreeReader(s.pager, s.issues).seek_index(root, compare):
                v = decode_record_lenient(payload, enc)[0]
                if not v or not isinstance(v[-1], int):
                    continue
                row = s.row(table, Locator("rowid", v[-1]), scan=False)
                if row is not None and ci < len(row.values) and \
                        _value_key(row.values[ci]) == want:
                    rows.append(row)
        except Exception:           # noqa: BLE001 - a damaged index: the caller scans instead
            return None
        return rows

    # -- rows --------------------------------------------------------------------------------
    def related_rows(self, table, column, value, limit=ROW_LIMIT, min_score=MIN_SCORE,
                     cancel=None, relations=None, verify=False):
        """Yield a RelatedResult for every related column (score >= min_score, best first, or
        the given relations): the rows whose column equals the value. verify=True checks each
        relation's values first (cached, see verify()), so min_score applies to the checked
        score and a misleading name is left out."""
        if relations is not None:
            rels = relations
        else:
            rels = self.for_column(table, column, 0.0 if verify else min_score)
        for rel in rels:
            if cancel is not None and cancel():
                return
            if verify and relations is None:
                if not self.verify(rel, cancel=cancel):
                    return
                if rel.score < min_score:
                    continue
            yield self.rows_for(rel, value, limit, cancel)

    def rows_for(self, rel, value, limit=ROW_LIMIT, cancel=None):
        """The RelatedResult of one relation for a value of its queried column."""
        s = self.session
        u, d = rel.other, rel.other_column
        cols = s.visible_columns(u)
        v = coerce(value, self.affinity(u, d))
        t0 = time.perf_counter()
        if self.is_rowid(u, d) and (not isinstance(v, int) or isinstance(v, bool)):
            # only an integer can equal a rowid
            return RelatedResult(rel, 0, [], cols, s.source(u), "not a row id", v)
        if self.is_rowid(u, d):
            # the rowid (or its INTEGER PRIMARY KEY alias): a seek, in SQL and natively
            row = s.row(u, Locator("rowid", v), scan=False)
            return RelatedResult(rel, 1 if row is not None else 0,
                                 [row] if row is not None else [], cols, s.source(u), "", v,
                                 time.perf_counter() - t0)
        expr = equals_expr(v)
        if expr is None:
            return RelatedResult(rel, 0, [], cols, s.source(u),
                                 "NULL or invalid text: nothing to look up", v)
        if s.source(u) == "native":
            index = self._native_index(u, d, cancel)
            if index is not None:
                locs = index.get(_value_key(v), ())
                rows = [r for r in (s.row(u, loc, scan=False) for loc in locs[:limit])
                        if r is not None]
                return RelatedResult(rel, len(locs), rows, cols, "native", "", v,
                                     time.perf_counter() - t0)
            rows = self._disk_rows(u, d, v)
            if rows is not None:
                return RelatedResult(rel, len(rows), rows[:limit], cols, "native",
                                     "found through the table's index", v,
                                     time.perf_counter() - t0)
        n, rows, src = s.lookup(u, d, expr, limit=limit, cancel=cancel)
        note = ""
        if src == "unavailable":
            note = "the table cannot be read"
        return RelatedResult(rel, n, rows, cols, src, note, v, time.perf_counter() - t0)
