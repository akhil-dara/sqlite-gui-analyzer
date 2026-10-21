"""The visual query builder's model: tables, joins, the columns shown (with how each is
presented), filters, sort, distinct and a row limit - turned into one read-only SELECT.

The model never changes data and never hides a raw value: a column's presentation (a date
kind, a label) is applied by the grid when it draws the results; the SQL selects the stored
values. Joins are suggested from the relationship engine's trusted links (a declared foreign
key, or a link verified by the values) between a table being added and the tables already in
the query. Identifiers are always quoted; filter values are written as SQL literals (quoted
text, numbers as numbers) so the SQL shown is exactly the SQL run, and can be copied.
"""

import re

from .schema import quote_ident

JOIN_KINDS = ("INNER", "LEFT")
# every condition: key, its plain name, the values it takes (0, 1, 2 or "list")
CONDITIONS = (
    ("=", "is", 1), ("!=", "is not", 1),
    ("<", "is less than / before", 1), ("<=", "is at most / on or before", 1),
    (">", "is more than / after", 1), (">=", "is at least / on or after", 1),
    ("between", "is between", 2), ("not between", "is not between", 2),
    ("in", "is one of", "list"), ("not in", "is none of", "list"),
    ("contains", "contains", 1), ("not contains", "does not contain", 1),
    ("starts with", "starts with", 1), ("ends with", "ends with", 1),
    ("like", "matches the pattern (% = anything, _ = one character)", 1),
    ("glob", "matches the pattern exactly (* = anything, ? = one character, capitals count)",
     1),
    ("is null", "has no value (NULL)", 0), ("is not null", "has a value (not NULL)", 0),
    ("is empty", "is empty (NULL or empty text)", 0),
    ("is not empty", "is not empty", 0))
OPS = tuple(k for k, _l, _n in CONDITIONS)
OP_LABEL = dict((k, label) for k, label, _n in CONDITIONS)
OP_VALUES = dict((k, n) for k, _l, n in CONDITIONS)
TEXT_OPS = ("contains", "not contains", "starts with", "ends with", "like")
ROWID = "rowid"
# a column's summary over the rows of each group (the columns without one are the groups)
AGGREGATES = (("count", "Count", "COUNT(%s)"),
              ("count_distinct", "Count distinct", "COUNT(DISTINCT %s)"),
              ("min", "Smallest", "MIN(%s)"), ("max", "Largest", "MAX(%s)"),
              ("sum", "Sum", "SUM(%s)"), ("avg", "Average", "AVG(%s)"),
              ("list", "List of values", "GROUP_CONCAT(%s, ', ')"))
AGG_LABEL = dict((k, label) for k, label, _sql in AGGREGATES)
AGG_SQL = dict((k, sql) for k, _label, sql in AGGREGATES)
KEEPS_VALUE = (None, "min", "max")      # aggregates whose result is one of the stored values
RECOVERED = "~recovered"        # TableRef.db of the records the Forensics recovery found


def literal(v):
    """A SQL literal for a filter value: numbers as numbers, everything else quoted text."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if re.fullmatch(r"-?\d{1,18}", s):
        return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        return s
    return "'" + s.replace("'", "''") + "'"


def _like(s):
    """Text inside a LIKE pattern: %, _ and \\ matched literally (ESCAPE '\\'), quotes doubled."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")


class TableRef(object):
    """A table in the query; db: the database it belongs to in a case (None: the query's
    one database); by: joined by a field inside another table's values - {"alias",
    "column", "paths", "to"}: the field read out of alias.column (the first of paths with a
    value) equals this table's column 'to'. SQLite cannot read inside the values, so such a
    table is matched by the tool after the query (and is not in the SQL)."""
    __slots__ = ("alias", "table", "db", "by")

    def __init__(self, alias, table, db=None, by=None):
        self.alias, self.table, self.db = alias, table, db
        self.by = dict(by) if by else None

    def source(self, schemas=None):
        """'"table"', or '"db2"."table"' when the query spans databases."""
        if self.db == RECOVERED:
            return "temp.%s" % quote_ident(self.table)
        if schemas and self.db is not None and self.db in schemas:
            return "%s.%s" % (quote_ident(schemas[self.db]), quote_ident(self.table))
        return quote_ident(self.table)


