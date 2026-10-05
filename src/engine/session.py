"""Session: the single engine facade used by the UI.

Opens evidence read-only, picks a mode (IMMUTABLE / RAM_OVERLAY / MAIN_ONLY / NATIVE),
and serves tables either through SQL or through the native B-tree reader.
"""

import collections
import gc
import os
import queue
import sqlite3
import sys
import threading
import time
import weakref

from . import limits, locks, positions, uiyield
from .backends import (CONN_OP_LOCK, Filter, NativeTable, SqlBackend,
                       build_overlay_image, ram_overlay_supported)
from .evidence import EvidenceSet
from .fileformat.header import HeaderError
from .fileformat.pager import Pager, PageError
from .fileformat.record import InvalidText
from .fileformat.wal import WalCancelled, WalError, WalFile
from .issues import IssueLog
from .schema import HIDDEN_VIRTUAL_GEN, Locator, SchemaModel, column_affinity, quote_ident
from . import sqlsafe
from .sqlsafe import SQL_ERRORS
from .search import Matcher, blob_lower_works, match_row

IMMUTABLE, RAM_OVERLAY, MAIN_ONLY, NATIVE = "immutable", "ram-overlay", "main-only", "native"
DEFAULT_RAM_LIMIT = None        # the limit 'ram_overlay_bytes' (engine.limits)
CLOSE_WAIT = 3.0        # seconds close() waits for worker threads to let go of their connections


def default_search_workers():
    """Parallel table searches: one per core, at least 2 and at most 8."""
    return max(2, min(8, os.cpu_count() or 2))


class SessionError(Exception):
    """The file cannot be opened as an SQLite database (e.g. encrypted or not SQLite)."""


class Banner(object):
    """A status message: 'short' is a chip label, 'text' the full explanation."""
    __slots__ = ("level", "text", "short")

    def __init__(self, level, text, short=None):
        self.level, self.text = level, text
        self.short = short or text

    def __repr__(self):
        return "Banner(%s, %r)" % (self.level, self.text)


class Row(object):
    __slots__ = ("locator", "values", "flags")

    def __init__(self, locator, values, flags=()):
        self.locator, self.values, self.flags = locator, values, set(flags)


class Page(object):
    def __init__(self, columns, rows, source, capped=False, note=""):
        self.columns, self.rows, self.source = columns, rows, source
        self.capped, self.note = capped, note


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def is_interrupt(err):
    """True for the error of a statement cancelled with Connection.interrupt()."""
    return isinstance(err, sqlite3.OperationalError) and str(err).lower() == "interrupted"


def _closed_error():
    return sqlite3.ProgrammingError("Cannot operate on a closed database.")


def _interrupt_quietly(conn):
    with CONN_OP_LOCK:          # never while another thread closes it (see backends)
        try:
            conn.interrupt()
        except sqlite3.Error:
            pass                # already closed: nothing is running on it


def _close_quietly(conn):
    with CONN_OP_LOCK:
        try:
            conn.close()
        except Exception:       # noqa: BLE001 - one connection must not stop the rest of close()
            pass


def sort_key(v):
    """SQLite ordering across storage classes: NULL < numbers < text < blob."""
    if v is None:
        return (0, 0)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (1, v)
    if isinstance(v, InvalidText):
        return (2, v.decode("utf-8", "replace"))
    if isinstance(v, bytes):
        return (3, v)
    return (2, str(v))


class OpenCancelled(Exception):
    """Opening was stopped by the caller (cancel() turned true)."""


