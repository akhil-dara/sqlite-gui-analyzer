"""Running SQL that comes from the evidence, with no power over the machine.

The CREATE statements stored in a database (its sqlite_master `sql` texts, schema rows carved
from free space, the schema of a journal or WAL state) are written by whoever made the file.
This module is the one place that hands such text to SQLite:

- first_statement() cuts the text at a NUL (SQLite reads stored text up to the NUL) and at the
  end of its first complete statement; whatever follows is never run.
- create_as_select() tells a 'CREATE TABLE name AS SELECT ...' apart: its query is never run.
- Scratch is an empty in-memory database whose authorizer allows creating the object being
  described and the PRAGMAs that read its columns and indexes, and nothing else (no SELECT,
  no recursive query, no ATTACH, no writes but SQLite's own bookkeeping). A progress handler
  stops a statement after limits 'schema_replay_steps' SQLite steps, and on Python 3.11+
  setlimit() caps value and statement sizes. Every failure is a ReplayError with a reason.
- harden_reader() configures a read-only connection on the evidence: defensive mode (Python
  3.12+), trusted_schema off, cell_size_check on, no memory-mapped I/O, no ATTACH, value
  sizes capped (limits 'sql_value_bytes', Python 3.11+), and a ReadGuard that can stop a
  statement after a number of steps (views and computed columns, limits 'sql_view_steps').

SQL_ERRORS is every exception class a call into SQLite can raise for hostile input (the
sqlite3 module raises more than sqlite3.Error: Warning and ValueError on Python <= 3.11,
UnicodeDecodeError for an error message that is not UTF-8, MemoryError for SQLITE_NOMEM).
"""

import sqlite3
import sys
import threading

from . import limits

SQL_ERRORS = (sqlite3.Error, sqlite3.Warning, ValueError, UnicodeError, MemoryError,
              OverflowError)

HAS_SETLIMIT = hasattr(sqlite3.Connection, "setlimit")
HAS_SETCONFIG = hasattr(sqlite3.Connection, "setconfig")
PROGRESS_EVERY = 1000           # SQLite steps between two calls of a progress handler

# Built-in functions that can build a value of any size from a few bytes of SQL. On Python
# without setlimit() (3.8-3.10) a view or trigger may not call them (computed columns that
# call them are read natively: see risky_sql()).
SIZE_FUNCTIONS = frozenset(("zeroblob", "randomblob", "printf", "format", "replace", "hex",
                            "quote"))
_RISKY_WORDS = tuple(sorted(SIZE_FUNCTIONS))

# The oldest SQLite whose known security fixes all apply to how this tool uses it.
SQLITE_ADVISED = (3, 44, 0)


class ReplayError(sqlite3.DatabaseError):
    """A CREATE statement from the evidence was not (or not completely) replayed. A
    sqlite3.DatabaseError, so code catching SQLite's errors catches it too."""


# -- tokens --------------------------------------------------------------------------------
def _scan(sql):
    """Yield (start, end, kind) of the tokens of sql: kind is 'str' ('...'), 'qid' ("..",
    `..`, [..]), 'word', 'semi' or 'punct'. Comments and white space are skipped. Unterminated
    quotes and comments run to the end. Linear in len(sql)."""
    n = len(sql)
    i = 0
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in "'\"`":
            j = i + 1
            while True:
                k = sql.find(ch, j)
                if k < 0:
                    j = n
                    break
                if k + 1 < n and sql[k + 1] == ch:
                    j = k + 2
                    continue
                j = k + 1
                break
            yield i, j, ("str" if ch == "'" else "qid")
            i = j
            continue
        if ch == "[":
            k = sql.find("]", i + 1)
            j = n if k < 0 else k + 1
            yield i, j, "qid"
            i = j
            continue
        if ch == ";":
            yield i, i + 1, "semi"
            i += 1
            continue
        if ch.isalnum() or ch == "_" or ch == "$" or ord(ch) > 127:
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "_$" or ord(sql[j]) > 127):
                j += 1
            yield i, j, "word"
            i = j
            continue
        yield i, i + 1, "punct"
        i += 1


def words(sql, most=8):
    """The first `most` significant tokens of sql, upper-cased words and raw others."""
    out = []
    for s, e, kind in _scan(sql):
        out.append(sql[s:e].upper() if kind == "word" else sql[s:e])
        if len(out) >= most:
            break
    return out


