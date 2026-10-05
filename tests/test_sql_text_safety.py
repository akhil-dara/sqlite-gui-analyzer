"""SQL and Markdown written from evidence text: a crafted table name, column name, declared
type or stored statement can add no statement and no sqlite3 '.command' line to a .sql file,
and can not leave a Markdown code block."""
import os
import re
import sqlite3
import sys

from tests.test_session import SessionTestBase
from engine import datamap as dm
from engine import related_copy as rc
from engine import sqltext as st
from engine.relations import relation_map
from engine.schema import Locator

EVIL_TABLE = "par\n.shell echo PWNED\n```\n<img src=x onerror=alert(1)>\n# heading"
EVIL_COLUMN = "na\n.shell echo COL\n);ATTACH 'x.db' AS y;--"
EVIL_TYPE = "weird;type"
TRAILER = "\n.shell echo TRAILER\nATTACH DATABASE 'evil.db' AS e;\nCREATE TABLE pwned(x);"


def cli_statements(text):
    """Split text the way the sqlite3 command-line shell reads a script: a line starting with
    '.' while no statement is pending is a dot-command. Returns (statements, dot_lines)."""
    stmts, dots, buf = [], [], ""
    for line in text.split("\n"):
        if not buf.strip() and line.startswith("."):
            dots.append(line)
            continue
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            body = "\n".join(l for l in buf.split("\n") if not l.lstrip().startswith("--")).strip()
            if body:
                stmts.append(body)
            buf = ""
    return stmts, dots


def md_outside_fences(text):
    """The lines of Markdown outside fenced code blocks (CommonMark fence rules)."""
    out, fence = [], ""
    for line in text.split("\n"):
        if not fence:
            m = re.match(r"^ {0,3}(`{3,})", line)
            if m:
                fence = m.group(1)
            else:
                out.append(line)
        else:
            s = line.strip()
            if s and set(s) == {"`"} and len(s) >= len(fence):
                fence = ""
    return out, fence


def build_evil(path, trailer=False):
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE %s (id INTEGER PRIMARY KEY, %s "%s", v TEXT NOT NULL)' % (
        '"' + EVIL_TABLE.replace('"', '""') + '"', '"' + EVIL_COLUMN.replace('"', '""') + '"',
        EVIL_TYPE))
    c.execute('CREATE TABLE wr (a TEXT NOT NULL, b INT, c BLOB, PRIMARY KEY (a, b)) WITHOUT ROWID')
    c.execute("CREATE VIEW v AS SELECT '```\n# hi\n<script>alert(1)</script>' AS x")
    c.execute('INSERT INTO "%s" VALUES (1, 2, \'x\')' % EVIL_TABLE.replace('"', '""'))
    c.execute("INSERT INTO wr VALUES ('k', 1, x'00')")
    c.commit()
    if trailer:
        c.execute("PRAGMA writable_schema=ON")
        c.execute("UPDATE sqlite_master SET sql = sql || ? WHERE name='wr'", (TRAILER,))
        c.commit()
    c.close()
    return path


class SqlTextHelpersTest(SessionTestBase):
    def test_comment_lines_prefix_every_line(self):
        for text in ("a\n.shell x", "a\r.shell x", "a\r\nb", "a b\x0bc", "x\x00y", ""):
            lines = st.comment_lines(text)
            self.assertTrue(lines)
            joined = "\n".join(lines)
            for line in re.split(r"[\r\n]", joined):
                self.assertTrue(line.startswith("-- "), repr(line))
            self.assertNotIn("\x00", joined)

    def test_first_statement_and_display(self):
        # with no ';' of its own the statement runs on into the text after it: still one
        # statement pending in the shell, so no line of it is a dot-command
        sql = "CREATE TABLE t(a, b DEFAULT ';')" + TRAILER
        self.assertEqual(cli_statements(st.display_sql(sql))[1], [])
        stmt, rest = st.first_statement("CREATE TABLE t(a, b DEFAULT ';');" + TRAILER)
        self.assertEqual(stmt, "CREATE TABLE t(a, b DEFAULT ';');")
        self.assertIn("ATTACH", rest)
        shown = st.display_sql("CREATE TABLE t(a);" + TRAILER)
        stmts, dots = cli_statements(shown)
        self.assertEqual(dots, [])
        self.assertEqual(stmts, ["CREATE TABLE t(a);"])
        self.assertIn(".shell echo TRAILER", shown)      # still shown, as a comment
        trig = "CREATE TRIGGER g AFTER INSERT ON t BEGIN SELECT 1; SELECT 2; END"
        self.assertEqual(st.display_sql(trig), trig + ";")

    def test_safe_type(self):
        for ok in ("INTEGER", "VARCHAR(255)", "DECIMAL(10, 2)", "unsigned big int", "", "TEXT"):
            self.assertIsNotNone(st.safe_type(ok), ok)
        self.assertEqual(st.safe_type("unsigned\n  big int"), "unsigned big int")
        for bad in ("weird;type", "a)--", "TEXT); DROP", '"q"', "x -- y", "INT/*"):
            self.assertIsNone(st.safe_type(bad), bad)

    def test_rebuilt_create_table(self):
        schema = {"name": "t\n.x", "kind": "table", "without_rowid": True,
                  "primary_key": ["a", "b"], "rowid_alias": None,
                  "columns": [{"name": "a", "type": "TEXT", "affinity": "TEXT", "not_null": True,
                               "visible": True, "note": ""},
                              {"name": "b", "type": "x;y", "affinity": "NUMERIC",
                               "not_null": False, "visible": True, "note": ""}]}
        sql, notes = st.create_table_sql(schema)
        self.assertEqual(sql, 'CREATE TABLE "t\n.x" (\n  "a" TEXT NOT NULL,\n  "b" NUMERIC,\n'
                              '  PRIMARY KEY ("a", "b")\n) WITHOUT ROWID;')
        self.assertTrue(any("x;y" in n for n in notes))
        c = sqlite3.connect(":memory:")
        try:
            c.execute(sql)
            info = c.execute('PRAGMA table_info("t\n.x")').fetchall()
            self.assertEqual([(r[1], r[3], r[5]) for r in info], [("a", 1, 1), ("b", 1, 2)])
        finally:
            c.close()
        self.assertIsNone(st.create_table_sql({"name": "v", "kind": "virtual",
                                               "columns": [{"name": "a"}]})[0])


