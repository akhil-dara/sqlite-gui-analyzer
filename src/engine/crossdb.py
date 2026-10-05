"""Links between the databases of a case, found and checked by their values.

Two databases of one extraction often hold the same identifiers: a messages database keeps
the contact of each chat as text (msgstore jid.raw_string '123@s.whatsapp.net') that a contacts
database keeps too (wa.db wa_contacts.jid). No schema says so, and names differ, so these
links are found by the values alone:

  1. profile   every text column that holds identifier-like values (text of 4..200
               characters without spaces: ids, jids, URLs, e-mail addresses, host names,
               file paths, phone numbers written as text) gets a sample of its distinct values
               (limit crossdb_profile_values, read from at most crossdb_profile_rows rows).
  2. pair      columns of DIFFERENT databases whose samples share at least crossdb_min_shared
               values are candidates (at most crossdb_max_pairs, most shared first).
  3. check     each candidate is checked both ways: up to crossdb_check_values distinct
               values of one column are looked up in the whole other column with batched
               'IN (...)' queries (an index seek per value, or one scan per batch - never a
               scan per value); a natively read table as engine.relations checks a link. The
               direction whose values are found more is the link: from -> to.

Integer columns take part only as identifiers, as small integers (row ids, flags, counts, types)
coincide everywhere. An integer pair must be named as one: the key of a table in one database
(its INTEGER PRIMARY KEY or id / _id / Z_PK column) and a column of the other whose name refers
to that table (engine.relations' name rules: chat_row_id -> chat, place_id -> moz_places) while
its own database has no such table; or two columns of the same id-like name that name no table
of their own database (contact_id and contact_id; never the generic id, _id, rowid, and never
two keys). Both columns need at least crossdb_min_found distinct integers in their sample, not
all of them within -1..9 (flags, types and status codes), and overlapping value ranges; their
samples must share crossdb_min_shared values, and the pair is then checked as above.

A cross-database link is always 'matched by value' (never a declared key): it is trusted
(confident) when at least CONFIDENT_OVERLAP of the sample and crossdb_min_found values were
found (an integer link says which name rule paired it). The limits are engine.limits settings.

Nothing here writes anything; every read goes through the databases' Sessions.
"""

import re
import sqlite3
import time

from . import uiyield
from .limits import get
from .relations import CONFIDENT_OVERLAP, _GENERIC, Overlap, _flag_like, id_like, name_base, \
    relation_map
from .schema import column_affinity, quote_ident
from .session import is_interrupt

PROFILE_SAMPLE = "crossdb_profile_values"  # engine.limits names of the caps used here
PROFILE_SCAN = "crossdb_profile_rows"
MIN_LEN, MAX_LEN = 4, 200   # identifier-like text
MIN_DISTINCT = 3            # a column needs this many identifier-like values
MIN_SHARED = "crossdb_min_shared"
MIN_FOUND = "crossdb_min_found"
CHECK_VALUES = "crossdb_check_values"
MAX_PAIRS = "crossdb_max_pairs"
IDENTIFIER_SHARE = 0.8      # of a column's sampled text values that must look like ids

_SPACE = re.compile(r"\s")
_WORDS = frozenset(("true", "false", "null", "none", "unknown", "default", "yes", "no"))


def identifier_like(v):
    """Text that can identify something across databases (not free text, not a number)."""
    if not isinstance(v, str):
        return False
    n = len(v)
    if n < MIN_LEN or n > MAX_LEN or _SPACE.search(v):
        return False
    return v.lower() not in _WORDS


class Profile(object):
    """A text column of one database with a sample of its distinct identifier-like values."""
    __slots__ = ("db", "table", "column", "values", "rows_read")

    def __init__(self, db, table, column, values, rows_read):
        self.db, self.table, self.column = db, table, column
        self.values, self.rows_read = values, rows_read

    @property
    def key(self):
        return (self.db, self.table, self.column)

    def __repr__(self):
        return "Profile(%s %s.%s, %d values)" % (self.db, self.table, self.column,
                                                  len(self.values))