class Join(object):
    """left.alias.left_col = right.alias.right_col; kind INNER or LEFT; why: the evidence."""
    __slots__ = ("left", "left_col", "right", "right_col", "kind", "why")

    def __init__(self, left, left_col, right, right_col, kind="LEFT", why=""):
        self.left, self.left_col, self.right, self.right_col = left, left_col, right, right_col
        self.kind, self.why = kind, why

    def text(self):
        return "%s.%s = %s.%s" % (self.left, self.left_col, self.right, self.right_col)


class Column(object):
    """A column shown: alias.column, its label in the results, and how it is presented
    (show: a date kind of engine.timeline, or None for the stored value; meanings: {stored
    value as text: what it means}, shown next to the value, never instead of it; agg: a key
    of AGGREGATES, or None for the value itself; paths: a field inside the column's values
    - engine.decode.fields paths, the first that has a value is shown - or None; calc:
    engine.calc steps applied to the value (or the field) to make a new column, or None)."""
    __slots__ = ("alias", "column", "label", "show", "meanings", "agg", "paths", "calc")

    def __init__(self, alias, column, label=None, show=None, meanings=None, agg=None,
                 paths=None, calc=None):
        self.alias, self.column, self.label, self.show = alias, column, label, show
        self.meanings = dict(meanings or {})
        self.paths = list(paths) if paths else None
        self.calc = [list(st) for st in calc] if calc else None
        self.agg = agg if agg in AGG_SQL and not self.paths and not self.calc else None

    def made_by_tool(self):
        """True when the tool works out the value after the query (a field, a calculation)."""
        return bool(self.paths or self.calc)

    def heading(self):
        return self.label or "%s.%s" % (self.alias, self.column)

    def field_name(self):
        """'ZDATA › to[0].name' (+N when other paths fill in): a field column's heading."""
        first = self.paths[0]
        short = first[5:] if first.startswith("root.") else first
        return "%s › %s%s" % (self.column, short, " (+%d)" % (len(self.paths) - 1)
                              if len(self.paths) > 1 else "")

    def expr(self):
        e = "%s.%s" % (quote_ident(self.alias), _col(self.column))
        return AGG_SQL[self.agg] % e if self.agg else e


def split_list(text):
    """The values of an 'is one of' list: one per line, or separated by commas."""
    if isinstance(text, (list, tuple)):
        return [str(v) for v in text]
    lines = [x.strip() for x in str(text).splitlines() if x.strip()]
    if len(lines) == 1 and "," in lines[0]:
        lines = [x.strip() for x in lines[0].split(",") if x.strip()]
    return lines


