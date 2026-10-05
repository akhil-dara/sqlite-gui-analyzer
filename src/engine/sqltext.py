"""SQL text the app writes for people to read, copy or run, built so that text from the
evidence can never add a statement or an sqlite3 command-line ('.shell') line.

- comment_lines(): any text as '-- ' comment lines (every line prefixed, whatever the line
  break: \\n, \\r, U+2028...).
- first_statement(): a stored CREATE statement cut at the end of its first complete statement
  (SQLite reads only that much when it loads a schema; a crafted database can store more).
- display_sql(): stored SQL to show or copy: the first statement, and anything after it only
  as comment lines.
- create_table_sql(): a CREATE TABLE rebuilt from the parsed columns (engine.datamap's
  table_schema dict) instead of the stored text: quoted names, declared types reduced to
  plain type names, NOT NULL, PRIMARY KEY (also composite), WITHOUT ROWID.
"""

import re

from . import sqlsafe
from .schema import quote_ident

_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?: +[A-Za-z_][A-Za-z0-9_]*)*"
                      r"(?: *\( *[+-]?\d+(?:\.\d+)? *(?:, *[+-]?\d+(?:\.\d+)? *)?\))?$")
_AFFINITY_TYPE = {"INTEGER": "INTEGER", "TEXT": "TEXT", "BLOB": "BLOB", "REAL": "REAL",
                  "NUMERIC": "NUMERIC"}


def comment_lines(text, prefix="-- "):
    """text as SQL comment lines: every line (split at any line break) starts with prefix, NUL
    becomes \\x00. Never an empty list."""
    s = str(text).replace("\x00", "\\x00")
    return [prefix + line for line in (s.splitlines() or [""])]


def first_statement(sql):
    """(statement, rest): sql cut after the ';' that completes its first statement (statement
    keeps that ';'), rest the text after it with surrounding blanks removed. A text with no
    complete statement is returned whole as the statement."""
    sql = str(sql or "")
    nul = sql.find("\x00")
    head = sql if nul < 0 else sql[:nul]        # SQLite reads stored text up to a NUL
    end = sqlsafe.statement_end(head)           # one pass, quotes and comments understood
    if end is None:
        if nul < 0:
            return sql.strip(), ""
        return head.strip(), sql[nul:].strip()
    return sql[:end[1]].strip(), sql[end[1]:].strip()


def display_sql(sql):
    """Stored SQL to show or copy: its first complete statement ending with ';', and any text
    stored after it only as comment lines (so pasting it into sqlite3 runs one statement)."""
    stmt, rest = first_statement(sql)
    if not stmt:
        return ""
    if not stmt.endswith(";"):
        stmt += ";"
    if not rest:
        return stmt
    return "\n".join([stmt, "-- Text stored after the statement (SQLite ignores it):"]
                     + comment_lines(rest))


def safe_type(decl_type):
    """The declared type when it is plain type words with an optional (n) or (n, m), with
    blanks collapsed; else None."""
    t = " ".join(str(decl_type or "").split())
    if not t:
        return ""
    return t if _TYPE_RE.match(t) else None


def create_table_sql(schema, name=None):
    """(sql, notes): a CREATE TABLE statement rebuilt from a table_schema() dict, and notes
    (plain text) on what it does not reproduce. sql is None when the table cannot be rebuilt
    (a virtual table, no columns): the caller writes the stored text as comments instead."""
    name = schema.get("name") if name is None else name
    cols = [c for c in schema.get("columns") or () if c.get("visible", True)]
    if schema.get("kind", "table") != "table":
        return None, ["%s is a %s table: its statement is not rebuilt" % (name, schema.get("kind"))]
    if not cols:
        return None, ["the columns of %s could not be read: its statement is not rebuilt" % name]
    notes = []
    pk = list(schema.get("primary_key") or ())
    alias = schema.get("rowid_alias")
    defs = []
    for c in cols:
        t = safe_type(c.get("type"))
        if t is None:
            t = _AFFINITY_TYPE.get(c.get("affinity") or "", "")
            notes.append("column %s: declared type %r written as %s" % (
                c["name"], c.get("type"), t or "no type"))
        part = quote_ident(c["name"]) + (" " + t if t else "")
        if alias is not None and c["name"] == alias and pk == [alias]:
            part += " PRIMARY KEY"
        if c.get("not_null"):
            part += " NOT NULL"
        if c.get("note"):
            notes.append("column %s: %s, written as an ordinary column" % (c["name"], c["note"]))
        defs.append(part)
    if pk and not (alias is not None and pk == [alias]):
        defs.append("PRIMARY KEY (%s)" % ", ".join(quote_ident(p) for p in pk))
    sql = "CREATE TABLE %s (\n  %s\n)%s;" % (quote_ident(name), ",\n  ".join(defs),
                                            " WITHOUT ROWID" if schema.get("without_rowid")
                                            else "")
    return sql, notes