def statement_end(sql):
    """(start, end) of the ';' that ends the first statement of sql, or None when it has none.
    A CREATE TRIGGER ends at the ';' after the END that closes its BEGIN ... END body (an END
    right after a ';' or the BEGIN, as SQLite decides). One pass over the text."""
    head = []
    trigger = False
    body = False            # inside a trigger's BEGIN ... END
    closed = False          # the body's END has been seen
    prev = None             # the previous significant token: (kind, upper text)
    for s, e, kind in _scan(sql):
        tok = sql[s:e].upper() if kind == "word" else sql[s:e]
        if len(head) < 4:
            head.append(tok)
            if len(head) >= 2 and head[0] == "CREATE" and "TRIGGER" in head[1:4]:
                trigger = True
        if kind == "semi":
            if not trigger or closed:
                return s, e
        elif trigger and kind == "word":
            if not body and tok == "BEGIN":
                body = True
            elif body and not closed and tok == "END" and prev is not None and \
                    (prev[0] == "semi" or prev[1] == "BEGIN"):
                closed = True
        prev = (kind, tok)
    return None


def first_statement(sql):
    """(statement, cut): the text up to the end of its first complete statement (without the
    ';'), and True when anything that would run was cut off (a NUL and what follows it, or
    more statements). A CREATE TRIGGER keeps its BEGIN ... END body. Linear time."""
    if not sql:
        return "", False
    cut = False
    nul = sql.find("\x00")
    if nul >= 0:
        cut = bool(sql[nul:].strip("\x00 \t\r\n"))
        sql = sql[:nul]
    end = statement_end(sql)
    if end is None:
        return sql, cut
    s, e = end
    if any(k != "semi" for _s, _e, k in _scan(sql[e:])):
        cut = True
    return sql[:s], cut


def create_as_select(sql):
    """True for 'CREATE [TEMP] TABLE [IF NOT EXISTS] [schema.]name AS ...'."""
    toks = []
    for s, e, kind in _scan(sql):
        toks.append((sql[s:e], kind))
        if len(toks) >= 12:
            break
    i = 0

    def word(k):
        return i + k < len(toks) and toks[i + k][1] == "word" and toks[i + k][0].upper()

    if word(0) != "CREATE":
        return False
    i = 1
    if word(0) in ("TEMP", "TEMPORARY"):
        i += 1
    if word(0) != "TABLE":
        return False
    i += 1
    if word(0) == "IF" and word(1) == "NOT" and word(2) == "EXISTS":
        i += 3
    i += 1                                  # the name
    if i < len(toks) and toks[i][0] == ".":
        i += 2                              # schema.name
    return word(0) == "AS"


def risky_sql(sql):
    """True when SQL text calls a function that can build very large values (checked for
    computed columns on Python without setlimit())."""
    low = "".join((sql or "").lower().split())
    return any(w + "(" in low for w in _RISKY_WORDS)


# -- scratch replays -----------------------------------------------------------------------
_PRAGMAS_ALLOWED = frozenset(("table_info", "table_xinfo", "index_list", "index_info",
                              "index_xinfo", "foreign_key_list", "data_version"))