def _sample_sql(session, table, column, n, scan):
    q, t = quote_ident(column), quote_ident(table)
    cur = session.conn().execute(
        "SELECT DISTINCT v FROM (SELECT %s AS v FROM %s LIMIT ?) WHERE typeof(v) = 'text' "
        "LIMIT ?" % (q, t), (scan, n))
    try:
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _sample_native(session, table, columns, n, scan, cancel, ints=False):
    """{column: distinct text values} of a natively read table (one scan for every column);
    ints=True: distinct integers instead."""
    cols = session.visible_columns(table)
    idx = [(c, [x.lower() for x in cols].index(c.lower())) for c in columns]
    out = dict((c, []) for c in columns)
    seen = dict((c, set()) for c in columns)
    wanted = _is_int if ints else (lambda v: isinstance(v, str))
    for i, row in enumerate(session.iter_rows(table)):
        if i >= scan or (cancel is not None and i % 1000 == 0 and cancel()):
            break
        for c, ci in idx:
            v = row.values[ci] if ci < len(row.values) else None
            if wanted(v) and v not in seen[c] and len(out[c]) < n:
                seen[c].add(v)
                out[c].append(v)
    return out


def _sample_ints_sql(session, table, column, n, scan):
    q, t = quote_ident(column), quote_ident(table)
    cur = session.conn().execute(
        "SELECT DISTINCT v FROM (SELECT %s AS v FROM %s LIMIT ?) WHERE typeof(v) = 'integer' "
        "LIMIT ?" % (q, t), (scan, n))
    try:
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()


class IntProfile(object):
    """An integer id column of one database: the key of its table (is_key) or a column whose
    name makes it an id, with a sample of its distinct integers and their range."""
    __slots__ = ("db", "table", "column", "values", "is_key", "lo", "hi")

    def __init__(self, db, table, column, values, is_key):
        self.db, self.table, self.column = db, table, column
        self.values, self.is_key = values, is_key
        self.lo, self.hi = min(values), max(values)

    @property
    def key(self):
        return (self.db, self.table, self.column)

    def __repr__(self):
        return "IntProfile(%s %s.%s%s, %d values %d..%d)" % (
            self.db, self.table, self.column, " key" if self.is_key else "", len(self.values),
            self.lo, self.hi)


def id_values(values):
    """True for a sample of integers that can identify rows across databases: at least
    crossdb_min_found distinct values, not all within -1..9 (a flag, a type, a status code)."""
    ints = [v for v in values if _is_int(v)]
    return len(set(ints)) >= get(MIN_FOUND) and any(not -1 <= v <= 9 for v in ints)


def profile_integers(key, session, cancel=None, problems=None):
    """[IntProfile] of the integer id columns of one database: each table's key (INTEGER
    PRIMARY KEY, or an INTEGER column id / _id / Z_PK) and the INTEGER columns with an id-like
    name (chat_row_id, contact_id), whose samples pass id_values()."""
    out = []
    m = relation_map(session)
    m.build()
    for t in m.tables:
        if cancel is not None and cancel():
            break
        try:
            info = session.info(t)
            if session.source(t) == "unavailable":
                continue
            k = m.key(t)
            want = []
            for c in info.columns:
                if c.hidden == 1 or column_affinity(c.decl_type) != "INTEGER":
                    continue
                is_key = isinstance(k, str) and c.name.lower() == k.lower()
                if is_key or id_like(c.name) or name_base(c.name) is not None:
                    want.append((c.name, is_key))
            if not want:
                continue
            n, scan = get(PROFILE_SAMPLE), get(PROFILE_SCAN)
            if session.source(t) == "sql":
                sampled = {}
                for c, _k in want:
                    try:
                        sampled[c] = _sample_ints_sql(session, t, c, n, scan)
                    except sqlite3.Error as e:
                        if is_interrupt(e):
                            raise
                        sampled.update(_sample_native(session, t, [c], n, scan, cancel, True))
            else:
                sampled = _sample_native(session, t, [c for c, _k in want], n, scan, cancel,
                                         True)
        except sqlite3.Error as e:
            if is_interrupt(e):
                break
            if problems is not None:
                problems.append("%s %s: %s" % (key, t, e))
            continue
        except Exception as e:          # noqa: BLE001 - one table must not stop the rest
            if problems is not None:
                problems.append("%s %s: %s" % (key, t, e))
            continue
        for c, is_key in want:
            values = [v for v in sampled.get(c, ()) if _is_int(v)]
            if id_values(values):
                out.append(IntProfile(key, t, c, values, is_key))
    return out