class RelatedCopySqlTest(SessionTestBase):
    def bundle(self, path, table, loc):
        s = self.open(path)
        return rc.related_bundle(s, relation_map(s), table, [loc])

    def check_script(self, text, tables):
        """Runs every statement of text in order, as the sqlite3 shell would, on an empty
        database: only CREATE TABLE and SELECT, never an ATTACH; returns the tables made."""
        stmts, dots = cli_statements(text)
        self.assertEqual(dots, [], text)
        actions = []

        def auth(code, *_a):
            actions.append(code)
            return sqlite3.SQLITE_OK
        c = sqlite3.connect(":memory:")
        try:
            c.set_authorizer(auth)
            for s in stmts:
                self.assertRegex(s, r"^(CREATE TABLE|SELECT)\b", s)
                c.execute(s)
            names = set(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        finally:
            c.close()
        self.assertNotIn(sqlite3.SQLITE_ATTACH, actions)
        self.assertTrue(names <= set(tables), names)
        return names

    def test_crafted_names_add_no_command(self):
        path = build_evil(os.path.join(self.tmp, "evil.db"))
        b = self.bundle(path, EVIL_TABLE, Locator("rowid", 1))
        text = b.render("sql")
        self.assertEqual(self.check_script(text, [EVIL_TABLE]), {EVIL_TABLE})
        self.assertIn("weird;type", text)                # named in a comment only
        c = sqlite3.connect(":memory:")
        try:
            c.executescript("\n".join(cli_statements(text)[0][:1]))
            types = [r[2] for r in c.execute('PRAGMA table_info("%s")' % EVIL_TABLE.replace('"', '""'))]
        finally:
            c.close()
        self.assertEqual(types, ["INTEGER", "NUMERIC", "TEXT"])
        md, open_fence = md_outside_fences(b.render("markdown"))
        self.assertEqual(open_fence, "")
        self.assertFalse([l for l in md if "<img" in l or l.startswith(".shell")], md)

    def test_without_rowid_composite_key(self):
        path = build_evil(os.path.join(self.tmp, "evil.db"))
        b = self.bundle(path, "wr", Locator("pk", ("k", 1)))
        text = b.render("sql")
        self.assertEqual(self.check_script(text, ["wr"]), {"wr"})
        self.assertIn('PRIMARY KEY ("a", "b")\n) WITHOUT ROWID;', text)

    def test_stored_text_after_the_statement(self):
        path = build_evil(os.path.join(self.tmp, "trail.db"), trailer=True)
        try:
            b = self.bundle(path, "wr", Locator("pk", ("k", 1)))
        except Exception:
            if sys.version_info < (3, 12):
                self.skipTest("this Python's sqlite3 refuses to open the crafted schema")
            raise
        text = b.render("sql")
        self.check_script(text, ["wr"])     # rebuilt, or only comments when unparsed
        for line in text.split("\n"):
            if "TRAILER" in line or "evil.db" in line or "pwned" in line:
                self.assertTrue(line.startswith("--"), line)
        md, open_fence = md_outside_fences(b.render("markdown"))
        self.assertEqual(open_fence, "")
        self.assertFalse([l for l in md if "TRAILER" in l], md)
        stmts, dots = cli_statements(dm.create_statements(b.schemas["wr"]))
        self.assertEqual((len(stmts), dots), (1, []))


class MapMarkdownTest(SessionTestBase):
    def test_fences_hold_crafted_text(self):
        path = build_evil(os.path.join(self.tmp, "evil.db"))
        s = self.open(path)
        m = dm.database_map(s, relation_map(s), dm.MapOptions(), tool_version="9.9.9")
        text = dm.map_markdown(m)
        outside, open_fence = md_outside_fences(text)
        self.assertEqual(open_fence, "")
        for line in outside:
            self.assertNotIn("<script", line)
            self.assertNotIn("<img", line)
            self.assertFalse(line.startswith(".shell"), line)
        # the case Markdown demotes headings outside code blocks only
        demoted = dm._demoted(text)
        self.assertIn("\n# hi\n", demoted)
        self.assertEqual(md_outside_fences(demoted)[1], "")

    def test_code_block_and_span_helpers(self):
        for body in ("```", "````\n# x", "a ``````` b", "plain"):
            lines = dm.md_code_block(body)
            self.assertEqual(lines[0].rstrip("sql"), lines[-1])
            outside, open_fence = md_outside_fences("\n".join(lines + ["after"]))
            self.assertEqual((outside, open_fence), (["after"], ""))
        span = dm.md_code_span("a|b`c\nd")
        self.assertEqual(span, "`a\\|b'c d`")