class Session(object):
    def __init__(self, path, ram_limit=DEFAULT_RAM_LIMIT, hash_evidence=True, safe_parse=False,
                 cancel=None, progress=None):
        """Open the evidence at `path` read-only.

        safe_parse=True: only the built-in parser reads the file; SQLite never opens it (mode
        NATIVE, `safe_parse_reason` says why). It is also chosen automatically when SQLite
        cannot read the file or the built-in check of the header and schema finds damage.
        cancel() -> True stops a long open (OpenCancelled); progress(text) is told what is
        being read."""
        self.issues = IssueLog(cap=limits.get("issues_kept"))
        self.evidence = EvidenceSet(path)
        # memory the in-RAM view of the WAL may take: the database image is built once and
        # SQLite copies it, so about twice the database size is needed
        self.ram_limit = limits.get("ram_overlay_bytes") if ram_limit is None else ram_limit
        self.wal = self.pager = self.sql = None
        self.wal_problem = None          # why an existing -wal file could not be used
        self.mode = None
        self.sql_error = None
        self.safe_parse = False
        self.safe_parse_reason = ""
        self._cancel = cancel
        self._progress = progress
        self._local = threading.local()
        self._owned = []                 # (connection, thread) for every conn() connection
        self._lent = []                  # new_connection() connections: their callers close them
        self._thread_conns = {}          # thread -> that thread's connection (idents get reused)
        self._conns_lock = threading.Lock()
        self._closed = False
        self._native = {}
        self._native_sorted = None       # (key, rows, capped) of the last filtered/sorted native read
        self._sql_failed = {}
        self._counts = {}
        self._positions = collections.OrderedDict()   # view key -> positions.PositionIndex
        self._pos_lock = threading.Lock()
        self._sql_lower = False          # SQL pre-filters may fold BLOB bytes with lower()
        if hash_evidence:
            self.evidence.start_hashing()
        try:
            self.wal = self._open_wal()
            try:
                self.pager = Pager(self.evidence.main, self.wal, issues=self.issues)
            except OSError as e:
                if locks.is_sharing_violation(e, self.evidence.main):
                    raise SessionError("Cannot read the database: %s"
                                       % locks.sharing_advice(self.evidence.main))
                raise SessionError("Not a readable SQLite database: %s" % e)
            except (HeaderError, PageError, ValueError) as e:
                raise SessionError("Not a readable SQLite database: %s" % e)
            self._step("schema")
            before = len(self.issues)
            self.schema = SchemaModel(SchemaModel.read_master(self.pager, self.issues), self.issues)
            if safe_parse:
                self._use_safe_parse("chosen when the database was opened")
            else:
                damage = self._precheck(before)
                if damage:
                    self._use_safe_parse("the built-in check found damage SQLite should not "
                                         "be given: %s" % damage)
                else:
                    self._step("SQLite")
                    self._choose_mode()
            # Checked once, on a UTF-8 database only: in UTF-16 lower() would transcode the bytes
            try:
                self._sql_lower = self.sql is not None and self.pager.encoding == "utf-8" and \
                    blob_lower_works(self.conn())
            except SQL_ERRORS:
                self._sql_lower = False
            self._step("columns")
            self.schema.describe_all(self.conn() if self.sql is not None else None)
            self._cross_check_schema()
        except BaseException:
            self._release()     # never leak mmaps, connections or the hashing thread
            raise

    @classmethod
    def open(cls, path, ram_limit=DEFAULT_RAM_LIMIT, hash_evidence=True, safe_parse=False,
             cancel=None, progress=None):
        return cls(path, ram_limit=ram_limit, hash_evidence=hash_evidence,
                   safe_parse=safe_parse, cancel=cancel, progress=progress)

    # -- setup -------------------------------------------------------------
    def _step(self, what):
        if self._cancel is not None and self._cancel():
            raise OpenCancelled("opening stopped")
        if self._progress is not None:
            self._progress("reading the %s" % what)

    def _open_wal(self):
        p = self.evidence.path("wal")
        if not p:
            return None
        try:
            if os.path.getsize(p) == 0:
                return None        # an empty -wal is normal after a checkpoint: nothing to apply
            if self._progress is not None:
                self._progress("reading the WAL")
            wal = WalFile(p, cancel=self._cancel, max_frames=limits.get("wal_frames"),
                          progress=self._progress)
        except WalCancelled:
            raise OpenCancelled("opening stopped while the WAL was read")
        except (WalError, OSError) as e:
            self.wal_problem = (locks.sharing_advice(p) if isinstance(e, OSError)
                                and locks.is_sharing_violation(e, p) else str(e))
            self.issues.add("wal_unreadable", self.wal_problem, p, "error")
            return None
        if wal.frames_cut:
            self.issues.add("wal_frames_cut",
                            "the WAL holds %s frames; only the first %s were read (%s): later "
                            "commits are not applied" % (format(wal.frames_total, ","),
                                                         format(len(wal.frames), ","),
                                                         limits.hint("wal_frames")),
                            p, "warning")
        return wal

    def _precheck(self, before):
        """Damage the built-in parser found in the header or the schema that SQLite should not
        be given ('' when there is none): a declared size far beyond the file, or damaged
        schema pages."""
        bad = []
        pg = self.pager
        if pg.declared_page_count > pg.page_count:
            bad.append("the database declares %s pages (header or WAL), the files hold at most %s"
                       % (format(pg.declared_page_count, ","), format(pg.page_count, ",")))
        kinds = set(i.kind for i in self.issues.items[before:])
        serious = kinds & set(("bad_schema_record", "bad_cell_pointer", "overflow_truncated",
                               "bad_cell", "payload_cut", "payload_clamped", "btree_loop",
                               "bad_page"))
        if serious:
            bad.append("damaged schema pages (%s)" % ", ".join(sorted(serious)))
        return "; ".join(bad)

    def _use_safe_parse(self, why):
        self.safe_parse = True
        self.safe_parse_reason = why
        self.mode = NATIVE
        self.sql_error = "Safe parse: SQLite is not used (%s)" % why
        self.issues.add("safe_parse", "only the built-in parser reads this file; SQLite never "
                        "opens it (%s)" % why, "", "info")

    def _probe(self, backend):
        backend.issues = self.issues
        backend.view_names = frozenset(e.name.lower() for e in self.schema.entries
                                       if e.type == "view")
        conn = None
        try:
            conn = backend.connect()
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except SQL_ERRORS as e:
            # SQLite refused the file: release what the probe opened (its error may not even be
            # an sqlite3.Error: a message that is not UTF-8, SQLITE_NOMEM as MemoryError, ...)
            if conn is not None:
                _close_quietly(conn)
            backend.close()
            conn = None
            self.sql_error = sqlsafe.describe_error(e)
            self.issues.add("sqlite_open_failed", self.sql_error, backend.label, "error")
            return False
        self.sql = backend
        self._register(conn)
        return True

    def _register(self, conn):
        """Make `conn` this thread's connection (the thread that uses it is its owner)."""
        with self._conns_lock:
            if self._closed:
                conn.close()        # nobody else has seen it yet
                raise _closed_error()
            self._owned.append((conn, threading.current_thread()))
            self._thread_conns[threading.current_thread()] = conn
            self._local.conn = conn

    def _choose_mode(self):
        collations = self.schema.collations
        has_current = self.wal is not None and bool(self.wal.overlay)
        if not has_current:
            self.mode = IMMUTABLE if self._probe(SqlBackend.for_file(self.evidence.main, collations)) \
                else NATIVE
            return
        image = self.pager.page_count * self.pager.page_size
        if ram_overlay_supported() and 2 * image <= self.ram_limit:
            try:
                backend = SqlBackend.for_image(build_overlay_image(self.pager), collations)
            except Exception as e:
                self.issues.add("ram_overlay_failed", str(e), "", "warning")
                backend = None
            if backend is not None and self._probe(backend):
                self.mode = RAM_OVERLAY
                return
        self.mode = MAIN_ONLY if self._probe(SqlBackend.for_file(self.evidence.main, collations)) \
            else NATIVE

    def _cross_check_schema(self):
        if self.sql is None:
            return
        try:
            rows = self.conn().execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()
        except SQL_ERRORS:
            return
        sql_names = set(r[0] for r in rows)
        native_names = set(self.schema.names())
        if self.mode in (IMMUTABLE, RAM_OVERLAY) and sql_names != native_names:
            self.issues.add("schema_mismatch",
                            "SQLite and native schema differ: sql-only %s, native-only %s"
                            % (sorted(sql_names - native_names), sorted(native_names - sql_names)))

    # -- connections -------------------------------------------------------
    def conn(self):
        """This thread's read-only SQL connection, or None when SQL is unavailable."""
        if self.sql is None:
            return None
        c = getattr(self._local, "conn", None)
        if c is None:
            if self._closed:
                raise _closed_error()
            c = self.sql.connect()
            self._register(c)
        return c

    def new_connection(self):
        """A fresh read-only SQL connection for the caller (SQL tab), or None.

        The caller owns it: interrupt() and close() interrupt it, but only the caller closes it,
        with release_connection() (normally in a `finally` on the thread that uses it).
        """
        if self.sql is None:
            return None
        if self._closed:
            raise _closed_error()
        c = self.sql.connect()
        with self._conns_lock:
            if self._closed:
                c.close()
                raise _closed_error()
            self._lent.append(c)
        return c

    def release_connection(self, conn):
        """Close a connection obtained from new_connection() (also after close())."""
        with self._conns_lock:
            if conn in self._lent:
                self._lent.remove(conn)
        with CONN_OP_LOCK:
            conn.close()

    def release_thread_connection(self):
        """Close this thread's conn() connection: for a worker thread that is about to end.

        Only the owning thread may call this, so nothing else can be running on it.
        """
        c = getattr(self._local, "conn", None)
        if c is None:
            return
        self._local.conn = None
        with self._conns_lock:
            self._owned = [(oc, ow) for oc, ow in self._owned if oc is not c]
            if self._thread_conns.get(threading.current_thread()) is c:
                del self._thread_conns[threading.current_thread()]
        _close_quietly(c)

    def interrupt(self, thread=None):
        """Cancel running SQL on every connection, or only on the one `thread` uses.

        The statement fails with sqlite3.OperationalError('interrupted'); the session treats
        that as a cancellation and never as a reason to read the table natively. A connection
        with nothing running is not affected.
        """
        with self._conns_lock:
            if thread is None:
                targets = [c for c, _owner in self._owned] + self._lent
            else:
                c = self._thread_conns.get(thread)
                targets = [c] if c is not None else []
        for c in targets:
            _interrupt_quietly(c)

    @property
    def sql_sees_current_state(self):
        return self.mode in (IMMUTABLE, RAM_OVERLAY)

    def main_only_reason(self):
        """Why SQL sees the main file only (main-only mode), in words with what to change;
        '' in any other mode."""
        if self.mode != MAIN_ONLY:
            return ""
        if not ram_overlay_supported():
            return ("Python %d.%d / SQLite %s cannot build the in-memory image (Python 3.11 or "
                    "later can)." % (sys.version_info[0], sys.version_info[1],
                                     sqlite3.sqlite_version))
        image = self.pager.page_count * self.pager.page_size
        return ("The in-memory view of the WAL would need about %s (twice the database's %s), "
                "more than the limit ram_overlay_bytes (%s): raise it in Limits… and open the "
                "database again." % (_fmt_bytes(2 * image), _fmt_bytes(image),
                                     _fmt_bytes(self.ram_limit)))

    # -- schema helpers ----------------------------------------------------
    def info(self, name):
        t = self.schema.get(name)
        if t is None:
            raise KeyError(name)
        return t

    def tables(self):
        return self.schema.names("table") + self.schema.names("virtual")

    def views(self):
        return self.schema.names("view")

    def visible_columns(self, name):
        return [c.name for c in self.info(name).columns if c.hidden != 1]

    def source(self, name):
        """'sql', 'native' or 'unavailable' for this table/view."""
        t = self.info(name)
        if self.sql_sees_current_state and name not in self._sql_failed:
            if t.kind == "table" and not t.without_rowid and t.rowid_name is None:
                return "native"      # every rowid alias is shadowed by a real column
            if self._unbounded_computed(t):
                return "native"      # see _unbounded_computed
            return "sql"
        return "native" if t.natively_readable else "unavailable"

    @staticmethod
    def _computed(t):
        """True for a view or a table with VIRTUAL generated columns: SQLite computes values
        on read, from expressions the evidence chose, so the read is held to the step budget."""
        return t.kind == "view" or any(c.hidden == HIDDEN_VIRTUAL_GEN for c in t.columns)

    @staticmethod
    def _unbounded_computed(t):
        """True for a table whose computed columns call a function that can build a value of
        any size, on a Python that cannot cap value sizes (3.8-3.10): read natively, where
        those columns show as NULL (the page note says so)."""
        return (t.kind == "table" and not sqlsafe.HAS_SETLIMIT and t.natively_readable
                and any(c.hidden == HIDDEN_VIRTUAL_GEN for c in t.columns)
                and sqlsafe.risky_sql(t.sql))

    def _guarded(self, t):
        """Context for reading table/view t on this thread's connection (sqlsafe.guarded)."""
        return sqlsafe.guarded(self.conn(), self._computed(t))

    def _native_table(self, name):
        nt = self._native.get(name)
        if nt is None:
            nt = self._native[name] = NativeTable(self.info(name), self.pager, self.issues)
        return nt

    def _sql_failed_on(self, name, err):
        """Record that SQLite failed on a table, which is then read natively. An error because
        the session was closed under the read is re-raised: the read stops, it is no failure."""
        if self._closed:
            raise err
        text = sqlsafe.describe_error(err)
        self._sql_failed[name] = text
        self.issues.add("sql_table_failed", text, name, "warning")

    def _select(self, t):
        """(select_list, lead) where lead = number of locator columns before the data columns."""
        cols = ", ".join(quote_ident(c) for c in self.visible_columns(t.name))
        if t.kind == "table" and not t.without_rowid:
            # rowid_name is always one of the literal aliases in schema.ROWID_ALIASES (rowid, _rowid_, oid),
            # never a user column name, so it is safe to interpolate without quoting.
            return "%s, %s" % (t.rowid_name, cols), 1
        if t.kind == "table":
            return cols, 0
        return "*", 0

    def _locator(self, t, lead_values, values, ordinal, columns):
        if t.kind == "table" and not t.without_rowid:
            return Locator("rowid", lead_values[0])
        if t.kind == "table":
            return Locator("pk", tuple(values[i] for i in t.pk_columns))
        # Views / virtual tables have no key: the ordinal is only a position in this particular
        # (possibly sorted or filtered) read, so the locator carries the row itself.
        return Locator("ordinal", ordinal, snapshot=(columns, values))

    @staticmethod
    def _reverse_natural_order(t):
        """ORDER BY terms giving the reverse of the natural order ('_rid DESC', newest first):
        rowid descending, or each PRIMARY KEY column against its index direction for WITHOUT
        ROWID tables. '' for views and virtual tables, which have no natural key order."""
        if t.kind != "table":
            return ""
        if not t.without_rowid:
            return "%s DESC" % t.rowid_name      # a literal alias from ROWID_ALIASES
        terms = []
        for ci, (descending, coll) in zip(t.pk_columns, t.pk_index_order):
            terms.append("%s%s %s" % (quote_ident(t.columns[ci].name),
                                      " COLLATE " + quote_ident(coll) if coll else "",
                                      "ASC" if descending else "DESC"))
        return ", ".join(terms)

    @staticmethod
    def _result_columns(t, cur, lead, cols):
        """Column names of a SELECT result: the view's own names, or the table's columns."""
        return [d[0] for d in cur.description][lead:] if t.kind != "table" else cols

    @property
    def encoding(self):
        """Text encoding of the database ('utf-8', 'utf-16-le' or 'utf-16-be')."""
        return self.pager.encoding

    # -- data access -------------------------------------------------------
    def count(self, name, flt=None):
        """Row count, or with a Filter the number of rows browse() can show for it (natively
        read tables: matches among the first limits 'native_sort_rows' rows)."""
        if flt:
            return self._count_filtered(name, flt)
        if name in self._counts:
            return self._counts[name]
        src = self.source(name)
        n = None
        if src == "sql":
            try:
                with self._guarded(self.info(name)):
                    n = self.conn().execute("SELECT COUNT(*) FROM %s"
                                            % quote_ident(name)).fetchone()[0]
            except SQL_ERRORS as e:
                if is_interrupt(e) or not self.info(name).natively_readable:
                    raise
                self._sql_failed_on(name, e)
                src = "native"
        if src == "native":
            n = self._native_table(name).count()
        if n is not None:
            self._counts[name] = n
        return n

    def _count_filtered(self, name, flt):
        t = self.info(name)
        src = self.source(name)
        if src == "unavailable":
            return 0
        if src == "sql":
            where, params = flt.where_sql(self.visible_columns(name), self.encoding)
            if not where:
                return self.count(name)
            fk = flt.key()
            with self._pos_lock:
                done = [i.count for k, i in self._positions.items()
                        if k[0] == name and k[3] == fk and i.complete and i.error is None]
            if done:
                return done[0]          # a position index of this filter counted its rows
            try:
                with self._guarded(t):
                    return self.conn().execute("SELECT COUNT(*) FROM %s WHERE %s"
                                               % (quote_ident(name), where), params).fetchone()[0]
            except SQL_ERRORS as e:
                if is_interrupt(e) or not t.natively_readable:
                    raise
                self._sql_failed_on(name, e)
        return len(self._native_matches(t, None, False, flt)[0])

    def browse(self, name, offset=0, limit=200, order_by=None, desc=False, flt=None):
        t = self.info(name)
        src = self.source(name)
        if src == "unavailable":
            return Page(self.visible_columns(name), [], src,
                        note="%s '%s' cannot be read without SQLite (%s)"
                        % (t.kind, name, self.sql_error or "SQLite unavailable"))
        if src == "sql":
            try:
                with self._guarded(t):
                    return self._browse_sql(t, offset, limit, order_by, desc, flt)
            except SQL_ERRORS as e:
                if is_interrupt(e):
                    raise
                if not t.natively_readable:
                    return Page(self.visible_columns(name), [], "error",
                                note="%s '%s': %s" % (t.kind, name, sqlsafe.describe_error(e)))
                self._sql_failed_on(name, e)
        return self._browse_native(t, offset, limit, order_by, desc, flt)

    def _where_sql(self, t, flt):
        """(where, params) of a Filter on a table or view ('' when it selects every row)."""
        if not flt:
            return "", []
        where, params = flt.where_sql(self.visible_columns(t.name), self.encoding)
        return where or "", list(params)

    def _select_sql(self, t, flt, order_by, desc):
        """(sql, params, lead) reading a table or view with an optional Filter and order.

        Tables are always read in a total order: sorted by the column, ties by rowid (or
        primary key) the same way round; unsorted by rowid (or primary key). Windows read at
        any offset then fit together, and match the windows a PositionIndex serves."""
        select, lead = self._select(t)
        sql = "SELECT %s FROM %s" % (select, quote_ident(t.name))
        where, params = self._where_sql(t, flt)
        if where:
            sql += " WHERE " + where
        plan = positions.order_plan(t, order_by, desc)
        if plan is not None:
            sql += " ORDER BY " + plan.order_sql
        elif order_by:
            sql += " ORDER BY %s %s" % (quote_ident(order_by), "DESC" if desc else "ASC")
        elif desc and self._reverse_natural_order(t):
            sql += " ORDER BY " + self._reverse_natural_order(t)
        return sql, params, lead

    def _browse_sql(self, t, offset, limit, order_by, desc, flt):
        select, lead = self._select(t)
        cols = self.visible_columns(t.name)
        idx = self._ready_positions(t.name, order_by, desc, flt)
        if idx is not None:
            where, params = self._where_sql(t, flt)
            conn = self.conn()
            raw = idx.window(offset, limit, select, where, params,
                             lambda sql, ps: conn.execute(sql, ps).fetchall(), lead)
            if raw is not None:
                rows = []
                for r in raw:
                    values = list(r[lead:])
                    rows.append(Row(self._locator(t, r[:lead], values, 0, cols), values))
                return Page(cols, rows, "sql", note=self._positions_note(idx))
        sql, params, lead = self._select_sql(t, flt, order_by, desc)
        cur = self.conn().execute(sql + " LIMIT ? OFFSET ?", params + [limit, offset])
        cols = self._result_columns(t, cur, lead, cols)
        fetched, cut = self._fetch_window(cur, limit, self._computed(t))
        rows = []
        for i, r in enumerate(fetched):
            values = list(r[lead:])
            rows.append(Row(self._locator(t, r[:lead], values, offset + i, cols), values))
        note = "; ".join(n for n in (self._positions_note(idx), cut) if n)
        return Page(cols, rows, "sql", capped=bool(cut), note=note)

    @staticmethod
    def _fetch_window(cur, limit, computed):
        """(rows, note) of one grid window. Values SQLite computes (views, generated columns)
        are kept up to limits 'sql_window_bytes' in all; the note says when the window stopped
        there (the cursor is closed)."""
        if not computed:
            return cur.fetchall(), ""
        most = limits.get("sql_window_bytes")
        rows, size = [], 0
        while len(rows) < limit:
            chunk = cur.fetchmany(1)       # one row at a time: never more than one past the cap
            if not chunk:
                return rows, ""
            for r in chunk:
                size += sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in r)
                rows.append(r)
            if size > most:
                cur.close()
                return rows, ("stopped after %d rows: their computed values take more than "
                              "%s bytes (%s)" % (len(rows), format(most, ","),
                                                   limits.hint("sql_window_bytes")))
        return rows, ""

    # -- position indexes -------------------------------------------------------------
    @staticmethod
    def _positions_key(name, order_by, desc, flt):
        return (name, order_by or None, bool(desc), flt.key() if flt else None)

    def _ready_positions(self, name, order_by, desc, flt):
        """The PositionIndex of this view, complete or being built, or None."""
        key = self._positions_key(name, order_by, desc, flt)
        with self._pos_lock:
            idx = self._positions.get(key)
            if idx is not None:
                self._positions.move_to_end(key)
        return idx if idx is not None and idx.error is None else None

    @staticmethod
    def _positions_note(idx):
        if idx is not None and idx.map_skipped:
            return ("more than %s rows in this sorted/filtered view: scrolling far reads more "
                    "slowly (raise %s)" % (format(idx.map_limit, ","),
                                           limits.hint("position_map_rows")))
        return ""

    def positions_info(self, name, order_by=None, desc=False, flt=None):
        """{'complete', 'rows', 'checkpoints', 'every', 'mapped', 'bytes', 'usable'} of the
        PositionIndex of a view, or None when there is none."""
        idx = self._ready_positions(name, order_by, desc, flt)
        if idx is None:
            return None
        return {"complete": idx.complete, "rows": idx.count,
                "checkpoints": idx.checkpoint_count(), "every": idx.every,
                "mapped": len(idx.dense) if idx.dense is not None else 0,
                "bytes": idx.memory_bytes(), "usable": idx.usable,
                "served": idx.served, "missed": idx.missed}

    def build_positions(self, name, order_by=None, desc=False, flt=None, cancel=None):
        """Build (or return) the PositionIndex of a table view so browse() windows are fast at
        any position; returns the number of rows in the view, or None when no index applies
        (views, natively read tables - whose sorted/filtered rows are read into memory here
        instead - and SQLite older than needed). Slow: call it on a worker thread. cancel()
        -> True, interrupt() or close() stop it; a stopped build is dropped."""
        t = self.info(name)
        src = self.source(name)
        if src == "native":
            if order_by or flt:
                rows, _capped = self._native_matches(t, order_by, desc, flt)
                return len(rows)
            return None
        if src != "sql":
            return None
        plan = positions.order_plan(t, order_by, desc)
        if plan is None:
            return None
        key = self._positions_key(name, order_by, desc, flt)
        with self._pos_lock:
            idx = self._positions.get(key)
            if idx is not None and idx.complete and idx.error is None:
                self._positions.move_to_end(key)
                return idx.count
        lim = limits.current()
        total = self._counts.get(name)
        every = lim["checkpoint_every"]
        if total:
            every = max(every, -(-total // lim["checkpoints_max"]))
        where, params = self._where_sql(t, flt)
        idx = positions.PositionIndex(key, quote_ident(name), plan, every, lim["checkpoints_max"],
                                      lim["position_map_rows"], plan.rowid is not None)
        with self._pos_lock:
            self._positions[key] = idx
            self._positions.move_to_end(key)
            while len(self._positions) > lim["position_indexes_kept"]:
                self._positions.popitem(last=False)
        conn = self.conn()

        def explain(sql, ps):
            return [str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, ps)]

        def stop():
            return self._closed or (cancel is not None and cancel())
        try:
            done = idx.build(lambda sql, ps: conn.execute(sql, ps), where, params, explain, stop)
        except BaseException as e:
            idx.error = str(e) or type(e).__name__
            self._drop_positions(key, idx)
            raise
        if not done:
            idx.error = "cancelled"
            self._drop_positions(key, idx)
            return None
        if idx.dense is not None and not flt and all(idx.seekable):
            idx.dense = None            # checkpoints seek as fast: keep only those
        if not flt:
            self._counts.setdefault(name, idx.count)
        return idx.count

    def _drop_positions(self, key, idx):
        with self._pos_lock:
            if self._positions.get(key) is idx:
                del self._positions[key]

    def _native_matches(self, t, order_by, desc, flt):
        """(rows, capped): the rows of a natively read table that pass `flt`, in the requested
        order, from its first limits 'native_sort_rows' rows. Ties keep the natural order, and
        descending the reverse of it, as SQL reads (see _select_sql). The latest result is
        kept: a grid scrolling through a sorted or filtered table asks for it window by
        window."""
        cap = limits.get("native_sort_rows")
        key = (t.name, order_by, bool(desc), flt.key() if flt else None, cap)
        hit = self._native_sorted
        if hit is not None and hit[0] == key:
            return hit[1], hit[2]
        nt = self._native_table(t.name)
        cols = t.column_names
        enc = self.encoding
        matched, capped = [], False
        for n, (loc, row, flags) in enumerate(nt.iter_all()):
            if n >= cap:
                capped = True
                break
            if not flt or flt.matches(cols, row, enc):
                matched.append(Row(loc, row, flags))
        if order_by and order_by in cols:
            ci = cols.index(order_by)
            if desc:
                matched.reverse()       # a stable sort keeps ties in reverse natural order
            matched.sort(key=lambda r: sort_key(r.values[ci]), reverse=bool(desc))
        elif desc and not order_by:
            matched.reverse()           # natural order, newest first
        self._native_sorted = (key, matched, capped)
        return matched, capped

    def _browse_native(self, t, offset, limit, order_by, desc, flt):
        nt = self._native_table(t.name)
        cols = t.column_names
        if not order_by and not flt:
            if desc:
                total = nt.count()
                start = max(0, total - offset - limit)
                got = nt.rows(start, max(0, total - offset - start))
                got.reverse()
            else:
                got = nt.rows(offset, limit)
            return Page(cols, [Row(l, r, f) for l, r, f in got], "native",
                        note=self._native_note(t, nt))
        matched, capped = self._native_matches(t, order_by, desc, flt)
        note = ("native mode: sort/filter limited to the first %s rows (raise %s)"
                % (format(limits.get("native_sort_rows"), ","), limits.hint("native_sort_rows"))
                if capped else "")
        nt.count()
        return Page(cols, matched[offset:offset + limit], "native", capped,
                    self._native_note(t, nt, note))

    @staticmethod
    def _native_note(t, nt, *extra):
        """Page note for a natively read table: caps, uncomputable columns, read problems."""
        parts = list(extra)
        vgen = [c.name for c in t.columns if c.hidden == HIDDEN_VIRTUAL_GEN]
        if vgen:
            parts.append("VIRTUAL generated column(s) %s are computed by SQLite, not stored: "
                         "shown as NULL when read natively" % ", ".join(vgen))
        parts.append(nt.problem)
        return "; ".join(p for p in parts if p)

    def row(self, name, locator, scan=True):
        """Full row for a locator, or None.

        An ordinal locator (view, virtual table) returns the row it carries: re-querying by
        position would return a different row once the read was sorted or filtered.
        scan=False: a natively read table is not scanned for a row its key seek misses (as a
        miss on an SQL-served table is not), for callers that look up many keys that may be
        absent.
        """
        t = self.info(name)
        if locator.kind == "ordinal" and locator.snapshot is not None:
            return Row(locator, list(locator.snapshot[1]))
        src = self.source(name)
        if src == "sql":
            try:
                with self._guarded(t):
                    hit = self._row_sql(t, locator)
                if hit is not None or not t.natively_readable or locator.kind == "ordinal":
                    return hit
                # SQLite's key seek can miss rows in a damaged tree that the native reader still
                # reaches through the row's recorded cell or its own rowid seek. A row that is
                # simply absent (deleted, or only in older WAL data) is not an issue and must not
                # cost a full table scan.
                found = self._native_table(name).find(locator, scan=False)
                if found is None:
                    return None
                self.issues.add("sql_seek_missed", "SQLite could not seek %s; used native lookup"
                                % locator.display(), name)
                return Row(*found)
            except SQL_ERRORS as e:
                if is_interrupt(e):
                    raise
                if not t.natively_readable:
                    return None
                self._sql_failed_on(name, e)
        if not t.natively_readable:
            return None
        hit = self._native_table(name).find(locator, scan=scan)
        return Row(*hit) if hit else None

    def _row_sql(self, t, locator):
        select, lead = self._select(t)
        base = "SELECT %s FROM %s" % (select, quote_ident(t.name))
        if locator.kind == "rowid":
            # rowid_name is always one of the literal aliases in schema.ROWID_ALIASES (rowid, _rowid_, oid),
            # never a user column name, so it is safe to interpolate without quoting.
            cur = self.conn().execute(base + " WHERE %s = ?" % t.rowid_name, (locator.value,))
        elif locator.kind == "pk":
            where = " AND ".join("%s = ?" % quote_ident(t.columns[i].name) for i in t.pk_columns)
            cur = self.conn().execute(base + " WHERE " + where, tuple(locator.value))
        else:
            # An ordinal without a snapshot is a position in the natural (unsorted) order.
            cur = self.conn().execute(base + " LIMIT 1 OFFSET ?", (locator.value,))
        r = cur.fetchone()
        if r is None:
            return None
        values = list(r[lead:])
        cols = self._result_columns(t, cur, lead, self.visible_columns(t.name))
        return Row(self._locator(t, r[:lead], values,
                                 locator.value if locator.kind == "ordinal" else 0, cols), values)

    def _sql_rows(self, t, lead, sql, params, chunk_size=2000):
        """Execute SQL and yield Row objects chunk by chunk. Does not handle errors; caller must catch.

        SQLite only steps as far as a fetch asks: a caller that needs few rows (a search with a
        row limit) passes a small chunk_size so the scan stops once it has them.

        The cursor is closed here, on the thread that ran it, however the read ends: a cursor
        left to be freed later may be freed on another thread (an error's traceback keeps it),
        and freeing it while its connection is being closed crashes Python <= 3.10.
        """
        with self._guarded(t):
            cur = self.conn().execute(sql, params)
            try:
                cols = self._result_columns(t, cur, lead, self.visible_columns(t.name))
                i = 0
                while True:
                    uiyield.pause()             # the user interface first while it is busy
                    chunk = cur.fetchmany(chunk_size)
                    if not chunk:
                        return
                    for r in chunk:
                        values = list(r[lead:])
                        yield Row(self._locator(t, r[:lead], values, i, cols), values)
                        i += 1
            finally:
                with CONN_OP_LOCK:
                    try:
                        cur.close()
                    except sqlite3.Error:
                        pass            # its connection was closed already

    def iter_rows(self, name, flt=None, order_by=None, desc=False):
        """Stream every row as Row objects (used by search and exports); with a Filter and/or
        an order, the rows browse() shows for them, in that order."""
        if flt or order_by or desc:
            for row in self._iter_selected(name, flt, order_by, desc):
                yield row
            return
        t = self.info(name)
        done = 0
        if self.source(name) == "sql":
            select, lead = self._select(t)
            sql = "SELECT %s FROM %s" % (select, quote_ident(name))
            try:
                for row in self._sql_rows(t, lead, sql, []):
                    yield row
                    done += 1
                return  # Clean SQL run, do not fall through to native
            except SQL_ERRORS as e:
                if is_interrupt(e) or not t.natively_readable:
                    raise
                self._sql_failed_on(name, e)
                if done > 0:
                    self.issues.add("sql_scan_resumed",
                                    "SQLite failed after %d rows; the remaining rows come from the native reader"
                                    % done, name)
        if t.natively_readable:
            skipped = 0
            n = 0
            for loc, row, flags in self._native_table(name).iter_all():
                n += 1
                if n % 1000 == 0:
                    uiyield.pause()
                if skipped < done:
                    skipped += 1
                    continue
                yield Row(loc, row, flags)

    def _iter_selected(self, name, flt, order_by, desc):
        t = self.info(name)
        src = self.source(name)
        if src == "unavailable":
            return
        seen = set()
        if src == "sql":
            sql, params, lead = self._select_sql(t, flt, order_by, desc)
            try:
                for row in self._sql_rows(t, lead, sql, params):
                    seen.add(row.locator)
                    yield row
                return
            except SQL_ERRORS as e:
                if is_interrupt(e) or not t.natively_readable:
                    raise
                self._sql_failed_on(name, e)
                if seen:
                    self.issues.add("sql_scan_resumed",
                                    "SQLite failed after %d rows; the remaining rows come from "
                                    "the native reader (in its order)" % len(seen), name)
        if not order_by and not desc:
            # natural order: stream the whole table, however large
            cols, enc = t.column_names, self.encoding
            for loc, row, flags in self._native_table(name).iter_all():
                if loc not in seen and (not flt or flt.matches(cols, row, enc)):
                    yield Row(loc, row, flags)
            return
        rows, capped = self._native_matches(t, order_by, desc, flt)
        if capped:
            self.issues.add("native_sort_capped", "sorted rows read natively are limited to the "
                            "first %d rows of the table (raise %s)"
                            % (limits.get("native_sort_rows"), limits.hint("native_sort_rows")),
                            name)
        for row in rows:
            if row.locator not in seen:
                yield row

    def lookup(self, name, column, expr, limit=200, count=True, cancel=None):
        """(count, rows, source): the rows of a table whose `column` matches the filters.Expr
        `expr` - at most `limit` of them, in natural order - and how many match in all
        (count=False: only the rows). An '=' comparison is also written as a plain
        'column = ?' in SQL, so SQLite can seek an index on the column; the exact test that
        follows it keeps the answer equal to the native one, which tests every row with
        expr.match(). cancel() -> True stops a native scan early."""
        t = self.info(name)
        src = self.source(name)
        if src == "unavailable":
            return 0, [], src
        flt = Filter(col_exprs={column: expr})
        if src == "sql":
            try:
                return self._lookup_sql(t, column, expr, limit, count) + ("sql",)
            except SQL_ERRORS as e:
                if is_interrupt(e) or not t.natively_readable:
                    raise
                self._sql_failed_on(name, e)
        cols, enc = t.column_names, self.encoding
        n, rows = 0, []
        for i, (loc, row, flags) in enumerate(self._native_table(name).iter_all()):
            if cancel is not None and i % 1000 == 0 and cancel():
                break
            if flt.matches(cols, row, enc):
                n += 1
                if n <= limit:
                    rows.append(Row(loc, row, flags))
                elif not count:
                    break
        return (n if count else len(rows)), rows, "native"

    def _lookup_sql(self, t, column, expr, limit, count):
        frag, params = expr.sql(column, self.encoding)
        if expr.kind == "cmp" and expr.op == "=" and self._seekable(t, column, expr.operand[0]):
            # Every row the exact test keeps also passes this one (the column's affinity and
            # collation only widen '='), and SQLite can answer it from an index.
            frag = "%s = ? AND %s" % (quote_ident(column), frag)
            params = [expr.operand[1]] + list(params)
        select, lead = self._select(t)
        base = "FROM %s WHERE %s" % (quote_ident(t.name), frag)
        rows = list(self._sql_rows(t, lead, "SELECT %s %s LIMIT ?" % (select, base),
                                   list(params) + [limit], chunk_size=max(1, min(limit, 2000))))
        n = len(rows)
        if count and n >= limit:
            with self._guarded(t):
                n = self.conn().execute("SELECT COUNT(*) " + base, params).fetchone()[0]
        return n, rows

    @staticmethod
    def _seekable(t, column, kind):
        """True when 'column = operand' cannot miss a row the exact comparison keeps: the
        column's affinity leaves an operand of this kind as it is."""
        if kind == "blob":
            return True
        decl = next((c.decl_type for c in t.columns if c.name.lower() == column.lower()), "")
        aff = column_affinity(decl)
        if kind == "num":
            return aff in ("INTEGER", "REAL", "NUMERIC")
        return aff in ("TEXT", "BLOB")

    def matcher(self, term, mode, deep_blob=False, decoded=False):
        """The search rule for a term (engine.search.Matcher) in this database's text encoding.
        Raises ValueError for a malformed hex pattern, re.error for a bad regex and
        DecodedSearchUnavailable when decoded content cannot be searched."""
        return Matcher(term, mode, deep_blob, decoded, self.pager.encoding,
                       sql_lower=self._sql_lower)

    def search(self, name, term, mode, limit=500, deep_blob=False, cancel=None, decoded=False,
               matcher=None):
        """Yield hit dicts {table, column, locator, rowid, value, type, encoding, offset, row},
        one per matching cell, for at most `limit` matching rows (every hit of a row is yielded;
        None: every matching row).
        'row' is the whole row, in visible_columns() order; 'encoding' and 'offset' say how and
        where a BLOB matched (see engine.search). decoded=True also searches the strings found
        in decoded BLOB content. matcher: a rule object used instead of the term and mode (it
        has Matcher's skip_column, sql_where and cell_hit; see engine.value_search)."""
        t = self.info(name)
        cols = self.visible_columns(name)
        types = [c.decl_type for c in t.columns if c.hidden != 1]
        if mode == "col":
            for c in cols:
                if term.lower() in c.lower():
                    yield {"table": name, "column": c, "locator": None, "rowid": "-",
                           "value": c, "type": "column_name", "encoding": "text", "offset": None}
            return
        if matcher is None:
            matcher = self.matcher(term, mode, deep_blob, decoded)
        hits = []
        found = 0
        rows = self._search_rows(t, cols, types, matcher,
                                 chunk_size=2000 if limit is None else max(1, min(2000, limit)))
        while True:
            try:
                row = next(rows)
            except StopIteration:
                return
            except sqlite3.OperationalError as e:
                # The caller interrupted the running statement to cancel the search.
                if is_interrupt(e) and cancel is not None and cancel():
                    return
                raise
            if cancel is not None and cancel():
                return
            del hits[:]
            match_row(name, cols, types, row.locator, row.values, matcher, hits.append)
            if not hits:
                continue
            for h in hits:
                h["row"] = row.values
                yield h
            found += 1
            if limit is not None and found >= limit:
                return

    def search_tables(self, names, term, mode, limit=500, deep_blob=False, cancel=None,
                      workers=None, decoded=False, matcher=None):
        """Search several tables at once; yield (name, hits, error) as each table finishes.

        Each table is searched by search() on one of `workers` threads, each with its own
        read-only connection: SQLite runs a scan without holding Python's GIL, so tables are
        scanned in parallel. Results come in completion order; a table's hits keep their order.
        When cancel() turns true the running statements are interrupted and nothing more is
        yielded. Every worker closes its own connection before it ends.

        A term the mode cannot search (malformed hex pattern or regex, decoded content without
        engine.decode) raises before any table is searched. matcher: see search().
        """
        if mode != "col" and matcher is None:
            self.matcher(term, mode, deep_blob, decoded)
        todo = queue.Queue()
        # the largest tables first (row counts known so far): a large table started last
        # would leave the other workers idle while it alone is scanned
        for n in sorted(names, key=lambda n: -(self._counts.get(n) or 0)):
            todo.put(n)
        done = queue.Queue()
        stop = threading.Event()
        count = min(len(names), workers or default_search_workers())
        if count == 0:
            return

        def work():
            try:
                while not stop.is_set():
                    try:
                        name = todo.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        done.put((name, list(self.search(name, term, mode, limit, deep_blob,
                                                         cancel=stop.is_set, decoded=decoded,
                                                         matcher=matcher)),
                                  None))
                    except Exception as e:      # the table fails, not the search
                        # Hand over the error only: its traceback holds this thread's frames
                        e.__traceback__ = e.__context__ = e.__cause__ = None
                        done.put((name, [], e))
                        e = None
            finally:
                self.release_thread_connection()
                done.put(None)

        threads = [threading.Thread(target=work, name="search-%d" % i) for i in range(count)]
        for th in threads:
            th.daemon = True
            th.start()

        def halt():
            if not stop.is_set():
                stop.set()
            for th in threads:
                self.interrupt(th)

        running = count
        try:
            while running:
                if cancel is not None and cancel():
                    halt()
                try:
                    item = done.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    running -= 1
                elif not stop.is_set():
                    yield item
        finally:
            # Also reached when the caller stops iterating: no worker may outlive the search.
            deadline = time.time() + CLOSE_WAIT
            while any(th.is_alive() for th in threads) and time.time() < deadline:
                halt()
                for th in threads:
                    th.join(0.02)

    def _search_rows(self, t, cols, types, matcher, chunk_size=2000):
        if self.source(t.name) != "sql":
            # Not SQL-served; delegate to iter_rows which handles native/fallback
            for row in self.iter_rows(t.name):
                yield row
            return

        kept = [(c, ty) for c, ty in zip(cols, types) if not matcher.skip_column(ty)]
        if not kept:
            return
        searchable = [c for c, _ty in kept]
        stypes = [ty for _c, ty in kept]

        select, lead = self._select(t)
        where, params = matcher.sql_where(searchable, stypes)
        # No WHERE clause (e.g. a regex without a literal every match contains): read every row
        if not where:
            for row in self.iter_rows(t.name):
                yield row
            return
        sql = "SELECT %s FROM %s WHERE %s" % (select, quote_ident(t.name), where)

        # Stream via _sql_rows with tracking
        seen = set()
        try:
            for row in self._sql_rows(t, lead, sql, params, chunk_size):
                seen.add(row.locator)
                yield row
            return  # Clean SQL run, do not fall through to native
        except SQL_ERRORS as e:
            if is_interrupt(e) or not t.natively_readable:
                raise
            self._sql_failed_on(t.name, e)
            # Fall through to native, skipping rows we already saw

        # Continue with native, skipping seen locators
        for row in self.iter_rows(t.name):
            if row.locator not in seen:
                yield row

    # -- reporting ---------------------------------------------------------
    def banners(self):
        out = []
        wal = self.wal
        if self.mode == IMMUTABLE:
            out.append(Banner("info", "Read-only: evidence opened immutable; nothing is written.",
                              "Read-only"))
        elif self.mode == RAM_OVERLAY:
            out.append(Banner("info", "WAL applied in RAM: %d committed frames (%d commits) merged; "
                                      "evidence untouched." % (wal.last_commit + 1, wal.commit_count),
                              "WAL merged in RAM"))
        elif self.mode == MAIN_ONLY:
            out.append(Banner("warning", "WAL has %d committed frames that SQL cannot see. %s "
                                         "Browse, Search and the Timeline read the tables "
                                         "natively with the WAL applied (sorting and filtering "
                                         "a table then works on its first %s rows: limit "
                                         "native_sort_rows); the SQL tab shows the main file "
                                         "only." % (wal.last_commit + 1, self.main_only_reason(),
                                                    format(limits.get("native_sort_rows"), ",")),
                              "WAL not in SQL tab"))
        elif self.mode == NATIVE and self.safe_parse:
            out.append(Banner("info", "Safe parse: only the built-in parser reads this file; "
                                      "SQLite never opens it (%s). Views, virtual tables and the "
                                      "SQL tab need SQLite and are not available."
                                      % self.safe_parse_reason, "Safe parse"))
        elif self.mode == NATIVE:
            out.append(Banner("error", "SQLite cannot read this file (%s). Showing rows parsed natively."
                                       % (self.sql_error or "unknown error"),
                              "Parsed natively"))
        if self.sql is not None and sqlsafe.old_sqlite():
            out.append(Banner("warning", "This Python bundles SQLite %s, older than %s: it lacks "
                                         "later security fixes%s. For a file you do not trust, "
                                         "open it with Safe parse (SQLite is then not used) or "
                                         "run the tool on a newer Python."
                                         % (sqlite3.sqlite_version,
                                            ".".join(str(v) for v in sqlsafe.SQLITE_ADVISED),
                                            "" if sqlsafe.HAS_SETLIMIT else
                                            ", and Python %d.%d cannot cap the size of values "
                                            "SQLite builds" % sys.version_info[:2]),
                              "Old SQLite %s" % sqlite3.sqlite_version))
        if self.wal is not None and self.wal.frames_cut:
            out.append(Banner("warning", "Only the first %s of the WAL's %s frames were read "
                                         "(%s): commits after them are not applied."
                                         % (format(len(self.wal.frames), ","),
                                            format(self.wal.frames_total, ","),
                                            limits.hint("wal_frames")),
                              "WAL cut"))
        if self.pager is not None and self.pager.declared_page_count > self.pager.page_count:
            out.append(Banner("warning", "The file declares %s pages but holds %s: the size it "
                                         "declares is not used."
                                         % (format(self.pager.declared_page_count, ","),
                                            format(self.pager.page_count, ",")),
                              "Declared size ignored"))
        if self.wal_problem:
            out.append(Banner("warning", "The -wal file could not be read (%s): its frames are NOT "
                                         "applied, so tables may show an older state than the last "
                                         "commit." % self.wal_problem,
                              "WAL unreadable"))
        if wal is not None:
            c = wal.state_counts()
            if c["uncommitted"] or c["stale"]:
                parts = ["%d %s" % (c[k], k) for k in ("stale", "uncommitted") if c[k]]
                out.append(Banner("info", "WAL also holds %d uncommitted and %d stale frames - see "
                                          "the WAL tab." % (c["uncommitted"], c["stale"]),
                                  "WAL: %s" % ", ".join(parts)))
        if self.evidence.journal_is_hot():
            out.append(Banner("warning", "Hot rollback journal present: the main file may contain an "
                                         "interrupted transaction (not rolled back).",
                              "Hot journal"))
        if self.schema.collations:
            out.append(Banner("info", "Approximate sort order for app-defined collation(s): %s"
                                      % ", ".join(sorted(self.schema.collations)),
                              "Approx. sort order"))
        if self.schema.unregistered_collations and self.sql is not None:
            out.append(Banner("warning", "Collation(s) %s could not be registered on Python %d.%d: "
                                         "SQL that needs them fails, so those tables are read "
                                         "natively (binary sort order)."
                                         % (", ".join(sorted(self.schema.unregistered_collations)),
                                            sys.version_info[0], sys.version_info[1]),
                              "Collation fallback"))
        if self._sql_failed:
            out.append(Banner("warning", "SQLite failed on %d table(s); read natively instead: %s"
                                         % (len(self._sql_failed), ", ".join(sorted(self._sql_failed))),
                              "%d table(s) read natively" % len(self._sql_failed)))
        vgen = [n for n in self.schema.names("table")
                if any(c.hidden == HIDDEN_VIRTUAL_GEN for c in self.info(n).columns)
                and self.source(n) == "native"]
        if vgen:
            risky = [n for n in vgen if self._unbounded_computed(self.info(n))]
            out.append(Banner("warning", "VIRTUAL generated columns are computed by SQLite, not "
                                         "stored: they show as NULL in natively read table(s) %s.%s"
                                         % (", ".join(vgen),
                                            " (%s: their expressions can build values of any "
                                            "size, which Python %d.%d cannot cap, so SQLite does "
                                            "not compute them.)" % ((", ".join(risky),)
                                                                    + sys.version_info[:2])
                                            if risky else ""),
                              "Generated columns NULL"))
        return out

    def _release(self, wait=0.0):
        """Close the SQL connections, the SQL backend, the pager and the WAL; stop hashing."""
        self._release_connections(wait)
        with self._pos_lock:
            self._positions.clear()
        if self.sql is not None:
            self.sql.close()
        if self.pager is not None:
            self.pager.close()
        if self.wal is not None:
            self.wal.close()
        self.evidence.cancel_hashing()

    def _release_connections(self, wait):
        """Close the SQL connections without ever closing one another thread may be running on.

        Closing a connection finalizes its statements; on Python <= 3.10 doing that while another
        thread is still inside sqlite3_step on one of them crashes the process. So:
        - connections of this thread, and of threads that have ended, are closed here;
        - another live thread's connection is left to that thread. The session drops all its
          own references to it, so CPython frees (and so closes) it once the thread lets go of
          it (for an interrupted worker, as soon as its statement has returned); it is closed
          here if the thread ends first. This waits up to `wait` seconds for either, watching a
          weak reference; a connection still held after that is logged as an Issue;
        - new_connection() connections belong to their callers: they are only interrupted.
        """
        with self._conns_lock:
            self._closed = True           # conn() and new_connection() now refuse
            owned, self._owned = self._owned, []
            lent, self._lent = self._lent, []
            self._thread_conns = {}
            self._local = threading.local()       # drops every thread's reference as well
        for c in lent:
            _interrupt_quietly(c)         # a statement started since interrupt(): stop it too
        me = threading.current_thread()
        waiting = []                      # (weak reference, owning thread)
        while owned:
            c, owner = owned.pop()
            _interrupt_quietly(c)
            if owner is me or not owner.is_alive():
                _close_quietly(c)         # nothing else can be running on it
            else:
                waiting.append((weakref.ref(c), owner))
            c = None                      # hold no reference while waiting
        now = time.time()
        deadline, next_gc = now + wait, now + 0.05
        while waiting:
            still = []
            for ref, owner in waiting:
                c = ref()
                if c is not None and not owner.is_alive():
                    _close_quietly(c)     # its thread has ended: nothing can be running on it
                elif c is not None:
                    still.append((ref, owner))
                c = None
            waiting = still
            now = time.time()
            if not waiting or now >= deadline:
                break
            if now >= next_gc:
                # On Python 3.11+ a connection sits in a reference cycle with its statement
                # cache, so one no thread holds any more is freed only by the cycle collector.
                gc.collect()
                next_gc = now + 0.5
            time.sleep(0.01)
        if waiting:
            self.issues.add("connection_left_open",
                            "%d SQL connection(s) still held by worker thread(s) after waiting "
                            "%.1f s: their statements were interrupted, and each is closed when "
                            "its thread lets go of it (closing it from here could crash "
                            "Python <= 3.10)" % (len(waiting), wait),
                            ", ".join(sorted(set(o.name for _r, o in waiting))), "warning")
        if lent:
            self.issues.add("connection_left_open",
                            "%d caller-owned SQL connection(s) (SQL tab) not released at close: "
                            "interrupted; each is closed by its caller (release_connection())"
                            % len(lent), "", "info")

    def close(self, wait=CLOSE_WAIT, verify=True):
        """Release everything and verify the evidence is unchanged.

        Running SQL on worker threads (counts, searches, the SQL tab) is interrupted first, so
        closing does not wait for a long COUNT(*) or scan to finish; it waits at most `wait`
        seconds for those threads to leave their interrupted statements (see
        _release_connections).

        The evidence is verified: size and modification time always, and the SHA-256 again
        when it was computed and the files total at most the limit 'verify_rehash_bytes'; the
        report says which. verify=False: no verification (None; the caller verified the
        evidence itself, e.g. with progress and a way to skip the re-hash).
        """
        self.interrupt()
        self._release(wait)
        if not verify:
            return None
        return self.evidence.verify_on_close(limits.get("verify_rehash_bytes"))

    # -- deep forensics ----------------------------------------------------
    @property
    def forensics(self):
        """engine.forensics.Forensics for this session (carving, row history, dropped schema,
        journal, audit, reports), created on first use."""
        from .forensics import forensics_for
        return forensics_for(self)