def integer_pairs(profiles, maps, min_shared=None):
    """[(profile a, profile b, shared sampled values, rule)] of integer id columns of different
    databases that are named as one (see the module doc), whose value ranges overlap and
    whose samples share at least min_shared values. For a key and a column naming its table,
    a is the naming column and b the key. maps: {database key: its RelationMap}."""
    if min_shared is None:
        min_shared = get(MIN_SHARED)
    keys = dict(((p.db, p.table), p) for p in profiles if p.is_key)
    out = []
    seen = set()

    def add(a, b, rule):
        pair = (a.key, b.key)
        if pair in seen or (b.key, a.key) in seen:
            return
        if max(a.lo, b.lo) > min(a.hi, b.hi):
            return                  # the value ranges do not overlap
        shared = len(set(a.values) & set(b.values))
        if shared >= min_shared:
            seen.add(pair)
            out.append((a, b, shared, rule))

    local = set(p.key for p in profiles if maps[p.db].name_target(p.column) is not None)
    for a in profiles:
        if a.is_key or a.key in local:
            continue                # a key, or a reference inside its own database
        for db in sorted(maps, key=str):
            if db == a.db:
                continue
            hit = maps[db].name_target(a.column)
            if hit is None or not hit[1]:
                continue
            b = keys.get((db, hit[0]))
            if b is not None:
                add(a, b, "integer ids: %s names table %s" % (a.column, b.table))
    groups = {}
    for p in profiles:
        n = p.column.lower()
        if n not in _GENERIC and id_like(n) and p.key not in local:
            groups.setdefault(n, []).append(p)
    for n in sorted(groups):
        members = groups[n]
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if a.db != b.db and not (a.is_key and b.is_key):
                    add(a, b, "integer ids: same column name %s" % a.column)
    out.sort(key=lambda t: (-t[2], t[0].key, t[1].key))
    return out


def profile_database(key, session, cancel=None, progress=None, problems=None):
    """[Profile] of the identifier-like text columns of one database (key names it).
    Columns declared INTEGER or REAL are skipped; a column qualifies when at least
    IDENTIFIER_SHARE of its sampled text looks like identifiers and MIN_DISTINCT do."""
    out = []
    tables = [n for n in session.schema.names("table") if not n.lower().startswith("sqlite_")]
    for i, t in enumerate(tables):
        if cancel is not None and cancel():
            break
        if progress is not None:
            progress(i, len(tables))
        try:
            info = session.info(t)
            if not info.columns or session.source(t) == "unavailable":
                continue
            cols = [c.name for c in info.columns if c.hidden != 1 and
                    column_affinity(c.decl_type) not in ("INTEGER", "REAL")]
            if info.rowid_alias is not None:
                alias = info.columns[info.rowid_alias].name
                cols = [c for c in cols if c != alias]
            if not cols:
                continue
            if session.source(t) == "sql":
                sampled = {}
                for c in cols:
                    try:
                        sampled[c] = _sample_sql(session, t, c, get(PROFILE_SAMPLE),
                                                 get(PROFILE_SCAN))
                    except sqlite3.Error as e:
                        if is_interrupt(e):
                            raise
                        sampled.update(_sample_native(session, t, [c], get(PROFILE_SAMPLE),
                                                      get(PROFILE_SCAN), cancel))
            else:
                sampled = _sample_native(session, t, cols, get(PROFILE_SAMPLE), get(PROFILE_SCAN),
                                         cancel)
        except sqlite3.Error as e:
            if is_interrupt(e):
                break
            if problems is not None:
                problems.append("%s %s: %s" % (key, t, e))
            continue
        except Exception as e:          # noqa: BLE001 - one table must not stop the rest
            if problems is not None:
                problems.append("%s %s: %s" % (key, t, e))
            continue
        for c, values in sampled.items():
            texts = [v for v in values if isinstance(v, str)]
            ids = [v for v in texts if identifier_like(v)]
            if len(ids) >= MIN_DISTINCT and len(ids) >= IDENTIFIER_SHARE * len(texts):
                out.append(Profile(key, t, c, ids, len(values)))
    if progress is not None:
        progress(len(tables), len(tables))
    return out


def candidate_pairs(profiles, min_shared=None):
    """[(profile a, profile b, shared sampled values)] for columns of different databases
    whose samples share at least min_shared values, most shared first. A value found in
    more than crossdb_common_value_columns columns (a default, a placeholder) links nothing."""
    if min_shared is None:
        min_shared = get(MIN_SHARED)
    common = get("crossdb_common_value_columns")
    index = {}
    for i, p in enumerate(profiles):
        for v in p.values:
            index.setdefault(v, []).append(i)
    shared = {}
    for v, owners in index.items():
        if len(owners) < 2 or len(owners) > common:
            continue
        for x in range(len(owners)):
            a = profiles[owners[x]]
            for y in range(x + 1, len(owners)):
                b = profiles[owners[y]]
                if a.db == b.db:
                    continue
                k = (owners[x], owners[y])
                shared[k] = shared.get(k, 0) + 1
    out = [(profiles[i], profiles[j], n) for (i, j), n in shared.items() if n >= min_shared]
    out.sort(key=lambda t: (-t[2], t[0].key, t[1].key))
    return out