class Scratch(object):
    """An isolated in-memory database for replaying evidence CREATE statements.

    replay(sql) creates one table, index or virtual table (raises ReplayError), pragma(sql)
    reads metadata (raises ReplayError), literal(sql) evaluates a constant DEFAULT (or None).
    Use as a context manager, or call close().
    """

    def __init__(self, steps=None):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.steps = limits.get("schema_replay_steps") if steps is None else steps
        self._allow_select = False
        self._calls = 0
        self.tripped = False
        self.denied = []
        try:
            self.conn.execute("PRAGMA trusted_schema=OFF")
        except SQL_ERRORS:
            pass
        _apply_limits(self.conn, limits.get("schema_value_bytes"), limits.get("schema_sql_bytes"))
        _defensive(self.conn)
        self.conn.set_authorizer(self._auth)
        self.conn.set_progress_handler(self._progress, PROGRESS_EVERY)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.conn.close()
        except SQL_ERRORS:
            pass

    def _auth(self, action, arg1, arg2, db_name, source):
        a = sqlite3
        if action in (a.SQLITE_CREATE_TABLE, a.SQLITE_CREATE_INDEX, a.SQLITE_CREATE_TEMP_TABLE,
                      a.SQLITE_CREATE_TEMP_INDEX, a.SQLITE_CREATE_VTABLE, a.SQLITE_INSERT,
                      a.SQLITE_UPDATE, a.SQLITE_READ, a.SQLITE_FUNCTION, a.SQLITE_TRANSACTION,
                      a.SQLITE_SAVEPOINT, a.SQLITE_REINDEX):
            # SQLite's own bookkeeping (sqlite_master, a virtual table's shadow tables) in
            # this empty scratch database; functions are only resolved, never run, by CREATE
            return a.SQLITE_OK
        if action == a.SQLITE_PRAGMA and (arg1 or "").lower() in _PRAGMAS_ALLOWED:
            return a.SQLITE_OK
        if action == a.SQLITE_SELECT and self._allow_select:
            return a.SQLITE_OK
        self.denied.append(action)
        return a.SQLITE_DENY

    def _progress(self):
        self._calls += 1
        if self._calls * PROGRESS_EVERY > self.steps:
            self.tripped = True
            return 1
        return 0

    def _run(self, sql, select=False):
        self._calls = 0
        self.tripped = False
        self.denied = []
        self._allow_select = select
        try:
            return self.conn.execute(sql).fetchall()
        except SQL_ERRORS as e:
            if self.tripped:
                raise ReplayError("stopped after %s SQLite steps (%s)"
                                  % (format(self.steps, ","), limits.hint("schema_replay_steps")))
            if self.denied:
                raise ReplayError("refused: the statement does more than create its object "
                                  "(%s)" % _short(e))
            raise ReplayError(_short(e))
        finally:
            self._allow_select = False

    def replay(self, sql, virtual=False):
        """Create the object of one CREATE TABLE / INDEX / VIRTUAL TABLE statement. Returns
        a note ('' or why text after the statement was ignored); raises ReplayError."""
        stmt, cut = first_statement(sql or "")
        if not stmt.strip():
            raise ReplayError("empty statement")
        head = words(stmt, 4)
        if head[:1] != ["CREATE"]:
            raise ReplayError("not a CREATE statement")
        if create_as_select(stmt):
            raise ReplayError("CREATE TABLE ... AS SELECT: the table is made by a query, which "
                              "is never run")
        # A virtual table module reads its own shadow tables while it is created (and a
        # CREATE VIRTUAL TABLE cannot carry a query of its own).
        self._run(stmt, select=head[1:2] == ["VIRTUAL"] or (virtual and "VIRTUAL" in head))
        return "text after the first statement ignored" if cut else ""

    def pragma(self, sql):
        return self._run(sql)

    def literal(self, sql):
        """Value of 'SELECT <constant>' (the caller has checked the text is a literal)."""
        try:
            rows = self._run("SELECT " + sql, select=True)
        except ReplayError:
            return None
        return rows[0][0] if rows else None


def _short(e):
    try:
        text = str(e)
    except Exception:           # noqa: BLE001 - an exception whose text cannot be made
        text = ""
    return "%s: %s" % (type(e).__name__, text[:200]) if text else type(e).__name__


def describe_error(e):
    """The text of an error from SQLite: its message, or for the exceptions that are not
    sqlite3.Error also their class."""
    if isinstance(e, UnicodeDecodeError):
        return "SQLite refused it with an error message that is not valid text"
    if isinstance(e, MemoryError):
        return "SQLite ran out of memory on it (SQLITE_NOMEM, often a damaged page)"
    if isinstance(e, sqlite3.Error):
        try:
            return str(e) or "SQLite %s without a message" % type(e).__name__
        except Exception:       # noqa: BLE001
            return type(e).__name__
    return _short(e)


def _apply_limits(conn, value_bytes, sql_bytes=None):
    if not HAS_SETLIMIT:
        return False
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, value_bytes)
        if sql_bytes is not None:
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, sql_bytes)
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        return True
    except SQL_ERRORS:
        return False


def _defensive(conn):
    if not HAS_SETCONFIG:
        return
    for name, on in (("SQLITE_DBCONFIG_DEFENSIVE", True), ("SQLITE_DBCONFIG_TRUSTED_SCHEMA", False),
                     ("SQLITE_DBCONFIG_ENABLE_FTS3_TOKENIZER", False),
                     ("SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION", False)):
        op = getattr(sqlite3, name, None)
        if op is None:
            continue
        try:
            conn.setconfig(op, on)
        except SQL_ERRORS:
            pass


