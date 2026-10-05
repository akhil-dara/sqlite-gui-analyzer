"""Row sources: SQL (sqlite3 on the immutable original or an in-RAM WAL overlay image)
and native (our own B-tree reader)."""

import bisect
import re
import sqlite3
import threading
import uuid

from . import sqlsafe
from .evidence import sqlite_uri
from .filters import Expr, RowFilter, check_chars, like_escape, regexp_value  # noqa: F401
from .fileformat.btree import BTreeReader
from .fileformat.record import InvalidText, decode_record_lenient
from .schema import Locator, register_collations


def lenient_text(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return InvalidText(raw)


def safe_regexp(pattern, value):
    if value is None:
        return False
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return re.search(pattern, str(value)) is not None
    except Exception:
        return False


def ram_overlay_supported():
    return (hasattr(sqlite3.Connection, "deserialize")
            and sqlite3.sqlite_version_info >= (3, 36, 0))


_FILE_PRAGMAS = ("temp_store_directory", "data_store_directory")


def read_only_authorizer(action, arg1, arg2, db_name, trigger, guard=None):
    """Refuse statements that can create files wherever the user points them (e.g. next to the
    evidence): ATTACH (VACUUM INTO attaches its target too), DETACH, and the pragmas that move
    SQLite's temporary/data files to another directory. Everything else is allowed; writes are
    already impossible because every connection is opened read-only.

    Without setlimit() (Python 3.8-3.10) nothing caps the size of a value SQLite builds, so a
    view of the evidence may not call the functions that can build a value of any size from a
    few bytes, nor run a recursive query. SQLite names the innermost view (or CTE) a callback
    comes from in `trigger`; guard (sqlsafe.ReadGuard) holds the database's view names, notes
    when a statement has entered one of its views (a recursive CTE's callback names the CTE,
    not the view) and remembers what was refused, for the message. The examiner's own SQL is
    not limited this way."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _FILE_PRAGMAS:
        return sqlite3.SQLITE_DENY
    if sqlsafe.HAS_SETLIMIT or guard is None:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_SELECT and trigger is None:
        guard.in_view = False               # a (sub)query of the statement itself starts
    in_view = trigger is not None and trigger.lower() in guard.view_names
    if in_view:
        guard.in_view = True
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in sqlsafe.SIZE_FUNCTIONS \
            and (in_view or guard.in_view):
        guard.denied_function = "%s()" % (arg2 or "").lower()
        return sqlite3.SQLITE_DENY
    if action == getattr(sqlite3, "SQLITE_RECURSIVE", 33) and (in_view or guard.in_view):
        guard.denied_function = "a recursive query (WITH RECURSIVE)"
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


# Held around every close() and interrupt() of an engine connection. On Python <= 3.10 close()
# lets other threads run while SQLite frees the handle, and the connection still looks open:
# an interrupt() or a second close() from another thread then touches freed memory. Re-entrant,
# because a finalizer below can run on a thread that already holds it.
CONN_OP_LOCK = threading.RLock()


class EngineConnection(sqlite3.Connection):
    """A sqlite3 connection that can be weakly referenced and closes itself when freed.

    Session.close() leaves a connection another thread may still be running on to that thread,
    and watches a weak reference to learn when the thread has let go of it.
    """

    def __del__(self):
        # Runs on whichever thread drops the last reference (or in the cycle collector, which
        # Python 3.11+ needs for connections): nothing can be using the connection any more.
        # Its weak references still work while this runs, so Session.close() may be closing it
        # at the same moment: the lock keeps the two apart.
        with CONN_OP_LOCK:
            try:
                self.close()
            except Exception:       # noqa: BLE001 - nothing to report to during finalization
                pass


class SqlBackend(object):
    """Factory for configured, read-only sqlite3 connections."""

    def __init__(self, uri, collations, anchor=None, label="immutable"):
        self.uri = uri
        self.collations = set(collations)
        self.label = label
        self._anchor = anchor   # keeps a shared in-memory image alive
        self.issues = None
        self.view_names = frozenset()   # the database's views (see read_only_authorizer)

    @classmethod
    def for_file(cls, path, collations):
        return cls(sqlite_uri(path, immutable=True), collations)

    @classmethod
    def for_image(cls, image, collations):
        """Load a database image (bytes) into a shared in-memory database (vfs=memdb).

        Requires ram_overlay_supported(). Header bytes 18/19 must already say
        'legacy' (1) because an in-memory database cannot be in WAL mode.
        """
        name = "file:/sga-%s?vfs=memdb" % uuid.uuid4().hex
        tmp = sqlite3.connect(":memory:")
        try:
            # these two only copy pages (no statement reads the image's schema), and are
            # hardened like every connection anyway
            sqlsafe.harden_copy(tmp)
            tmp.deserialize(image)      # a bytearray is taken as it is (no extra copy)
            anchor = sqlite3.connect(name, uri=True, check_same_thread=False)
            sqlsafe.harden_copy(anchor)
            tmp.backup(anchor)
        finally:
            tmp.close()
        anchor.execute("PRAGMA query_only=ON")
        # Readers open the shared image read-only: 'PRAGMA query_only=OFF' in the SQL tab must
        # not let a statement change what Browse and the evidence banners show. Only the
        # anchor (never handed out) is read-write, for the backup above.
        return cls(name + "&mode=ro", collations, anchor, "ram-overlay")

    def connect(self):
        conn = sqlite3.connect(self.uri, uri=True, check_same_thread=False,
                               factory=EngineConnection)
        try:
            conn.text_factory = lenient_text
            sqlsafe.harden_reader(conn, self.issues, self.label)
            guard = getattr(conn, "sga_guard", None)
            if guard is not None:
                guard.view_names = self.view_names
            conn.set_authorizer(lambda a, b, c, d, e: read_only_authorizer(a, b, c, d, e, guard))
        except BaseException:
            conn.close()
            raise
        # Names this Python rejects are skipped (logged once by SchemaModel as
        # 'collation_unregistered'); SQL needing them fails and falls back to native reads.
        register_collations(conn, self.collations)
        conn.create_function("REGEXP", 2, safe_regexp)
        conn.create_function("sga_regexp", 4, regexp_value)    # /regex/ column filters
        return conn

    def close(self):
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None


def build_overlay_image(pager):
    """Main file + current WAL frames as one image (a bytearray, handed to deserialize() as
    it is), with header bytes 18/19 = 1. Its size is the pager's page count, which is never
    more than the pages the files hold (see Pager)."""
    ps = pager.page_size
    img = bytearray(pager.page_count * ps)
    for n in range(1, pager.page_count + 1):
        img[(n - 1) * ps:n * ps] = pager.page(n)
    img[18] = 1
    img[19] = 1
    return img


def text_of(value):
    """Text used for 'contains' filtering and matching, identical for SQL and native rows."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class Filter(RowFilter):
    """Rows matching every condition given. Plain terms match case-insensitively (ASCII
    letters, as SQLite's LIKE) anywhere in the column's text:

      any_term   one term that must occur in at least one column;
      col_terms  {column: term}: the column must contain its term;
      col_exprs  {column: expression}: engine.filters syntax (>5, =x, a~b, /re/, NULL, ...),
                 as text or a parsed filters.Expr;
      words      terms that must each occur in at least one column (the global filter).

    matches() (rows read natively or held in memory) and where_sql() (SQL) select the same
    rows. Raises filters.FilterError for an expression that cannot be used.
    """

    def __init__(self, any_term=None, col_terms=None, col_exprs=None, words=None):
        self.any_term = any_term or None
        self.col_terms = dict((k, v) for k, v in (col_terms or {}).items() if v)
        exprs = []
        for col, term in self.col_terms.items():
            check_chars(term)
            exprs.append((col, Expr("contains", term, term=term)))
        exprs.extend((col_exprs or {}).items())
        anywhere = ([self.any_term] if self.any_term else []) + list(words or [])
        for term in anywhere:
            check_chars(term)
        RowFilter.__init__(self, exprs, anywhere)


class NativeTable(object):
    """Rows of one table read directly from its B-tree through the Pager."""

    def __init__(self, info, pager, issues):
        self.info = info
        self.pager = pager
        self.issues = issues
        self.reader = BTreeReader(pager, issues)
        self._segments = None
        self._starts = None

    def _ensure_segments(self):
        if self._segments is None:
            before = len(self.issues) if self.issues is not None else 0
            segs = self.reader.segments(self.info.root_page, self.info.without_rowid)
            starts, total = [], 0
            for page_no, cell_index, count in segs:
                starts.append(total)
                total += count
            self._segments, self._starts, self._count = segs, starts, total
            if self.issues is not None and len(self.issues) > before:
                first = self.issues.items[before]
                self.problem = "%d problem(s) reading this table's pages, e.g. %s: %s" % (
                    len(self.issues) - before, first.where, first.detail)

    problem = ""

    def count(self):
        self._ensure_segments()
        return self._count

    def _decode(self, rowid, payload, ordinal, ref=None):
        values, problem = decode_record_lenient(payload, self.pager.encoding, self.issues,
                                                "%s row %d" % (self.info.name, ordinal))
        row, flags = self.info.record_to_row(rowid, values, damaged=bool(problem))
        cell = (ref.page, ref.offset) if ref is not None else None
        return self.info.locator_for(rowid, row, ordinal, cell), row, flags

    def rows(self, offset, limit):
        """Natural (rowid / primary key) order slice without decoding skipped rows."""
        self._ensure_segments()
        out = []
        if limit <= 0 or offset >= self._count:
            return out
        i = max(0, bisect.bisect_right(self._starts, offset) - 1)
        pos = offset
        wi = self.info.without_rowid
        while i < len(self._segments) and len(out) < limit:
            page_no, cell_index, count = self._segments[i]
            first = pos - self._starts[i]
            take = min(count - first, limit - len(out))
            for rowid, payload, ref in self.reader.read_segment(
                    page_no, cell_index, wi, first, first + take):
                out.append(self._decode(rowid, payload, pos, ref))
                pos += 1
            pos = self._starts[i] + count
            i += 1
        return out

    def iter_all(self):
        ordinal = 0
        if self.info.without_rowid:
            for payload, ref in self.reader.iter_index(self.info.root_page):
                yield self._decode(None, payload, ordinal, ref)
                ordinal += 1
        else:
            for rowid, payload, ref in self.reader.iter_table(self.info.root_page):
                yield self._decode(rowid, payload, ordinal, ref)
                ordinal += 1

    def find(self, locator, scan=True):
        """Row for a locator: its recorded cell, then a rowid seek, then (scan=True) a full scan."""
        if locator.cell is not None:
            try:
                rowid, payload, ref = self.reader.read_cell(*locator.cell)
                return self._decode(rowid, payload, -1, ref)
            except Exception as e:
                if self.issues is not None:
                    self.issues.add("bad_cell", str(e), "page %d offset %d" % locator.cell)
        if locator.kind == "rowid" and not self.info.without_rowid:
            try:
                hit = self.reader.find_rowid(self.info.root_page, locator.value)
            except Exception:
                hit = None
            if hit is not None:
                return self._decode(hit[0], hit[1], -1)
        if not scan:
            return None
        for loc, row, flags in self.iter_all():
            if loc == locator:
                return loc, row, flags
        return None