class Filter(object):
    """One condition: alias.column <op> value(s). value2: the 'and' of between; other:
    'alias.column' to compare with another column instead of a value; nocase: capitals do
    not count; date: a date kind (engine.timeline) - the values are dates typed as text
    ('2024-11-21', '2024-11-21 07:51') and compared as the numbers that kind stores; summary:
    the condition is on a summary column (HAVING) - the heading of that column."""
    __slots__ = ("alias", "column", "op", "value", "value2", "other", "nocase", "date",
                 "summary")

    def __init__(self, alias, column, op, value="", value2="", other=None, nocase=False,
                 date=None, summary=None):
        if op not in OPS:
            raise ValueError("unknown condition %r" % op)
        self.alias, self.column, self.op, self.value = alias, column, op, value
        self.value2, self.other, self.nocase = value2, other, bool(nocase)
        self.date, self.summary = date, summary

    def _raw(self, text, end=False):
        """A typed value as SQL: a date as the number its kind stores, else a literal."""
        if self.date:
            from .timeline import from_utc, parse_when
            dt = parse_when(str(text), end=end)
            if dt is None:
                raise ValueError("no date given")
            raw = from_utc(dt, self.date)
            if end and isinstance(raw, float) and self.date != "ole_days":
                raw = int(raw // 1)     # whole-number storage: the last whole unit in range
            return literal(raw)
        return literal(text)

    def sql(self, expr=None):
        """The SQL of the condition (expr: what it is on, for a summary)."""
        c = expr or "%s.%s" % (quote_ident(self.alias), _col(self.column))
        op, v = self.op, self.value
        if op == "is null":
            return "%s IS NULL" % c
        if op == "is not null":
            return "%s IS NOT NULL" % c
        if op == "is empty":
            return "(%s IS NULL OR %s = '')" % (c, c)
        if op == "is not empty":
            return "(%s IS NOT NULL AND %s <> '')" % (c, c)
        coll = " COLLATE NOCASE" if self.nocase else ""
        if self.other:
            a, _dot, col = self.other.partition(".")
            rhs = "%s.%s" % (quote_ident(a), _col(col))
            if op in ("contains", "not contains", "starts with", "ends with"):
                pat = {"contains": "'%%' || %s || '%%'", "not contains": "'%%' || %s || '%%'",
                       "starts with": "%s || '%%'", "ends with": "'%%' || %s"}[op] % rhs
                return "%s %sLIKE %s" % (c, "NOT " if op == "not contains" else "", pat)
            sym = {"!=": "<>", "like": "LIKE", "glob": "GLOB"}.get(op, op)
            if sym in OPS[:6] or sym in ("<>", "LIKE", "GLOB"):
                return "%s %s %s%s" % (c, sym, rhs, coll if sym not in ("LIKE", "GLOB") else "")
            raise ValueError("'%s' cannot compare with another column" % OP_LABEL[op])
        if op in ("contains", "not contains", "starts with", "ends with"):
            body = _like(str(v))
            pat = {"contains": "'%%%s%%'", "not contains": "'%%%s%%'", "starts with": "'%s%%'",
                   "ends with": "'%%%s'"}[op] % body
            return "%s %sLIKE %s ESCAPE '\\'" % (c, "NOT " if op == "not contains" else "",
                                                  pat)
        if op == "like":
            return "%s LIKE '%s'" % (c, str(v).replace("'", "''"))   # a pattern is text
        if op == "glob":
            return "%s GLOB '%s'" % (c, str(v).replace("'", "''"))
        if op in ("between", "not between"):
            return "%s %sBETWEEN %s AND %s%s" % (
                c, "NOT " if op == "not between" else "", self._raw(v),
                self._raw(self.value2, end=True), coll)
        if op in ("in", "not in"):
            items = split_list(v)
            if not items:
                raise ValueError("the list is empty")
            return "%s %sIN (%s)%s" % (c, "NOT " if op == "not in" else "",
                                       ", ".join(self._raw(x) for x in items), coll)
        # a date alone after '<=' / '>' means its whole day: 'on or before 21 Nov' includes it
        end = bool(self.date) and op in ("<=", ">")
        return "%s %s %s%s" % (c, "<>" if op == "!=" else op, self._raw(v, end=end), coll)

    def text(self, label=None):
        """'type is one of RECEIVED_PAYMENT, SENT_PAYMENT' - the condition in plain words."""
        what = self.summary or "%s.%s" % (label(self.alias) if label else self.alias,
                                          self.column)
        n = OP_VALUES[self.op]
        if n == 0:
            rest = ""
        elif self.other:
            rest = " the column %s" % self.other
        elif n == 2:
            rest = " %s and %s" % (self.value, self.value2)
        elif n == "list":
            items = split_list(self.value)
            rest = " " + ", ".join(items[:6]) + (" … (%d values)" % len(items)
                                                 if len(items) > 6 else "")
        else:
            rest = " '%s'" % self.value
        extra = []
        if self.date:
            from .timeline import LABELS
            extra.append("dates in UTC, stored as %s" % LABELS.get(self.date, self.date))
        if self.nocase:
            extra.append("capitals ignored")
        return "%s %s%s%s" % (what, OP_LABEL[self.op].split(" (")[0], rest,
                              " (%s)" % "; ".join(extra) if extra else "")

    def to_list(self):
        return {"alias": self.alias, "column": self.column, "op": self.op, "value": self.value,
                "value2": self.value2, "other": self.other, "nocase": self.nocase,
                "date": self.date, "summary": self.summary}

    @classmethod
    def from_saved(cls, d):
        if isinstance(d, dict):
            if "items" in d:
                return Group(d.get("mode", "any"), [cls.from_saved(x) for x in d["items"]])
            return cls(**d)
        return cls(*d)                  # an older save: [alias, column, op, value]


class Group(object):
    """Conditions combined among themselves: mode 'any' (OR) or 'all' (AND), as one condition
    of the list above it."""
    __slots__ = ("mode", "items")

    def __init__(self, mode="any", items=None):
        self.mode = mode if mode in ("any", "all") else "any"
        self.items = list(items or [])

    def sql(self, exprs=None):
        parts = [f.sql() for f in self.items]
        if not parts:
            return None
        return "(" + (" OR " if self.mode == "any" else " AND ").join(parts) + ")"

    def text(self, label=None):
        return "%s of: %s" % ("any" if self.mode == "any" else "all",
                              "; ".join(f.text(label) for f in self.items))

    def to_list(self):
        return {"mode": self.mode, "items": [f.to_list() for f in self.items]}

    @property
    def alias(self):
        return self.items[0].alias if self.items else None

    @property
    def summary(self):
        return None


def _col(name):
    return ROWID if name == ROWID else quote_ident(name)


class QueryModel(object):
    def __init__(self):
        self.tables = []            # [TableRef], the first is the FROM table
        self.joins = []             # [Join], each joins a later table to an earlier one
        self.columns = []           # [Column] in result order
        self.filters = []           # [Filter or Group]; match: "all" (AND) or "any" (OR)
        self.match = "all"
        self.offset = 0             # rows skipped before the first shown (OFFSET)
        self.sort = []              # [(alias, column, descending)]
        self.distinct = False
        self.count = False          # one line per distinct shown values, with how many rows
        self.limit = None           # None: every row (no LIMIT)

    # -- tables and joins ------------------------------------------------------------------------
    def alias_for(self, table):
        used = set(t.alias for t in self.tables)
        base = re.sub(r"\W+", "_", table).strip("_")[:24] or "t"
        alias, n = base, 2
        while alias in used:
            alias, n = "%s_%d" % (base, n), n + 1
        return alias

    def add_table(self, table, joins=(), db=None):
        """Add a table (a second copy gets its own alias); joins: [Join] to tables already
        there (from suggest_joins); db: its database in a case. Returns its alias."""
        ref = TableRef(self.alias_for(table), table, db)
        self.tables.append(ref)
        for j in joins:
            j.right = ref.alias if j.right is None else j.right
            j.left = ref.alias if j.left is None else j.left
            self.joins.append(j)
        return ref.alias

    def remove_table(self, alias):
        self.tables = [t for t in self.tables if t.alias != alias]
        self.joins = [j for j in self.joins if alias not in (j.left, j.right)]
        self.columns = [c for c in self.columns if c.alias != alias]
        kept = []
        for f in self.filters:
            if isinstance(f, Group):
                f.items = [x for x in f.items if x.alias != alias]
                if f.items:
                    kept.append(f)
            elif f.alias != alias:
                kept.append(f)
        self.filters = kept
        self.sort = [s for s in self.sort if s[0] != alias]

    def table(self, alias):
        return next((t for t in self.tables if t.alias == alias), None)

    def unjoined(self):
        """Aliases after the first that no join reaches (they would multiply every row)."""
        if not self.tables:
            return []
        reached = {self.tables[0].alias}
        changed = True
        while changed:
            changed = False
            for j in self.joins:
                if (j.left in reached) != (j.right in reached):
                    reached.update((j.left, j.right))
                    changed = True
        return [t.alias for t in self.tables if t.alias not in reached and not t.by]

    # -- tables joined by a field (matched by the tool after the query) ---------------------------
    def add_by_field(self, table, alias, column, paths, to, db=None):
        """Join table on a field inside alias.column's values (see TableRef.by)."""
        ref = TableRef(self.alias_for(table), table, db,
                       {"alias": alias, "column": column, "paths": list(paths), "to": to})
        self.tables.append(ref)
        return ref.alias

    def by_field(self):
        """The tables joined by a field, in order."""
        return [t for t in self.tables if t.by]

    def in_sql(self, c):
        """True when the column comes from the SQL (not from a table joined by a field)."""
        t = self.table(c.alias)
        return t is None or not t.by

    def key_name(self, t):
        """The result column that carries the stored value a field join reads its key from."""
        return "~key %s" % t.alias

    # -- columns --------------------------------------------------------------------------------
    def toggle_column(self, alias, column):
        for c in self.columns:
            if (c.alias, c.column) == (alias, column) and not c.made_by_tool():
                self.columns.remove(c)
                return False
        self.columns.append(Column(alias, column))
        return True

    def has_column(self, alias, column):
        return any((c.alias, c.column) == (alias, column) and not c.made_by_tool()
                   for c in self.columns)

    def add_field(self, alias, column, paths, label=None):
        """A field inside alias.column's values as a column of its own (paths: the first
        that has a value in a row is shown). Returns the Column."""
        c = Column(alias, column, label=label, paths=paths)
        self.columns.append(c)
        return c

    def add_calc(self, source, steps, label):
        """A new column: source (a Column: a stored value or a field) with the steps applied.
        Placed right after the source."""
        c = Column(source.alias, source.column, label=label, paths=source.paths, calc=steps)
        at = self.columns.index(source) + 1 if source in self.columns else len(self.columns)
        self.columns.insert(at, c)
        return c

    def fields(self):
        """(index, Column) of the field columns (read out of the values after the query)."""
        return [(i, c) for i, c in enumerate(self.columns) if c.paths]

    def headings(self):
        """Each column's heading in the results: its label; else its name, or 'alias.name'
        when two shown columns share the name (never two columns called the same)."""
        names = [c.column.lower() for c in self.columns
                 if not c.label and not c.agg and not c.made_by_tool()]
        out = []
        for c in self.columns:
            if c.label:
                out.append(c.label)
            elif c.calc:
                out.append("%s (calculated)" % (c.field_name() if c.paths else c.column))
            elif c.paths:
                out.append(c.field_name())
            elif c.agg:
                out.append("%s of %s" % (AGG_LABEL[c.agg].lower(), c.column))
            else:
                out.append(c.column if names.count(c.column.lower()) == 1
                           else "%s.%s" % (c.alias, c.column))
        return out

    def grouped(self):
        """True when a column is summarised: the others are the groups (GROUP BY)."""
        return self.count or any(c.agg for c in self.columns)

    # -- SQL --------------------------------------------------------------------------------------
    def databases(self):
        """The databases the query reads, in order (None: the one database)."""
        out = []
        for t in self.tables:
            if t.db not in out:
                out.append(t.db)
        return out

    def spans_databases(self):
        return len(self.databases()) > 1

    def to_sql(self, schemas=None):
        """The SELECT, as shown and run (read-only); schemas {db: schema name} when the query
        spans databases (engine.crossquery attaches them under those names)."""
        if not self.tables:
            return ""
        shown = [(c, h) for c, h in zip(self.columns, self.headings()) if self.in_sql(c)]
        exprs = [c.expr() for c, _h in shown]
        cols = [e + (" AS %s" % quote_ident(h) if h != c.column or c.agg or c.made_by_tool()
                     else "")
                for e, (c, h) in zip(exprs, shown)]
        group = [e for e, (c, _h) in zip(exprs, shown) if not c.agg] \
            if self.grouped() else []
        if self.count:
            cols.append('COUNT(*) AS "rows"')
        if not cols:
            cols = ["%s.*" % quote_ident(t.alias) for t in self.tables if not t.by]
        for t in self.by_field():       # the value each field join reads its key out of
            cols.append("%s.%s AS %s" % (quote_ident(t.by["alias"]), _col(t.by["column"]),
                                         quote_ident(self.key_name(t))))
        lines = ["SELECT %s%s" % ("DISTINCT " if self.distinct else "", ",\n       ".join(cols))]
        first = self.tables[0]
        lines.append("FROM %s AS %s" % (first.source(schemas), quote_ident(first.alias)))
        placed = {first.alias}
        pending = [t for t in self.tables[1:] if not t.by]
        while pending:
            progress = False
            for t in list(pending):
                conds = [j for j in self.joins if t.alias in (j.left, j.right) and
                         (j.right if j.left == t.alias else j.left) in placed]
                if not conds:
                    continue
                kind = "LEFT" if any(j.kind == "LEFT" for j in conds) else "INNER"
                on = " AND ".join("%s.%s = %s.%s" % (quote_ident(j.left), _col(j.left_col),
                                                     quote_ident(j.right), _col(j.right_col))
                                  for j in conds)
                lines.append("%s JOIN %s AS %s ON %s" % (kind, t.source(schemas),
                                                         quote_ident(t.alias), on))
                placed.add(t.alias)
                pending.remove(t)
                progress = True
            if not progress:
                for t in pending:       # no join: every combination (the UI warns)
                    lines.append("CROSS JOIN %s AS %s" % (t.source(schemas),
                                                          quote_ident(t.alias)))
                break
        sql_alias = set(t.alias for t in self.tables if not t.by)
        joiner = "\n  AND " if self.match == "all" else "\n   OR "

        def ok(f):
            if isinstance(f, Group):
                return all(x.alias in sql_alias for x in f.items) and f.items
            return f.alias in sql_alias and not f.summary
        where = [f.sql() for f in self.filters if ok(f)]
        sort = [x for x in self.sort if x[0] in sql_alias]
        if where:
            lines.append("WHERE " + joiner.join(w for w in where if w))
        if group:
            lines.append("GROUP BY " + ", ".join(group))
        having = []
        heads = dict(zip(self.headings(), self.columns))
        for f in self.filters:
            if isinstance(f, Filter) and f.summary and f.summary in heads:
                having.append(f.sql(heads[f.summary].expr()))
        if having and group is not None and (group or self.grouped()):
            lines.append("HAVING " + joiner.join(having))
        if sort:
            lines.append("ORDER BY " + ", ".join(
                "%s.%s%s" % (quote_ident(a), _col(c), " DESC" if d else "")
                for a, c, d in sort))
        if self.limit or self.offset:
            lines.append("LIMIT %d" % int(self.limit or -1) +
                         (" OFFSET %d" % int(self.offset) if self.offset else ""))
        return "\n".join(lines)

    # -- saving ---------------------------------------------------------------------------------
    def to_dict(self):
        return {"tables": [[t.alias, t.table] + ([t.db, t.by] if t.by else [t.db]
                                                  if t.db is not None else [])
                           for t in self.tables],
                "joins": [[j.left, j.left_col, j.right, j.right_col, j.kind, j.why]
                          for j in self.joins],
                "columns": [[c.alias, c.column, c.label, c.show, c.meanings] +
                            ([c.agg, c.paths, c.calc] if c.calc else [c.agg, c.paths]
                             if c.paths else [c.agg] if c.agg else [])
                            for c in self.columns],
                "filters": [f.to_list() for f in self.filters], "match": self.match,
                "offset": self.offset,
                "sort": [list(s) for s in self.sort], "distinct": self.distinct,
                "count": self.count,
                "limit": self.limit}

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.tables = [TableRef(*t) for t in d.get("tables", ())]
        m.joins = [Join(*j) for j in d.get("joins", ())]
        m.columns = [Column(*c) for c in d.get("columns", ())]
        m.filters = [Filter.from_saved(f) for f in d.get("filters", ())]
        m.match = d.get("match", "all") if d.get("match") in ("all", "any") else "all"
        m.offset = int(d.get("offset") or 0)
        m.sort = [tuple(s) for s in d.get("sort", ())]
        m.distinct = bool(d.get("distinct"))
        m.count = bool(d.get("count"))
        m.limit = d.get("limit")
        return m


def describe(model, unique=None, label=None, rows=None, looking=()):
    """What the query gives, in plain words, one sentence per line: what one line of the
    results is, what each joined table adds and whether lines without a match are kept,
    when a join repeats lines, the conditions, the summary and the limit.

    unique(alias, column) -> True when the schema makes the column's values unique (a join
    on it adds at most one row); label(alias) -> the table's name as shown; rows(alias) ->
    its row count or None."""
    if not model.tables:
        return ""
    unique = unique or (lambda a, c: False)
    label = label or (lambda a: model.table(a).table)
    first = model.tables[0].alias
    base = label(first)
    lines = []
    if model.grouped():
        groups = [c for c in model.columns if not c.agg]
        lines.append("One line per different %s%s." % (
            " + ".join(c.column for c in groups) if groups else "(everything together)",
            ", with how many rows have it" if model.count else ""))
    else:
        n = rows(first) if rows else None
        lines.append("One line per row of %s%s." % (
            base, " (%s rows)" % (format(n, ",") if isinstance(n, int) else n)
            if n is not None else ""))
    reached = {first}
    pending = list(model.joins)
    for _ in range(len(model.joins) + 1):
        left = []
        for j in pending:
            if j.left in reached and j.right not in reached:
                near, ncol, far, fcol = j.left, j.left_col, j.right, j.right_col
            elif j.right in reached and j.left not in reached:
                near, ncol, far, fcol = j.right, j.right_col, j.left, j.left_col
            else:
                if j.left not in reached or j.right not in reached:
                    left.append(j)
                continue
            reached.add(far)
            who, other = label(near), label(far)
            parts = ["+ %s, matched on %s.%s = %s.%s" % (other, who, ncol, other, fcol),
                     "if none matches: the %s row is kept, %s cells empty" % (who, other)
                     if j.kind == "LEFT" else "if none matches: the %s row is left out" % who]
            if not unique(far, fcol):
                # the schema does not make the column unique: a repeat is possible, not known
                parts.append("if several %s rows match, the %s row repeats once per match"
                             % (other, who))
            lines.append(" · ".join(parts))
        if len(left) == len(pending):
            break
        pending = left
    for t in model.by_field():
        src = t.by
        lines.append("+ %s, matched by the tool after the query: %s.%s = the field %s inside "
                     "%s.%s · if none matches: the row is kept, %s cells empty · if several "
                     "match, the row repeats once per match" % (
                         label(t.alias), label(t.alias), src["to"], ", else ".join(src["paths"]),
                         label(src["alias"]), src["column"], label(t.alias)))
    for c in model.columns:
        if c.calc:
            from . import calc as calcs
            lines.append("%s: %s %s (calculated by the tool; the stored value is unchanged)"
                         % (c.label or "calculated", "the field %s of %s.%s," % (
                             " / ".join(c.paths), label(c.alias), c.column) if c.paths else
                            "%s.%s," % (label(c.alias), c.column), calcs.describe(c.calc)))
            continue
        if c.paths:
            lines.append("%s: read out of %s.%s (decoded) - %s; empty where %s" % (
                c.label or c.field_name(), label(c.alias), c.column,
                ", else ".join(c.paths), "it has no value" if len(c.paths) == 1
                else "none of them has a value"))
    lone = [a for a in model.unjoined() if a not in looking]
    wait = [a for a in model.unjoined() if a in looking]
    if wait:
        lines.append("Looking for the links of %s…" % ", ".join(label(a) for a in wait))
    if lone:
        lines.append("Not joined: %s - every row of it is paired with every line above "
                     "(join it to one of the tables)." % ", ".join(label(a) for a in lone))
    conds = [f for f in model.filters if not getattr(f, "summary", None)]
    if conds:
        lines.append("Only rows where %s: %s." % (
            "all of these hold" if model.match == "all" or len(conds) == 1
            else "any of these holds", "; ".join(f.text(label) for f in conds)))
    sums = [f for f in model.filters if getattr(f, "summary", None)]
    if sums:
        lines.append("Only groups where: %s." % "; ".join(f.text(label) for f in sums))
    if model.distinct:
        lines.append("Lines that are exactly alike are shown once.")
    if model.offset:
        lines.append("The first %s lines are skipped." % format(int(model.offset), ","))
    if model.limit:
        lines.append("At most %s lines." % format(int(model.limit), ","))
    return "\n".join(lines)


def parse_meanings(text):
    """'1 = incoming' lines -> {'1': 'incoming'} (blank lines and lines without '=' ignored);
    raises ValueError naming a line whose value is empty."""
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        if "=" not in line:
            continue
        k, _eq, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if not k:
            raise ValueError("line %d has no value before '='" % n)
        out[k] = v
    return out


def meanings_text(meanings):
    return "\n".join("%s = %s" % kv for kv in meanings.items())


def meaning_formatter(meanings):
    """fn(value) -> 'value (meaning)' for the grid; None for a value without a meaning (the
    grid then draws the value as usual)."""
    def f(v):
        if v is None:
            return None
        key = repr(v) if isinstance(v, float) and not v.is_integer() else (
            str(int(v)) if isinstance(v, float) else str(v))
        hit = meanings.get(key)
        return None if hit is None else "%s (%s)" % (key, hit)
    return f


def suggest_joins(rmap, model, table, cancel=None):
    """[Join] between `table` (about to be added; its alias is filled in by add_table) and
    the tables already in the model, from the trusted relations of the relationship engine
    (engine.relations.RelationMap): a declared foreign key or a link verified by values."""
    from .relations import ROWID as REL_ROWID
    out = []
    present = dict((t.table, t.alias) for t in model.tables)
    if not present:
        return out

    def name(c):
        return ROWID if c is REL_ROWID else c
    try:
        cols = rmap.session.info(table).column_names if hasattr(rmap, "session") else None
    except Exception:               # noqa: BLE001
        cols = None
    seen = set()
    for col in (cols or []):
        try:
            rels = rmap.confident(table, col, cancel) or []
        except Exception:           # noqa: BLE001 - a column without trusted links
            continue
        for r in rels:
            other = r.other
            if other not in present or other == table:
                continue
            key = (col, other, str(r.other_column))
            if key in seen:
                continue
            seen.add(key)
            why = "declared foreign key" if r.kind == "fk" else "verified by values"
            j = Join(None, name(col), present[other], name(r.other_column), "LEFT", why)
            out.append((_link_score(rmap, table, name(col), other, name(r.other_column),
                                    r.kind == "fk"), len(out), j))
    out.sort(key=lambda s: (-s[0], s[1]))
    return [j for _s, _i, j in out]


def _stem(name):
    """'chat_row_id' -> 'chat', 'ZCHATSESSION' -> 'chatsession', 'jid' -> 'jid': a name with
    the usual key decorations taken off, for telling which table a key column points at."""
    name = name or ""
    s = name.lower().replace("-", "_")
    if name[:1] == "Z" and name.isupper():  # Core Data: ZCHAT, ZCHATSESSION, Z_PK
        s = s[1:].lstrip("_")
    for end in ("_row_id", "_rowid", "_id", "rowid", "id", "_ref", "_key"):
        if s.endswith(end) and len(s.rstrip("_")) >= len(end) + 3:
            s = s[:-len(end)]
            break
    for end in ("entity", "s"):
        if s.endswith(end) and len(s) > len(end) + 2:
            s = s[:-len(end)]
    return s.strip("_")


def _unique_col(rmap, table, col):
    """True when col is table's rowid, its INTEGER PRIMARY KEY or its one-column key."""
    if col == ROWID:
        return True
    try:
        info = rmap.session.info(table)
    except Exception:               # noqa: BLE001
        return False
    names = info.column_names
    return len(info.pk_columns) == 1 and names[info.pk_columns[0]] == col


def _link_score(rmap, t1, c1, t2, c2, declared):
    """How likely t1.c1 = t2.c2 is the link meant, when several trusted links tie two tables
    (WhatsApp chat/message share eight): a declared key first, then the key column whose name
    is the other table's name (chat.jid_row_id -> jid), then one that contains it
    (account_jid_row_id), each pointing at the other table's own key."""
    score = 100 if declared else 0
    u1, u2 = _unique_col(rmap, t1, c1), _unique_col(rmap, t2, c2)
    if u1 != u2:                    # one side is a key: the other side points at it
        ref_col, key_table = (c2, t1) if u1 else (c1, t2)
        score += 10
        a, b = _stem(ref_col), _stem(key_table)
        if a and b:
            if a == b:
                score += 50
            elif b in a or a in b:
                score += 20
    for c in (c1, c2):              # what kind of value the columns hold, by their names
        score += _name_kind(c)
    if (c1 or "").lower() == (c2 or "").lower():
        score += 3
    return score


_ID_END = ("id", "uuid", "guid", "key", "_pk", "jid", "ref")
_NOT_LINK = re.compile(r"(date|time|stamp|created|updated|modified|expir|priority|count|"
                       r"status|state|flag|order|sort|rank|level|version|position|index|"
                       r"seq|amount|price|size|length|width|height)")


def _name_kind(col):
    """+ for a name that says identifier (..._id, ...uuid), - for one that says a date, a
    count, an order or a state: equal dates or priorities tie rows by accident."""
    n = (col or "").lower()
    if n == "rowid" or n.endswith(_ID_END):
        return 8
    if _NOT_LINK.search(n) or (n.endswith("at") and n[:1] == "z" and len(n) > 4):
        return -30                  # ZCREATEDAT, ZUPDATEDAT: Core Data dates
    return 0
