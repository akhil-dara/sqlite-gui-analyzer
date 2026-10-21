"""One read-only connection over several databases of a case, for queries that join them.

Each database is attached by the URI its own Session reads through: the shared in-memory image
(main file + WAL frames) when the WAL is applied, else the file opened read-only and immutable.
Nothing is copied and each database shows exactly what its own SQL shows. Only this module
attaches (the evidence paths come from the sessions, never from SQL text); once attached, the
read-only authorizer refuses any further ATTACH, DETACH or file pragma, as on every connection
of the tool. A database SQLite cannot read (read natively) cannot take part: it is named.
"""

import sqlite3

from . import sqlsafe
from .backends import EngineConnection, lenient_text, read_only_authorizer, register_collations, \
    safe_regexp
from .schema import quote_ident


class CaseUnavailable(Exception):
    pass


def schema_name(i):
    return "db%d" % (i + 1)


def _sql_value(v):
    """A recovered value SQLite can store: text, numbers, bytes and NULL as they are; damaged
    text (InvalidText) as its bytes."""
    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    try:
        return bytes(v)
    except Exception:                   # noqa: BLE001
        return str(v)


def case_connection(sessions, recovered=None):
    """sessions: [(name, Session)] -> (connection, {name: schema}); raises CaseUnavailable
    naming the databases that cannot be attached. recovered: {table name: (columns, rows)}
    becomes TEMP tables of this connection (in memory, before it is made read-only): the
    records the Forensics recovery found, queried and joined like the live tables."""
    missing = [n for n, s in sessions if getattr(s, "sql", None) is None]
    if missing:
        raise CaseUnavailable("read natively (SQLite cannot open them), so they cannot be "
                              "joined with others: %s" % ", ".join(missing))
    conn = sqlite3.connect(":memory:", uri=True, check_same_thread=False,
                           factory=EngineConnection)
    try:
        conn.text_factory = lenient_text
        schemas, collations = {}, set()
        for i, (name, s) in enumerate(sessions):
            sch = schema_name(i)
            conn.execute("ATTACH DATABASE ? AS %s" % quote_ident(sch), (s.sql.uri,))
            schemas[name] = sch
            collations.update(s.sql.collations)
        conn.execute("PRAGMA temp_store=MEMORY")
        for table, (columns, rows) in (recovered or {}).items():
            conn.execute("CREATE TEMP TABLE %s (%s)" % (
                quote_ident(table), ", ".join(quote_ident(c) for c in columns)))
            conn.executemany("INSERT INTO temp.%s VALUES (%s)" % (
                quote_ident(table), ", ".join("?" * len(columns))),
                [tuple(_sql_value(v) for v in r) for r in rows])
        sqlsafe.harden_reader(conn)
        guard = getattr(conn, "sga_guard", None)
        conn.set_authorizer(lambda a, b, c, d, e: read_only_authorizer(a, b, c, d, e, guard))
        register_collations(conn, collations)
        conn.create_function("REGEXP", 2, safe_regexp)
        return conn, schemas
    except BaseException:
        conn.close()
        raise