# -- readers on the evidence ---------------------------------------------------------------
class ReadGuard(object):
    """Per-connection state of a reader: a step budget a caller arms for one statement (a view
    or a table with computed columns), and why the last statement was stopped."""

    def __init__(self):
        self.budget = None
        self.calls = 0
        self.tripped = None         # the limit's name when a statement was stopped by it
        self.denied_function = None
        self.view_names = frozenset()   # views of the database (read_only_authorizer)
        self.in_view = False
        self.lock = threading.Lock()

    def progress(self):
        b = self.budget
        if b is None:
            return 0
        self.calls += 1
        if self.calls * PROGRESS_EVERY > b:
            self.tripped = "sql_view_steps"
            return 1
        return 0


class LimitStopped(sqlite3.OperationalError):
    """A statement on the evidence was stopped by one of this tool's limits (its text says
    which, in plain words). Not an interruption: callers report it."""


class guarded(object):
    """with guarded(conn, steps): run statements on a connection made by harden_reader().
    steps=True arms the step budget (limits 'sql_view_steps', for views and computed columns).
    An error caused by a limit leaves as LimitStopped with a message naming the limit."""

    def __init__(self, conn, steps=False):
        self.conn = conn
        self.guard = getattr(conn, "sga_guard", None)
        self.steps = steps

    def __enter__(self):
        g = self.guard
        if g is not None:
            g.calls = 0
            g.tripped = None
            g.denied_function = None
            g.in_view = False
            g.budget = limits.get("sql_view_steps") if self.steps else None
        return self

    def __exit__(self, et, ev, tb):
        g = self.guard
        try:
            if ev is not None and isinstance(ev, SQL_ERRORS) and not isinstance(ev, LimitStopped):
                why = stopped_reason(self.conn, ev)
                if why:
                    raise LimitStopped(why)
        finally:
            if g is not None:
                g.budget = None
        return False


def harden_reader(conn, issues=None, label=""):
    """Configure a read-only connection on the evidence (see the module text)."""
    problems = []
    for pragma in ("PRAGMA query_only=ON", "PRAGMA trusted_schema=OFF",
                   "PRAGMA cell_size_check=ON", "PRAGMA mmap_size=0"):
        try:
            conn.execute(pragma).fetchall()
        except SQL_ERRORS as e:
            problems.append("%s failed: %s" % (pragma, _short(e)))
    _defensive(conn)
    limited = _apply_limits(conn, limits.get("sql_value_bytes"))
    guard = ReadGuard()
    try:
        conn.sga_guard = guard
    except AttributeError:
        guard = None
    if guard is not None:
        conn.set_progress_handler(guard.progress, PROGRESS_EVERY)
    if issues is not None:
        for p in problems:
            issues.add("pragma_failed", p, label)
    return limited


def harden_copy(conn):
    """Settings for a connection that only copies an image of the evidence (the in-RAM WAL
    view): defensive mode, trusted_schema off, no ATTACH, value sizes capped."""
    try:
        conn.execute("PRAGMA trusted_schema=OFF").fetchall()
    except SQL_ERRORS:
        pass
    _defensive(conn)
    _apply_limits(conn, limits.get("sql_value_bytes"))


def stopped_reason(conn, err):
    """Plain words for an error a guard caused (step budget, value size, a refused function),
    or None when the error is SQLite's own."""
    g = getattr(conn, "sga_guard", None)
    if g is not None and g.tripped:
        return ("stopped: exceeded the %s limit (%s SQLite steps; %s)"
                % (g.tripped, format(limits.get(g.tripped), ","), limits.hint(g.tripped)))
    if g is not None and g.denied_function:
        return ("refused: uses %s, which can build values of any size; Python %d.%d cannot "
                "limit value sizes (3.11 or later can)"
                % (g.denied_function, sys.version_info[0], sys.version_info[1]))
    try:
        text = str(err).lower()
    except Exception:           # noqa: BLE001
        text = ""
    if "too big" in text or "toobig" in text:
        return ("stopped: a value is larger than the sql_value_bytes limit (%s bytes; %s)"
                % (format(limits.get("sql_value_bytes"), ","), limits.hint("sql_value_bytes")))
    return None


def old_sqlite():
    """True when this Python's SQLite is older than SQLITE_ADVISED."""
    return sqlite3.sqlite_version_info < SQLITE_ADVISED