class CrossLink(object):
    """A link between columns of two databases, matched by value: the values of
    (src_db, src_table, src_col) are found in (dst_db, dst_table, dst_col). `overlap` is the
    check of that direction, `back` the other direction. `rule`: for integer ids, the name
    rule that paired the columns ('' for text)."""
    __slots__ = ("src_db", "src_table", "src_col", "dst_db", "dst_table", "dst_col", "overlap",
                 "back", "shared", "rule")

    def __init__(self, src, dst, overlap, back, shared=0, rule=""):
        self.src_db, self.src_table, self.src_col = src
        self.dst_db, self.dst_table, self.dst_col = dst
        self.overlap, self.back, self.shared, self.rule = overlap, back, shared, rule

    kind = "value"

    @property
    def fraction(self):
        return self.overlap.fraction if self.overlap is not None else 0.0

    @property
    def score(self):
        return round(self.fraction, 3)

    @property
    def confident(self):
        ov = self.overlap
        return ov is not None and not ov.error and ov.sampled and not ov.flag_like and \
            ov.found >= get(MIN_FOUND) and ov.fraction >= CONFIDENT_OVERLAP

    def reason(self):
        """Why the link holds, in plain words (always 'matched by value')."""
        ov = self.overlap
        if ov is None:
            return "matched by value: not checked"
        text = "matched by value%s: %s" % (" (%s)" % self.rule if self.rule else "",
                                           ov.text().replace(
            "sampled values found", "sampled %s.%s values found in %s.%s" % (
                self.src_table, self.src_col, self.dst_table, self.dst_col)))
        b = self.back
        if b is not None and b.sampled and not b.error:
            text += "; the other way %d%% of %d" % (round(100 * b.fraction), b.sampled)
        if ov.sampled and not ov.error and ov.found < get(MIN_FOUND):
            text += "; weaker: too few values (%d distinct value%s match; %d needed)" % (
                ov.found, "" if ov.found == 1 else "s", get(MIN_FOUND))
        return text

    def ends(self):
        return ((self.src_db, self.src_table, self.src_col),
                (self.dst_db, self.dst_table, self.dst_col))

    def touches(self, db, table, column):
        c = column.lower()
        return (self.src_db == db and self.src_table == table and self.src_col.lower() == c) or \
            (self.dst_db == db and self.dst_table == table and self.dst_col.lower() == c)

    def other_end(self, db, table, column):
        """(db, table, column) at the other end of the link seen from one end."""
        c = column.lower()
        if self.src_db == db and self.src_table == table and self.src_col.lower() == c:
            return self.dst_db, self.dst_table, self.dst_col
        return self.src_db, self.src_table, self.src_col

    def __repr__(self):
        return "CrossLink(%s %s.%s -> %s %s.%s %.2f)" % (
            self.src_db, self.src_table, self.src_col, self.dst_db, self.dst_table,
            self.dst_col, self.fraction)


IN_CHUNK = 250              # values per 'IN (...)' query (SQLite allows 999 at least)


def _found_sql(session, table, column, values):
    """How many of the (text) values the column holds, by 'SELECT DISTINCT c WHERE c IN
    (...)' in chunks: an index seek per value when the column is indexed, else one scan of
    the table per chunk (never a scan per value)."""
    q = quote_ident(column)
    found = set()
    for i in range(0, len(values), IN_CHUNK):
        chunk = values[i:i + IN_CHUNK]
        cur = session.conn().execute(
            "SELECT DISTINCT %s FROM %s WHERE %s IN (%s)" % (
                q, quote_ident(table), q, ",".join("?" * len(chunk))), chunk)
        try:
            found.update(r[0] for r in cur.fetchall())
        finally:
            cur.close()
    return sum(1 for v in values if v in found)


def check(session_a, a_table, a_col, values, session_b, b_table, b_col, cancel=None):
    """Overlap of up to SAMPLE of these values of a_col found in b_col (None: cancelled).
    SQL-served tables answer with batched IN queries; natively read ones as
    engine.relations checks a link (an in-memory index of the column, built once)."""
    t0 = time.perf_counter()
    n = get(CHECK_VALUES)
    values = list(values)[:n]
    capped = False
    try:
        if session_b.source(b_table) == "sql":
            try:
                found = _found_sql(session_b, b_table, b_col, values)
            except sqlite3.Error as e:
                if is_interrupt(e):
                    raise
                found = None
        else:
            found = None
        if found is None:
            m = relation_map(session_b)
            m.build()
            found, capped = m._count_found(b_table, b_col, values, cancel)
    except sqlite3.Error as e:
        if cancel is not None and cancel():
            return None
        return Overlap(0, 0, False, error=str(e))
    if cancel is not None and cancel():
        return None
    return Overlap(found, len(values), len(values) < n, _flag_like(values, False), capped,
                   time.perf_counter() - t0)


class CrossResult(object):
    """find_links()' answer: links (every candidate checked, best first), the columns
    profiled per database, timings, problems and whether it was cancelled."""

    def __init__(self):
        self.links, self.profiles, self.problems = [], {}, []
        self.int_profiles = {}      # database key -> integer id columns profiled
        self.candidates = 0
        self.unchecked = 0          # candidate pairs left out by the crossdb_max_pairs limit
        self.profile_seconds = self.check_seconds = 0.0
        self.cancelled = False

    def confident(self):
        return [l for l in self.links if l.confident]

    def limits_text(self):
        """What the limits left out or how the columns were sampled, in plain words."""
        out = ["text and integer id columns sampled from their first %s rows"
               % format(get(PROFILE_SCAN), ",")]
        if self.unchecked:
            out.append("%s candidate column pairs not checked (limit crossdb_max_pairs = %s)"
                       % (format(self.unchecked, ","), format(get(MAX_PAIRS), ",")))
        return "; ".join(out)


def find_links(databases, cancel=None, progress=None):
    """Links between the given databases [(key, session)], checked by their values (see the
    module doc). progress(step, done, total) with step 'profile' or 'check'."""
    res = CrossResult()
    t0 = time.perf_counter()
    profiles, ints, maps = [], [], {}
    sessions = dict(databases)
    for i, (key, session) in enumerate(databases):
        if cancel is not None and cancel():
            res.cancelled = True
            return res
        if progress is not None:
            progress("profile", i, len(databases))
        uiyield.pause()
        got = profile_database(key, session, cancel, problems=res.problems)
        res.profiles[key] = len(got)
        profiles.extend(got)
        maps[key] = relation_map(session)
        got = profile_integers(key, session, cancel, problems=res.problems)
        res.int_profiles[key] = len(got)
        ints.extend(got)
    res.profile_seconds = time.perf_counter() - t0
    t1 = time.perf_counter()
    pairs = [(a, b, n, "") for a, b, n in candidate_pairs(profiles)]
    pairs.extend(integer_pairs(ints, maps))
    pairs.sort(key=lambda t: (-t[2], t[0].key, t[1].key))    # the most shared first
    res.candidates = len(pairs)
    cap = get(MAX_PAIRS)
    if len(pairs) > cap:
        res.unchecked = len(pairs) - cap
        pairs = pairs[:cap]         # the most shared first
    for i, (a, b, shared, rule) in enumerate(pairs):
        if cancel is not None and cancel():
            res.cancelled = True
            break
        if progress is not None:
            progress("check", i, len(pairs))
        uiyield.pause()
        sa, sb = sessions[a.db], sessions[b.db]
        ab = check(sa, a.table, a.column, a.values, sb, b.table, b.column, cancel)
        ba = check(sb, b.table, b.column, b.values, sa, a.table, a.column, cancel)
        if ab is None or ba is None:
            res.cancelled = True
            break
        # a column naming a table's key refers to it; otherwise the better-found direction
        fixed = isinstance(b, IntProfile) and b.is_key and not a.is_key
        if not fixed and ba.fraction > ab.fraction:
            a, b, ab, ba = b, a, ba, ab
        res.links.append(CrossLink(a.key, b.key, ab, ba, shared, rule))
    res.check_seconds = time.perf_counter() - t1
    res.links.sort(key=lambda l: (-l.confident, -l.fraction, l.src_db, l.src_table,
                                  l.src_col))
    if progress is not None:
        progress("check", len(pairs), len(pairs))
    return res
