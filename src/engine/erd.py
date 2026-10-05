"""The entity-relationship diagram of the Relationships tab (no Tk): a model of the tables with
all their columns and the relationships between exact columns, a layered layout, and SVG.

Model (ErdModel)
  Every table shown, with ALL its columns (name, declared type, primary key, 'FK' when the
  column is the source of a link, 'target' when a link points at it) and its row count. Each
  link is one Relationship from an exact source column to an exact target column, so its
  connector attaches to those two column rows. Two links between the same tables stay two
  relationships (two connectors, each labelled 'src_col → dst_col'); a table referring to
  itself gets a loop on the card's right side.

  Cardinality, (referenced side, referring side):
    - the referenced side is '1' when its column is unique by the schema (the rowid, the
      INTEGER PRIMARY KEY, a one-column PRIMARY KEY or UNIQUE index), else '*';
    - the referring side is '1' when its column is unique by the schema, or when the link's
      value check read ALL its distinct values and found as many as the table has rows; '*'
      when that check found fewer (values repeat, or some are NULL), and '*' when nothing
      tells (then marked 'from schema' - never a claim about values that were not read).
    ('1', '*') is one-to-many, ('1', '1') one-to-one.

  Junction tables: a table whose columns refer to two or more other tables, with at most
  JUNCTION_OTHER other columns and nothing referring to it. With junctions collapsed the table
  is left out and each pair of the tables it joins gets one many-to-many relationship, named
  after it ('via message_tag').

  Groups: at least diagram_group_min tables (engine.limits) linked to the same table in exactly
  the same way and to nothing else are one group card listing them (name and row count),
  folded or listed.

Layout (arrange)
  Layered, left to right along the links (a table left of the tables it refers to): cycles
  broken by a depth-first search, layers by longest path, a layer taller than about the
  diagram's width split into several, long links passing through the layers between by
  reserved slots, the order within each layer chosen by barycenter sweeps (fewest crossings
  kept) and then by swapping neighbouring cards wherever that removes crossings, vertical positions pulled towards the linked rows without overlapping (isotonic
  regression per layer), connectors routed at right angles from the exact column rows through
  the gaps between layers, each vertical run in its own lane. Components (tables linked to
  each other) are laid out apart and packed side by side. Deterministic: the same model gives
  the same picture. Positions are plain {card key: (x, y)}; tidy() keeps moved cards where
  they are but removes overlaps and aligns them.
"""

import bisect
import html
import math
import time

from . import limits, sqlsafe
from .linkgraph import DB, col_name, node_label
from .schema import quote_ident, register_collations, rename_create_table, replace_collations
from .tags import valid_color


def escape(text):
    """Text for SVG content or a quoted attribute value: & < > " ' all escaped."""
    return html.escape(str(text), quote=True)

# -- geometry (pixels at zoom 1) -------------------------------------------------------------
HEAD_H = 28                 # a card's header strip
ROW_H = 19                  # a column row
PAD = 8                     # text inset in a card
MARK_W = 44                 # the PK / FK markers left of a column's name
TYPE_GAP = 16               # least room between a column's name and its type
BOTTOM = 4                  # below the last row
TOGGLE_W = 16               # the header's fold glyph
EDGE_W = 3                  # a database's colour on a card's left edge (a case)
CARD_W_MIN, CARD_W_MAX = 170, 480
GAP_Y = 28                  # between two cards of a layer
DUMMY_H = 8                 # the slot a long connector takes in a layer it crosses
DUMMY_GAP = 4
GUTTER_MIN = 56             # least gap between two layers
LANE = 6                    # between two vertical runs side by side
GUTTER_PAD = 12
LOOP_W = 12                 # a self-reference's loop beside its card
LABEL_H = 14
COMP_GAP = 90               # between two components
MARGIN = 30
SPLIT_MIN = 900             # a layer is split when taller than this and than the diagram wide
JUNCTION_OTHER = 2          # other columns a junction table may have (besides its keys)
SWEEPS = 16

# connector styles: (colour, dash) - solid declared keys, dashed checked links, dotted weaker,
# amber short dashes between databases
PRIMARY = "#1e40af"
MUTED = "#475569"
AMBER = "#d97706"
STYLES = {"declared": (PRIMARY, ()), "verified": (PRIMARY, (6, 4)), "weaker": (MUTED, (2, 3)),
          "cross": (AMBER, (3, 2)), "nm": (PRIMARY, (8, 3, 2, 3))}

CARD_WORDS = {("1", "*"): "one-to-many", ("1", "1"): "one-to-one", ("*", "*"): "many-to-many",
              ("*", "1"): "many-to-one"}


class TextMetrics(object):
    """Text widths without a screen (SVG, tests): generous per-character estimates. The view
    passes one measuring with its Tk fonts; width(text, kind) in pixels, px(kind) the font's
    pixel size."""
    SIZES = {"name": (12, 7.4), "col": (11, 6.6), "pk": (11, 7.0), "type": (10, 6.0),
             "badge": (8, 5.6), "label": (10, 6.0), "member": (11, 6.6), "rows": (10, 6.0),
             "note": (10, 6.0)}

    def width(self, text, kind):
        per = self.SIZES[kind][1]
        return sum(per * (2 if ord(ch) > 0x2e80 else 1) for ch in text)

    def px(self, kind):
        return self.SIZES[kind][0]


def rows_text(n):
    if n is None:
        return "? rows"
    return "%s row%s" % (format(n, ","), "" if n == 1 else "s")


# -- schema facts -------------------------------------------------------------------------------
class TableSpec(object):
    """What the schema says of a table: columns [(name, declared type, pk position)], the
    columns unique by the schema {lower name: why}, whether it has a rowid, its row count."""
    __slots__ = ("columns", "unique", "rowid", "rows")

    def __init__(self, columns, unique=None, rowid=True, rows=None):
        self.columns = list(columns)
        self.unique = dict(unique or {})
        self.rowid = rowid
        self.rows = rows


def unique_indexes(session, tables):
    """{table: {lower column: why}} of the one-column UNIQUE indexes and constraints (and
    non-INTEGER one-column PRIMARY KEYs), from the CREATE statements replayed in a scratch
    in-memory database. Cached on the session; nothing is read from the database file."""
    cache = session.__dict__.setdefault("_erd_unique", {})
    todo = [t for t in tables if t not in cache]
    if not todo:
        return dict((t, cache[t]) for t in tables)
    with sqlsafe.Scratch() as scratch:
        try:
            unregistered = register_collations(scratch.conn, session.schema.collations)
        except Exception:           # noqa: BLE001 - collations only matter to the replay
            unregistered = set()
        by_table = {}
        for e in session.schema.of_type("index"):
            if e.sql:
                by_table.setdefault(e.tbl_name, []).append(e.sql)
        for t in todo:
            out = cache[t] = {}
            try:
                info = session.info(t)
                sql = rename_create_table(info.sql, t)
                if not sql:
                    continue
                scratch.replay(replace_collations(sql, unregistered))
            except sqlsafe.SQL_ERRORS + (KeyError, AttributeError):
                continue
            for isql in by_table.get(t, ()):
                try:
                    scratch.replay(replace_collations(isql, unregistered))
                except sqlsafe.SQL_ERRORS:
                    pass
            try:
                for r in scratch.pragma("PRAGMA index_list(%s)" % quote_ident(t)):
                    unique, origin = r[2], r[3] if len(r) > 3 else "c"
                    partial = len(r) > 4 and r[4]
                    if not unique or partial or not isinstance(r[1], str):
                        continue
                    cols = scratch.pragma("PRAGMA index_info(%s)" % quote_ident(r[1]))
                    if len(cols) != 1 or not isinstance(cols[0][2], str):
                        continue
                    why = {"pk": "PRIMARY KEY", "u": "UNIQUE constraint"}.get(origin,
                                                                              "UNIQUE index")
                    out.setdefault(cols[0][2].lower(), why)
            except sqlsafe.SQL_ERRORS:
                pass
    return dict((t, cache[t]) for t in tables)


def table_specs(session, tables, rows=None):
    """{table: TableSpec} for tables of one session (rows: table -> row count or None)."""
    uniq = unique_indexes(session, tables)
    out = {}
    for t in tables:
        try:
            info = session.info(t)
        except Exception:           # noqa: BLE001 - a table the schema cannot describe
            continue
        cols = [(c.name, c.decl_type, c.pk_pos) for c in info.columns if c.hidden != 1]
        unique = dict(uniq.get(t, {}))
        if info.rowid_alias is not None:
            unique[info.columns[info.rowid_alias].name.lower()] = \
                "INTEGER PRIMARY KEY (the rowid)"
        pks = [c for c in info.columns if c.pk_pos]
        if len(pks) == 1:
            unique.setdefault(pks[0].name.lower(), "PRIMARY KEY")
        has_rowid = not info.without_rowid and bool(info.rowid_name)
        out[t] = TableSpec(cols, unique, has_rowid, rows(t) if rows is not None else None)
    return out


# -- the model -------------------------------------------------------------------------------
class ErdColumn(object):
    __slots__ = ("name", "type", "pk", "fk", "target", "unique", "pseudo")

    def __init__(self, name, type_="", pk=False, unique=None, pseudo=False):
        self.name, self.type, self.pk = name, type_ or "", pk
        self.fk = False             # the source of a link
        self.target = False         # a link points at it
        self.unique = unique        # why the schema makes it unique, or None
        self.pseudo = pseudo        # the rowid, not a declared column

    @property
    def linked(self):
        return self.fk or self.target


class ErdTable(object):
    kind = "table"

    def __init__(self, key, columns, rows, spec_known):
        self.key = key
        self.label = node_label(key)
        self.columns = columns
        self.rows = rows
        self.spec_known = spec_known    # False: columns known only from the links

    def column(self, name):
        for c in self.columns:
            if c.name == name:
                return c
        low = name.lower()
        for c in self.columns:
            if c.name.lower() == low:
                return c
        return None


class ErdGroup(object):
    """Tables linked to one table (anchor) in exactly the same way and to nothing else."""
    kind = "group"

    def __init__(self, anchor, pattern, members):
        self.anchor = anchor
        self.pattern = pattern      # ((role, member column, anchor column, kind, cross, peer),)
        self.members = members      # [(node, rows)] sorted by name
        self.key = "group|%s|%s|%r" % (anchor[0], anchor[1], pattern)
        self.label = "%d tables" % len(members)

    def lines(self):
        name = node_label(self.anchor)
        out = []
        for role, mine, theirs, _k, _c, peer in self.pattern:
            if peer:
                out.append("%s = %s.%s" % (mine, name, theirs))
            elif role == "src":
                out.append("%s → %s.%s" % (mine, name, theirs))
            else:
                out.append("%s.%s → %s" % (name, theirs, mine))
        return out

    def text(self):
        return "%s · %s" % (self.label, ", ".join(self.lines()))


class Relationship(object):
    """One connector: from src (card key) column src_col to dst column dst_col."""

    def __init__(self, rid, src, src_col, dst, dst_col, links, style, cross=False, peer=False):
        self.rid = rid
        self.src, self.src_col, self.dst, self.dst_col = src, src_col, dst, dst_col
        self.links = links
        self.link = links[0] if links else None
        self.style = style          # declared | verified | weaker | cross | nm
        self.cross, self.peer = cross, peer
        self.group = None           # the group card's key when one end is a group
        self.junction = None        # the junction table (key) of a many-to-many relationship
        self.junction_cols = None   # (junction column to src, junction column to dst)
        self.cardinality = ("1", "*")   # (referenced side, referring side)
        self.src_card, self.dst_card = "*", "1"
        self.basis = "schema"       # 'schema' | 'values' | 'schema and values' | 'junction'
        self.evidence_src = self.evidence_dst = ""
        self.label = "%s → %s" % (src_col, dst_col) if not peer else "%s = %s" % (src_col,
                                                                                  dst_col)

    @property
    def self_loop(self):
        return self.src == self.dst

    def card_text(self):
        """'one-to-many' (and 'from schema' when no values tell)."""
        return CARD_WORDS[self.cardinality]

    def kind_text(self):
        if self.junction is not None:
            return "many-to-many through %s" % node_label(self.junction)
        if self.link is None:
            return ""
        if len(self.links) > 1:
            return "%d links, %s" % (len(self.links), self.link.kind_text())
        return self.link.kind_text()

    def evidence(self):
        return self.link.evidence() if self.link is not None and self.junction is None else ""

    def reason(self):
        if self.junction is not None:
            return "; ".join(l.reason for l in self.links if l.reason)
        return self.link.reason if self.link is not None else ""

    def cardinality_text(self):
        """The cardinality, what it rests on, both sides."""
        base = "%s (%s)" % (self.card_text(), {"schema": "from the schema",
                                               "values": "from the values",
                                               "schema and values": "from the schema and the "
                                                                    "values",
                                               "junction": "through a junction table"}[
            self.basis])
        parts = [base]
        if self.evidence_dst:
            parts.append("%s: %s" % (self.dst_col_text(), self.evidence_dst))
        if self.evidence_src:
            parts.append("%s: %s" % (self.src_col_text(), self.evidence_src))
        return "\n".join(parts)

    def src_col_text(self):
        return "%s.%s" % (_short(self.src), self.src_col)

    def dst_col_text(self):
        return "%s.%s" % (_short(self.dst), self.dst_col)

    def title(self):
        return "%s → %s" % (self.src_col_text(), self.dst_col_text()) if not self.peer else \
            "%s = %s" % (self.src_col_text(), self.dst_col_text())

    def join_sql(self, model=None):
        """A sample SELECT joining both sides (text to copy, never run here)."""
        g = model.groups.get(self.group) if self.group is not None and model is not None             else None
        src, dst = self._example(self.src, g), self._example(self.dst, g)
        head = []
        if g is not None:
            head.append("-- %s is one of the %d tables of the group" % (
                (src if isinstance(self.src, str) else dst)[1], len(g.members)))
        if self.cross:
            head.append("-- the tables are in two databases of the case (matched by value): "
                        "ATTACH one to the other first, e.g.")
            head.append("-- ATTACH DATABASE '<path of %s>' AS %s;" % (dst[0], quote_ident(
                _alias_name(dst[0]))))
        s_t = _qualified(src, False)
        d_t = _qualified(dst, self.cross)
        if self.junction is not None:
            j = self.junction
            jc_src, jc_dst = self.junction_cols
            sql = ("SELECT s.*, d.*\nFROM %s AS s\nJOIN %s AS j ON j.%s = s.%s\n"
                   "JOIN %s AS d ON d.%s = j.%s;" % (
                       s_t, quote_ident(j[1]), _col(jc_src), _col(self.src_col), d_t,
                       _col(self.dst_col), _col(jc_dst)))
        else:
            sql = "SELECT s.*, d.*\nFROM %s AS s\nJOIN %s AS d ON d.%s = s.%s;" % (
                s_t, d_t, _col(self.dst_col), _col(self.src_col))
        return "\n".join(head + [sql])

    @staticmethod
    def _example(key, group):
        """A table's node; for a group's side, its first table."""
        if isinstance(key, tuple):
            return key
        return group.members[0][0] if group is not None and group.members else (DB, "?")


def _short(key):
    return node_label(key) if isinstance(key, tuple) else str(key)


def _col(name):
    return "rowid" if name == "rowid" else quote_ident(name)


def _alias_name(db):
    return "".join(ch if ch.isalnum() else "_" for ch in db) or "other"


def _qualified(key, attached):
    if attached and key[0] != DB:
        return "%s.%s" % (quote_ident(_alias_name(key[0])), quote_ident(key[1]))
    return quote_ident(key[1])


def _style_of(link):
    if link.cross:
        return "cross" if link.confident else "weaker"
    return link.kind if link.kind in ("declared", "verified", "weaker") else "verified"


def _overlap_of(link):
    """The value check of a link inside a database (engine.relations Overlap), or None."""
    rel = getattr(link, "relation", None)
    if rel is None:
        return None
    for l in getattr(rel, "links", ()) or ():
        ov = getattr(l, "overlap", None)
        if ov is not None and not getattr(ov, "error", None):
            return ov
    return None


class ErdModel(object):
    """The tables and relationships of the diagram. links: engine.linkgraph Link objects (the
    ones the tab shows); specs: {(db, table): TableSpec} (a table without one lists only its
    linked columns); keep: tables never folded into a group (the focused table); tables:
    extra tables to show without links; junctions: collapse junction tables into
    many-to-many relationships."""

    def __init__(self, links, specs=None, keep=(), tables=(), group_min=None, junctions=False):
        specs = specs or {}
        self.specs = specs
        self.links = list(links)
        self.junctions_collapsed = bool(junctions)
        self.tables = {}
        self.groups = {}
        self.rels = []
        self.junctions = {}         # junction table key -> [its relationships]
        self.hidden_junctions = []  # junction tables left out (collapsed)
        nodes = set(tables)
        for l in self.links:
            nodes.add(l.src)
            nodes.add(l.dst)
        rows = {}
        for l in self.links:
            if l.src_rows is not None:
                rows[l.src] = l.src_rows
            if l.dst_rows is not None:
                rows[l.dst] = l.dst_rows
        for n in sorted(nodes, key=_node_order):
            spec = specs.get(n)
            cols = []
            if spec is not None:
                for name, typ, pk in spec.columns:
                    cols.append(ErdColumn(name, typ, bool(pk), spec.unique.get(name.lower())))
            r = rows.get(n, spec.rows if spec is not None else None)
            self.tables[n] = ErdTable(n, cols, r, spec is not None)
        # the relationships, one per link, attached to exact columns
        rels = []
        for l in sorted(self.links, key=lambda l: (_node_order(l.src), col_name(l.src_col),
                                                   _node_order(l.dst), col_name(l.dst_col))):
            sc = self._column(l.src, col_name(l.src_col))
            dc = self._column(l.dst, col_name(l.dst_col))
            peer = getattr(l, "direction", "out") == "peer"
            sc.fk = sc.fk or not peer
            dc.target = dc.target or not peer
            if peer:
                sc.target = dc.target = True
            r = Relationship(0, l.src, sc.name, l.dst, dc.name, [l], _style_of(l), l.cross, peer)
            self._cardinality(r, sc, dc)
            rels.append(r)
        # junction tables
        incoming = {}
        outgoing = {}
        for r in rels:
            if r.peer or r.self_loop:
                continue
            outgoing.setdefault(r.src, []).append(r)
            incoming.setdefault(r.dst, []).append(r)
        for key, outs in sorted(outgoing.items(), key=lambda kv: _node_order(kv[0])):
            if key in incoming or key in keep or len(outs) < 2:
                continue
            t = self.tables[key]
            fk_cols = set(r.src_col.lower() for r in outs)
            if len(fk_cols) < 2:
                continue
            others = [c for c in t.columns if c.name.lower() not in fk_cols and not c.pk and
                      not c.pseudo and c.name.lower() not in ("id", "_id")]
            if len(others) > JUNCTION_OTHER:
                continue
            self.junctions[key] = outs
        if self.junctions_collapsed and self.junctions:
            drop = set()
            nm = []
            for key in sorted(self.junctions, key=_node_order):
                outs = sorted(self.junctions[key], key=lambda r: (r.src_col, _node_order(r.dst)))
                for i, a in enumerate(outs):
                    for b in outs[i + 1:]:
                        if a.src_col == b.src_col:
                            continue
                        r = Relationship(0, a.dst, a.dst_col, b.dst, b.dst_col,
                                         a.links + b.links, "nm", a.cross or b.cross)
                        r.junction = key
                        r.junction_cols = (a.src_col, b.src_col)
                        r.label = "via %s" % key[1]
                        r.cardinality = ("*", "*")
                        r.src_card = r.dst_card = "*"
                        r.basis = "junction"
                        r.evidence_src = "each row of %s pairs one %s with one %s" % (
                            key[1], _short(a.dst), _short(b.dst))
                        nm.append(r)
                drop.add(key)
            rels = [r for r in rels if r.src not in drop and r.dst not in drop] + nm
            for key in drop:
                del self.tables[key]
            self.hidden_junctions = sorted(drop, key=_node_order)
        # groups of tables linked alike to one anchor and to nothing else
        minimum = group_min if group_min is not None else limits.get("diagram_group_min")
        touching = {}
        for r in rels:
            touching.setdefault(r.src, []).append(r)
            if r.dst != r.src:
                touching.setdefault(r.dst, []).append(r)
        buckets = {}
        for key in sorted(self.tables, key=_node_order):
            if key in keep:
                continue
            rs = touching.get(key, ())
            if not rs or any(r.self_loop or r.junction is not None for r in rs):
                continue
            others = set(r.dst if r.src == key else r.src for r in rs)
            if len(others) != 1:
                continue
            anchor = next(iter(others))
            sig = tuple(sorted(set(
                ("src" if r.src == key else "dst", r.src_col if r.src == key else r.dst_col,
                 r.dst_col if r.src == key else r.src_col, r.style, r.cross, r.peer)
                for r in rs)))
            buckets.setdefault((anchor, sig), []).append(key)
        grouped = {}
        for (anchor, sig), members in sorted(buckets.items(),
                                             key=lambda kv: (_node_order(kv[0][0]),
                                                             repr(kv[0][1]))):
            if len(members) < max(2, minimum) or anchor in grouped:
                continue
            g = ErdGroup(anchor, sig, [(m, self.tables[m].rows) for m in
                                       sorted(members, key=lambda n: (node_label(n).lower(),
                                                                      n))])
            self.groups[g.key] = g
            for m in members:
                grouped[m] = g
        if grouped:
            out = []
            done = {}
            for r in rels:
                g = grouped.get(r.src) or grouped.get(r.dst)
                if g is None:
                    out.append(r)
                    continue
                member = r.src if r.src in grouped else r.dst
                line = ("src" if r.src == member else "dst",
                        r.src_col if r.src == member else r.dst_col,
                        r.dst_col if r.src == member else r.src_col, r.style, r.cross, r.peer)
                i = list(g.pattern).index(line)
                gr = done.get((g.key, i))
                if gr is None:
                    port = str(i)
                    if line[0] == "src":
                        gr = Relationship(0, g.key, port, g.anchor, line[2], [], r.style,
                                          r.cross, r.peer)
                    else:
                        gr = Relationship(0, g.anchor, line[2], g.key, port, [], r.style,
                                          r.cross, r.peer)
                    gr.group = g.key
                    gr.cardinality, gr.src_card, gr.dst_card = r.cardinality, r.src_card, \
                        r.dst_card
                    gr.basis = r.basis
                    gr.label = g.lines()[i]
                    done[(g.key, i)] = gr
                    out.append(gr)
                gr.links.append(r.link)
                gr.link = gr.links[0]
                # the group is as unique as its least unique member
                if r.src_card == "*":
                    gr.src_card = "*"
                if r.dst_card == "*":
                    gr.dst_card = "*"
                gr.cardinality = (gr.dst_card, gr.src_card)
            for gr in done.values():
                gr.label = "%d× %s" % (len(gr.links), gr.label)
                gr.evidence_src = "%d tables (the evidence of each: select it)" % len(gr.links)
            rels = out
            for m in grouped:
                del self.tables[m]
        for i, r in enumerate(rels):
            r.rid = i
        self.rels = rels

    def _column(self, node, name):
        t = self.tables[node]
        c = t.column(name)
        if c is None:
            if name == "rowid":
                spec = self.specs.get(node)
                c = ErdColumn("rowid", "INTEGER", False, "the rowid", pseudo=True)
                if spec is None or spec.rowid:
                    t.columns.insert(0, c)
                else:
                    t.columns.append(c)
            else:
                c = ErdColumn(name, "", False, None)
                t.columns.append(c)
        return c

    def _cardinality(self, r, sc, dc):
        dst_why = dc.unique if not dc.pseudo else "the rowid"
        if dst_why:
            r.dst_card, r.evidence_dst = "1", "unique: %s" % dst_why
        else:
            r.dst_card = "*"
            r.evidence_dst = "not unique by the schema (no PRIMARY KEY or UNIQUE index)" \
                if self.tables[r.dst].spec_known else "columns unknown"
        values = False
        if sc.unique or sc.pseudo:
            r.src_card, r.evidence_src = "1", "unique: %s" % (sc.unique or "the rowid")
        else:
            ov = _overlap_of(r.link)
            rows = r.link.src_rows
            if ov is not None and ov.exhausted and rows:
                values = True
                if ov.sampled >= rows:
                    r.src_card = "1"
                    r.evidence_src = "every one of its %s rows holds a different value" % (
                        format(rows, ","))
                else:
                    r.src_card = "*"
                    r.evidence_src = "%s distinct value%s in %s rows (values repeat or are " \
                                     "NULL)" % (format(ov.sampled, ","),
                                                "" if ov.sampled == 1 else "s",
                                                format(rows, ","))
            else:
                r.src_card = "*"
                r.evidence_src = "not unique by the schema; its values were not all counted"
        r.cardinality = (r.dst_card, r.src_card)
        r.basis = "schema and values" if values else "schema"

    # -- what the layout and the views ask ----------------------------------------------------
    def cards(self):
        """Every card key: the tables (not in a group), then the groups."""
        return sorted(self.tables, key=_node_order) + sorted(self.groups)

    def card(self, key):
        return self.groups.get(key) if key in self.groups else self.tables.get(key)

    def rel(self, rid):
        return self.rels[rid] if 0 <= rid < len(self.rels) else None

    def rels_of(self, key):
        return [r for r in self.rels if key in (r.src, r.dst)]

    def group_of(self, node):
        for g in self.groups.values():
            if any(m == node for m, _r in g.members):
                return g
        return None

    def table_count(self):
        """Tables drawn: cards and every table of a group."""
        return len(self.tables) + sum(len(g.members) for g in self.groups.values())

    def card_rows(self, key, mode="all", full=False, expanded=False):
        """The rows of a card: [Row]. mode 'linked' lists only the linked and key columns
        (and 'N other columns'); 'all' every column up to diagram_card_columns (the linked
        ones always), then 'N more columns' unless full. A group lists its link lines, then
        its tables when expanded."""
        if key in self.groups:
            g = self.groups[key]
            out = [Row("pattern", t, "", str(i)) for i, t in enumerate(g.lines())]
            if expanded:
                out += [Row("member", node_label(m), rows_text(n), None, node=m)
                        for m, n in g.members]
            else:
                out.append(Row("toggle", "List the %d tables ▸" % len(g.members), "", None))
            return out
        t = self.tables[key]
        cols = t.columns
        if mode == "linked":
            keep = [c for c in cols if c.linked or c.pk]
            out = [Row("col", c.name, c.type, c.name, col=c) for c in keep]
            if len(cols) > len(keep):
                n = len(cols) - len(keep)
                out.append(Row("more", "%d other column%s" % (n, "" if n == 1 else "s"), "",
                               None))
            return out
        cap = limits.get("diagram_card_columns")
        if full or len(cols) <= cap:
            chosen = cols
        else:
            must = set(id(c) for c in cols if c.linked or c.pk)
            budget = max(0, cap - len(must))
            chosen = []
            for c in cols:
                if id(c) in must:
                    chosen.append(c)
                elif budget:
                    chosen.append(c)
                    budget -= 1
        out = [Row("col", c.name, c.type, c.name, col=c) for c in chosen]
        if len(chosen) < len(cols):
            n = len(cols) - len(chosen)
            out.append(Row("more", "%d more column%s (limit diagram_card_columns)" % (
                n, "" if n == 1 else "s"), "", None))
        if not t.spec_known:
            out.append(Row("note", "columns known from the links only", "", None))
        return out

    def cut_cards(self):
        """Cards listing fewer columns than the table has (limit diagram_card_columns)."""
        cap = limits.get("diagram_card_columns")
        return [k for k, t in self.tables.items() if len(t.columns) > cap]


class Row(object):
    """A text row of a card: kind 'col' | 'more' | 'note' | 'pattern' | 'member' | 'toggle';
    port: what a connector attaches to (a column's name, a group's pattern line)."""
    __slots__ = ("kind", "text", "type", "port", "col", "node")

    def __init__(self, kind, text, type_, port, col=None, node=None):
        self.kind, self.text, self.type, self.port = kind, text, type_, port
        self.col, self.node = col, node


def _node_order(n):
    return (node_label(n).lower(), n) if isinstance(n, tuple) else (str(n).lower(), n)


# -- layout ----------------------------------------------------------------------------------
class Card(object):
    """A card placed: rows, size, position (x, y: top-left)."""
    __slots__ = ("key", "rows", "w", "h", "x", "y", "ports", "title", "sub")

    def __init__(self, key, rows, w, title, sub):
        self.key, self.rows, self.w = key, rows, w
        self.h = HEAD_H + ROW_H * len(rows) + BOTTOM
        self.x = self.y = 0.0
        self.title, self.sub = title, sub
        self.ports = {}
        for i, r in enumerate(rows):
            if r.port is not None:
                self.ports.setdefault(r.port, i)
                self.ports.setdefault(r.port.lower(), i)

    def port_off(self, port):
        """The y of a port's row centre from the card's top (the header's for none)."""
        i = self.ports.get(port)
        if i is None and port is not None:
            i = self.ports.get(port.lower())
        if i is None:
            return HEAD_H / 2.0
        return HEAD_H + ROW_H * i + ROW_H / 2.0

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class ErdLayout(object):
    """Cards placed and connectors routed. routes {rid: [(x, y)]} from the relationship's
    source to its target; labels {rid: (x, y, w, h)}; manual: True once positions came from
    the user (connectors then take the direct route)."""

    def __init__(self, model, cards, metrics):
        self.model = model
        self.cards = cards
        self.metrics = metrics
        self.routes = {}
        self.labels = {}
        self.manual = False
        self.stats = {}
        self.bounds = (0.0, 0.0, 10.0, 10.0)

    # positions ---------------------------------------------------------------------------
    def positions(self):
        return dict((k, (c.x, c.y)) for k, c in self.cards.items())

    def set_positions(self, positions):
        """Put cards where given (others stay), then route every connector directly."""
        for k, (x, y) in positions.items():
            c = self.cards.get(k)
            if c is not None:
                c.x, c.y = float(x), float(y)
        self.manual = True
        self.reroute()

    def move(self, key, x, y):
        """Move one card; returns the relationships re-routed."""
        c = self.cards[key]
        c.x, c.y = float(x), float(y)
        self.manual = True
        rids = [r.rid for r in self.model.rels if key in (r.src, r.dst)]
        self.reroute(rids)
        return rids

    def tidy(self):
        """Keep the cards where they are, but align cards whose left edges nearly agree and
        push overlapping cards down until nothing overlaps; route directly."""
        cards = sorted(self.cards.values(), key=lambda c: (c.x, c.y, _node_order(c.key)))
        # align left edges within 24 px of a column's first card
        cols = []
        for c in cards:
            for col in cols:
                if abs(c.x - col) <= 24:
                    c.x = col
                    break
            else:
                cols.append(c.x)
        for c in self.cards.values():
            c.y = round(c.y / 4.0) * 4.0
        placed = []
        for c in sorted(self.cards.values(), key=lambda c: (c.y, c.x, _node_order(c.key))):
            moved = True
            while moved:
                moved = False
                for p in placed:
                    if _overlap((c.x, c.y, c.w, c.h), (p.x, p.y, p.w, p.h), GAP_Y / 2.0):
                        c.y = p.y + p.h + GAP_Y
                        moved = True
            placed.append(c)
        self.manual = True
        self.reroute()

    # routing -----------------------------------------------------------------------------
    def reroute(self, rids=None):
        """Direct routes (after cards were moved): right angles from row to row, parallel
        connectors between the same cards in separate lanes."""
        model = self.model
        todo = model.rels if rids is None else [model.rels[i] for i in rids]
        pair_count, pair_index = {}, {}
        for r in model.rels:
            k = tuple(sorted((repr(r.src), repr(r.dst))))
            pair_index[r.rid] = pair_count.get(k, 0)
            pair_count[k] = pair_index[r.rid] + 1
        loops = {}
        for r in model.rels:
            if r.self_loop:
                loops.setdefault(r.src, []).append(r.rid)
        for r in todo:
            a, b = self.cards[r.src], self.cards[r.dst]
            ya, yb = a.y + a.port_off(r.src_col), b.y + b.port_off(r.dst_col)
            if r.self_loop:
                pts = _loop(a, ya, yb, loops[r.src].index(r.rid))
            else:
                k = tuple(sorted((repr(r.src), repr(r.dst))))
                n, i = pair_count[k], pair_index[r.rid]
                off = (i - (n - 1) / 2.0) * LANE
                if b.x >= a.x + a.w + 2 * LOOP_W:
                    xa, xb = a.x + a.w, b.x
                    mid = (xa + xb) / 2.0 + off
                elif a.x >= b.x + b.w + 2 * LOOP_W:
                    xa, xb = a.x, b.x + b.w
                    mid = (xa + xb) / 2.0 + off
                else:
                    xa, xb = a.x + a.w, b.x + b.w
                    mid = max(xa, xb) + 2 * LOOP_W + i * LANE
                pts = _simplify([(xa, ya), (mid, ya), (mid, yb), (xb, yb)])
            self.routes[r.rid] = pts
            self._direct_label(r)
        self._bounds()

    def _direct_label(self, r):
        self.labels.pop(r.rid, None)
        if not self._labels_on():
            return
        pts = self.routes[r.rid]
        w = self.metrics.width(r.label, "label") + 6
        if r.self_loop:
            x = max(p[0] for p in pts) + 4
            y = (pts[0][1] + pts[-1][1]) / 2.0 - LABEL_H / 2.0
            self.labels[r.rid] = (x, y, w, LABEL_H)
            return
        (x1, y1), (x2, _y2) = pts[0], pts[1]
        if abs(x2 - x1) >= w + 8:
            x = min(x1, x2) + 4 if x2 > x1 else x1 - 4 - w
            self.labels[r.rid] = (x, y1 - LABEL_H - 1, w, LABEL_H)

    def _labels_on(self):
        return len(self.model.rels) <= limits.get("diagram_edge_labels")

    def _bounds(self):
        xs, ys = [], []
        for c in self.cards.values():
            xs += [c.x, c.x + c.w]
            ys += [c.y, c.y + c.h]
        for pts in self.routes.values():
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
        for x, y, w, h in self.labels.values():
            xs += [x, x + w]
            ys += [y, y + h]
        if not xs:
            self.bounds = (0.0, 0.0, 10.0, 10.0)
        else:
            self.bounds = (min(xs) - MARGIN, min(ys) - MARGIN, max(xs) + MARGIN,
                           max(ys) + MARGIN)

    def overlaps(self):
        """Pairs of cards that overlap (none after arrange() or tidy())."""
        out = []
        cs = sorted(self.cards.values(), key=lambda c: c.x)
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                if b.x >= a.x + a.w:
                    break
                if _overlap(a.rect(), b.rect()):
                    out.append((a.key, b.key))
        return out

    def svg(self, colors=None, title=""):
        return render_svg(self, colors or {}, title)


def _overlap(a, b, gap=0.0):
    return (a[0] < b[0] + b[2] + gap and b[0] < a[0] + a[2] + gap and
            a[1] < b[1] + b[3] + gap and b[1] < a[1] + a[3] + gap)


def _loop(card, ya, yb, i):
    right = card.x + card.w
    x = right + LOOP_W + i * LANE
    if abs(ya - yb) < 1:
        yb = ya + 6
    return [(right, ya), (x, ya), (x, yb), (right, yb)]


def _simplify(pts):
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 0.01 and abs(out[-1][1] - p[1]) < 0.01:
            continue
        if len(out) >= 2:
            (x0, y0), (x1, y1) = out[-2], out[-1]
            if (abs(x0 - x1) < 0.01 and abs(x1 - p[0]) < 0.01) or \
                    (abs(y0 - y1) < 0.01 and abs(y1 - p[1]) < 0.01):
                out[-1] = p
                continue
        out.append(p)
    return out


def card_width(model, key, rows, metrics):
    m = metrics
    item = model.card(key)
    if key in model.groups:
        title = item.label + "  ▸"
        w = PAD + m.width(title, "name") + TOGGLE_W + PAD
        for r in rows:
            if r.kind == "member":
                w = max(w, PAD + m.width(r.text, "member") + TYPE_GAP + m.width(r.type, "rows") +
                        PAD)
            else:
                w = max(w, PAD + m.width(r.text, "col") + PAD)
    else:
        w = EDGE_W + PAD + m.width(item.label, "name") + 12 + m.width(rows_text(item.rows),
                                                                        "rows") + \
            TOGGLE_W + PAD
        for r in rows:
            if r.kind == "col":
                w = max(w, MARK_W + m.width(r.text, "pk" if r.col.pk else "col") + TYPE_GAP +
                        m.width(r.type, "type") + PAD)
            else:
                w = max(w, MARK_W + m.width(r.text, "note") + PAD)
    return max(CARD_W_MIN, min(CARD_W_MAX, math.ceil(w)))


def make_cards(model, metrics=None, linked_only=False, modes=None, full=(), expanded=()):
    """{key: Card} sized to their rows. modes: {key: 'linked' | 'all'} per card (over
    linked_only); full: cards listing every column; expanded: groups listing their tables."""
    metrics = metrics or TextMetrics()
    modes = modes or {}
    full, expanded = set(full), set(expanded)
    out = {}
    for key in model.cards():
        mode = modes.get(key, "linked" if linked_only else "all")
        rows = model.card_rows(key, mode, key in full, key in expanded)
        item = model.card(key)
        title = item.label + ("  ▾" if key in expanded else "  ▸") if key in model.groups \
            else item.label
        sub = "" if key in model.groups else rows_text(item.rows)
        out[key] = Card(key, rows, card_width(model, key, rows, metrics), title, sub)
    return out


def arrange(model, metrics=None, linked_only=False, modes=None, full=(), expanded=(),
            positions=None):
    """Lay the model out (see the module's doc). positions: {key: (x, y)} kept for the cards
    they name (the others are placed by the layout, then overlaps are removed). Returns the
    ErdLayout."""
    t0 = time.perf_counter()
    metrics = metrics or TextMetrics()
    cards = make_cards(model, metrics, linked_only, modes, full, expanded)
    lay = ErdLayout(model, cards, metrics)
    _layered(lay)
    lay.stats["seconds"] = time.perf_counter() - t0
    if positions:
        known = dict((k, v) for k, v in positions.items() if k in cards)
        if known:
            for k, (x, y) in known.items():
                cards[k].x, cards[k].y = float(x), float(y)
            if len(known) < len(cards) or lay.overlaps():
                lay.tidy()
            else:
                lay.manual = True
                lay.reroute()
    return lay


def _layered(lay):
    model, cards = lay.model, lay.cards
    order = dict((k, i) for i, k in enumerate(model.cards()))
    edges = []                          # (a, b, rid) of connectors between two cards
    for r in model.rels:
        if not r.self_loop:
            edges.append((r.src, r.dst, r.rid))
    comps = _components(list(order), edges, order)
    placed = []
    total_cross = total_naive = 0
    labels_on = lay._labels_on()
    for nodes in comps:
        cedges = [e for e in edges if e[0] in nodes]
        res = _layout_component(lay, nodes, cedges, order, labels_on)
        total_cross += res[0]
        total_naive += res[1]
        placed.append((nodes, _extent(lay, nodes, cedges)))
    _pack(lay, placed, edges)
    for r in model.rels:
        if r.self_loop:
            a = cards[r.src]
            loops = [x.rid for x in model.rels if x.self_loop and x.src == r.src]
            lay.routes[r.rid] = _loop(a, a.y + a.port_off(r.src_col),
                                      a.y + a.port_off(r.dst_col), loops.index(r.rid))
            lay._direct_label(r)
    lay.stats.update(crossings=total_cross, initial_crossings=total_naive,
                     components=len(comps))
    lay._bounds()


def _components(nodes, edges, order):
    parent = dict((n, n) for n in nodes)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b, _r in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            if order[ra] < order[rb]:
                parent[rb] = ra
            else:
                parent[ra] = rb
    comps = {}
    for n in nodes:
        comps.setdefault(find(n), []).append(n)
    out = [sorted(c, key=lambda n: order[n]) for c in comps.values()]
    out.sort(key=lambda c: (-len(c), order[c[0]]))
    return out


def _layout_component(lay, nodes, edges, order, labels_on):
    """Place one component's cards (top-left at 0, 0) and route its connectors. Returns
    (crossings, crossings of the initial order)."""
    cards = lay.cards
    nodeset = set(nodes)
    if len(nodes) == 1:
        c = cards[nodes[0]]
        c.x = c.y = 0.0
        return 0, 0
    # 1. break cycles: depth-first, sources first; an edge back to the path is reversed
    out = dict((n, []) for n in nodes)
    indeg = dict((n, 0) for n in nodes)
    for i, (a, b, _r) in enumerate(edges):
        out[a].append(i)
        indeg[b] += 1
    for n in nodes:
        out[n].sort(key=lambda i: (order[edges[i][1]], i))
    reverse = set()
    state = {}
    starts = sorted(nodes, key=lambda n: (indeg[n] > 0, order[n]))
    for s in starts:
        if s in state:
            continue
        state[s] = 1
        stack = [(s, iter(out[s]))]
        while stack:
            n, it = stack[-1]
            for i in it:
                v = edges[i][1]
                st = state.get(v)
                if st == 1:
                    reverse.add(i)
                elif st is None:
                    state[v] = 1
                    stack.append((v, iter(out[v])))
                    break
            else:
                state[n] = 2
                stack.pop()
    dag = []                            # (u, v, rid, u_is_src)
    for i, (a, b, rid) in enumerate(edges):
        if a == b:
            continue
        dag.append((b, a, rid, False) if i in reverse else (a, b, rid, True))
    # 2. layers: longest path from the sources, sources pulled next to their successors
    succ = dict((n, []) for n in nodes)
    pred = dict((n, []) for n in nodes)
    for u, v, _rid, _s in dag:
        succ[u].append(v)
        pred[v].append(u)
    indeg = dict((n, len(pred[n])) for n in nodes)
    import heapq
    heap = [(order[n], n) for n in nodes if not indeg[n]]
    heapq.heapify(heap)
    topo = []
    while heap:
        _o, n = heapq.heappop(heap)
        topo.append(n)
        for v in succ[n]:
            indeg[v] -= 1
            if not indeg[v]:
                heapq.heappush(heap, (order[v], v))
    layer = dict((n, 0) for n in nodes)
    for n in topo:
        for v in succ[n]:
            layer[v] = max(layer[v], layer[n] + 1)
    for n in reversed(topo):
        if not pred[n] and succ[n]:
            layer[n] = min(layer[v] for v in succ[n]) - 1
    low = min(layer.values())
    for n in nodes:
        layer[n] -= low
    # 3. a layer much taller than the diagram is wide is split (sources leftmost)
    nlayers = max(layer.values()) + 1
    members = [[] for _i in range(nlayers)]
    for n in nodes:
        members[layer[n]].append(n)
    area = sum(cards[n].w * cards[n].h for n in nodes)
    limit = max(SPLIT_MIN, 1.6 * math.sqrt(area))
    phys = []                           # physical layers: [[nodes]]
    for ms in members:
        ms.sort(key=lambda n: (bool(pred[n]), order[n]))
        total = sum(cards[n].h + GAP_Y for n in ms)
        k = int(math.ceil(total / limit)) if total > limit else 1
        if k <= 1:
            phys.append(ms)
            continue
        target = total / float(k)
        chunk, acc = [], 0.0
        for n in ms:
            if chunk and acc + cards[n].h / 2.0 > target:
                phys.append(chunk)
                chunk, acc = [], 0.0
            chunk.append(n)
            acc += cards[n].h + GAP_Y
        if chunk:
            phys.append(chunk)
    for i, ms in enumerate(phys):
        for n in ms:
            layer[n] = i
    L = len(phys)
    # 4. chains: every connector as a chain of slots, one per layer it crosses
    size = {}
    off_of = {}                         # (chain index, position in chain) -> port offset
    chains = []
    for u, v, rid, fwd in sorted(dag, key=lambda d: d[2]):
        r = lay.model.rels[rid]
        pu = cards[u].port_off(r.src_col if fwd else r.dst_col)
        pv = cards[v].port_off(r.dst_col if fwd else r.src_col)
        chain = [u]
        for j in range(layer[u] + 1, layer[v]):
            d = ("~", rid, j)
            size[d] = (0.0, float(DUMMY_H))
            phys[j].append(d)
            layer[d] = j
            order[d] = order[u] + 0.5
            chain.append(d)
        chain.append(v)
        chains.append((rid, fwd, chain, pu, pv))
    for n in nodes:
        size[n] = (float(cards[n].w), float(cards[n].h))
    # the segments between adjacent layers: (left node, right node, left offset, right offset)
    seg_left = {}                       # node -> [(other, own off, other off)] to the left
    seg_right = {}
    segs = []                           # (layer of left, left, right, offl, offr, rid)
    for rid, fwd, chain, pu, pv in chains:
        for j in range(len(chain) - 1):
            a, b = chain[j], chain[j + 1]
            oa = pu if j == 0 else DUMMY_H / 2.0
            ob = pv if j + 1 == len(chain) - 1 else DUMMY_H / 2.0
            segs.append((layer[a], a, b, oa, ob, rid))
            seg_right.setdefault(a, []).append((b, oa, ob))
            seg_left.setdefault(b, []).append((a, ob, oa))
    # 5. order within the layers: barycenter sweeps, the order with fewest crossings kept
    for ms in phys:
        ms.sort(key=lambda n: order[n])
    pos = {}

    def index():
        for ms in phys:
            for i, n in enumerate(ms):
                pos[n] = i

    def frac(n, off):
        return off / max(1.0, size[n][1])

    def crossings():
        total = 0
        by_layer = {}
        for li, a, b, oa, ob, _rid in segs:
            by_layer.setdefault(li, []).append((pos[a] + frac(a, oa), pos[b] + frac(b, ob)))
        for pairs in by_layer.values():
            pairs.sort()
            total += _inversions([q for _p, q in pairs])
        return total
    index()
    naive = best = crossings()
    best_order = [list(ms) for ms in phys]
    sweeps = SWEEPS if len(pos) < 400 else 6
    for it in range(sweeps):
        if best == 0:
            break
        down = it % 2 == 0
        rng = range(1, L) if down else range(L - 2, -1, -1)
        for li in rng:
            ms = phys[li]
            nb = seg_left if down else seg_right
            keyed = []
            for n in ms:
                ns = nb.get(n)
                if ns:
                    bc = sum(pos[m] + frac(m, om) for m, _own, om in ns) / len(ns)
                else:
                    bc = pos[n]
                keyed.append((bc, pos[n], n))
            keyed.sort(key=lambda t: (t[0], t[1]))
            phys[li] = [n for _b, _p, n in keyed]
            for i, n in enumerate(phys[li]):
                pos[n] = i
        c = crossings()
        if c < best:
            best, best_order = c, [list(ms) for ms in phys]
    phys = best_order
    index()
    # then adjacent cards swapped wherever that removes crossings (transposition)
    if best:
        phys = [list(ms) for ms in best_order]
        index()
        _transpose(phys, pos, seg_left, seg_right, frac)
        c = crossings()
        if c < best:
            best = c
        else:
            phys = best_order
        index()
    # 6. vertical positions: stacked, then pulled towards the linked rows (isotonic per layer)
    y = {}
    for ms in phys:
        acc = 0.0
        for n in ms:
            y[n] = acc
            acc += size[n][1] + _gap(n)
    for it in range(8):
        rng = range(L) if it % 2 == 0 else range(L - 1, -1, -1)
        for li in rng:
            ms = phys[li]
            if not ms:
                continue
            want, weight = [], []
            for n in ms:
                ns = [(m, own, om) for m, own, om in seg_left.get(n, ())] + \
                    [(m, own, om) for m, own, om in seg_right.get(n, ())]
                if ns:
                    want.append(sum(y[m] + om - own for m, own, om in ns) / len(ns))
                    weight.append(len(ns) * (0.3 if isinstance(n, tuple) and n and
                                             n[0] == "~" else 1.0))
                else:
                    want.append(y[n])
                    weight.append(0.05)
            gaps = [0.0] + [size[ms[i - 1]][1] + _gap(ms[i - 1], ms[i]) for i in range(1,
                                                                                    len(ms))]
            for n, v in zip(ms, _isotonic(want, weight, gaps)):
                y[n] = v
    top = min(y.values())
    for n in y:
        y[n] = float(round(y[n] - top))
    # 7. horizontal positions: layer widths and gutters sized to their lanes and labels
    width = [max([size[n][0] for n in ms] or [0.0]) for ms in phys]
    lanes = [dict() for _i in range(L)]     # gutter right of layer i: (rid, j) -> lane
    nlanes = [0] * L
    by_gutter = {}
    for rid, fwd, chain, pu, pv in chains:
        for j in range(len(chain) - 1):
            a, b = chain[j], chain[j + 1]
            ya = y[a] + (pu if j == 0 else DUMMY_H / 2.0)
            yb = y[b] + (pv if j + 1 == len(chain) - 1 else DUMMY_H / 2.0)
            by_gutter.setdefault(layer[a], []).append((min(ya, yb), max(ya, yb), rid, j))
    for g, ivs in by_gutter.items():
        ivs.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
        ends = []
        for lo, hi, rid, j in ivs:
            if hi - lo < 0.5:
                lanes[g][(rid, j)] = None
                continue
            for li, e in enumerate(ends):
                if e + LANE < lo:
                    ends[li] = hi
                    lanes[g][(rid, j)] = li
                    break
            else:
                ends.append(hi)
                lanes[g][(rid, j)] = len(ends) - 1
        nlanes[g] = len(ends)
    label_w = [0.0] * L
    if labels_on:
        for rid, fwd, chain, pu, pv in chains:
            r = lay.model.rels[rid]
            u = chain[0]
            label_w[layer[u]] = max(label_w[layer[u]],
                                    lay.metrics.width(r.label, "label") + 14)
    loops = {}
    for r in lay.model.rels:
        if r.self_loop and r.src in nodeset:
            loops[r.src] = loops.get(r.src, 0) + 1
    for n, k in loops.items():
        extra = LOOP_W + k * LANE + 6
        if labels_on:
            extra += max(lay.metrics.width(r.label, "label") + 8 for r in lay.model.rels
                         if r.self_loop and r.src == n)
        label_w[layer[n]] = max(label_w[layer[n]], extra)
    gutter = [max(GUTTER_MIN, label_w[i] + 2 * GUTTER_PAD + nlanes[i] * LANE)
              for i in range(L)]
    xs, acc = [], 0.0
    for i in range(L):
        xs.append(acc)
        acc += width[i] + gutter[i]
    for n in nodes:
        c = cards[n]
        c.x, c.y = xs[layer[n]], y[n]
    # 8. routes
    for rid, fwd, chain, pu, pv in chains:
        u, v = chain[0], chain[-1]
        pts = [(cards[u].x + cards[u].w, y[u] + pu)]
        for j in range(len(chain) - 1):
            a, b = chain[j], chain[j + 1]
            g = layer[a]
            ln = lanes[g].get((rid, j))
            yb = y[b] + (pv if j + 1 == len(chain) - 1 else DUMMY_H / 2.0)
            gx = xs[g] + width[g] + label_w[g] + GUTTER_PAD + (ln or 0) * LANE
            pts.append((gx, pts[-1][1]))
            pts.append((gx, yb))
        pts.append((cards[v].x, pts[-1][1]))
        pts = _simplify(pts)
        if not fwd:
            pts.reverse()
        lay.routes[rid] = pts
    # labels on the connector's run out of its left card (a second one below the line)
    if labels_on:
        used = set()
        for rid, fwd, chain, pu, pv in chains:
            r = lay.model.rels[rid]
            u = chain[0]
            k = (u, round(pu))
            if k in used:
                continue            # one label per row: the others show on hover
            used.add(k)
            w = lay.metrics.width(r.label, "label") + 6
            lay.labels[rid] = (cards[u].x + cards[u].w + 4, y[u] + pu - LABEL_H - 1, w,
                               LABEL_H)
    return best, naive


TRANSPOSE_ROUNDS = 12       # passes of adjacent swaps over every layer (fewer when none helps)


def _pair_crossings(a, b):
    """Crossings between the segments of two neighbouring cards on one side, with the card
    of `a` above that of `b`: pairs whose other ends are the other way round."""
    if not a or not b:
        return 0, 0
    bs = sorted(b)
    n = 0
    lower = 0
    for x in a:
        n += len(bs) - bisect.bisect_right(bs, x)       # b's end below a's: no crossing
        lower += bisect.bisect_left(bs, x)              # b's end above a's: crossing
    return lower, n


def _transpose(phys, pos, seg_left, seg_right, frac):
    """Swap neighbouring cards of a layer wherever that removes crossings with the layers
    on either side (the classic transposition step after barycenter ordering), until a pass
    changes nothing. pos is kept current."""
    def ends(n, side):
        return [pos[m] + frac(m, om) for m, _own, om in side.get(n, ())]
    for _round in range(TRANSPOSE_ROUNDS):
        improved = False
        for ms in phys:
            if len(ms) < 2:
                continue
            for i in range(len(ms) - 1):
                u, v = ms[i], ms[i + 1]
                now = after = 0
                for side in (seg_left, seg_right):
                    cross, keep = _pair_crossings(ends(u, side), ends(v, side))
                    now += cross
                    after += keep
                if after < now:
                    ms[i], ms[i + 1] = v, u
                    pos[u], pos[v] = i + 1, i
                    improved = True
        if not improved:
            break


def _gap(a, b=None):
    da = isinstance(a, tuple) and len(a) == 3 and a[0] == "~"
    db = b is not None and isinstance(b, tuple) and len(b) == 3 and b[0] == "~"
    if da and (b is None or db):
        return DUMMY_GAP
    if da or db:
        return GAP_Y / 2.0
    return GAP_Y


def _isotonic(want, weight, gaps):
    """Positions as close as possible to want (weighted least squares) keeping the order and
    at least gaps[i] between item i-1 and i (pool adjacent violators)."""
    off, acc = [], 0.0
    for g in gaps:
        acc += g
        off.append(acc)
    t = [w - o for w, o in zip(want, off)]
    blocks = []                         # [weighted sum, weight, count]
    for v, w in zip(t, weight):
        blocks.append([v * w, w, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, ww, n = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += ww
            blocks[-1][2] += n
    out = []
    for s, w, n in blocks:
        out.extend([s / w] * n)
    return [z + o for z, o in zip(out, off)]


def _inversions(seq):
    """Pairs i < j with seq[i] > seq[j] (merge sort)."""
    if len(seq) < 2:
        return 0
    arr = list(seq)
    tmp = [0] * len(arr)
    count = 0
    width = 1
    n = len(arr)
    while width < n:
        for lo in range(0, n, 2 * width):
            mid, hi = min(lo + width, n), min(lo + 2 * width, n)
            i, j, k = lo, mid, lo
            while i < mid and j < hi:
                if arr[i] <= arr[j]:
                    tmp[k] = arr[i]
                    i += 1
                else:
                    tmp[k] = arr[j]
                    count += mid - i
                    j += 1
                k += 1
            while i < mid:
                tmp[k] = arr[i]
                i += 1
                k += 1
            while j < hi:
                tmp[k] = arr[j]
                j += 1
                k += 1
        arr, tmp = tmp, arr
        width *= 2
    return count


def count_crossings(layers, edges):
    """Crossings of straight edges between adjacent layers: layers [[node]], edges [(a, b)]
    with a in layer i and b in layer i + 1 (for tests and comparisons)."""
    pos, lay = {}, {}
    for i, ms in enumerate(layers):
        for j, n in enumerate(ms):
            pos[n], lay[n] = j, i
    by = {}
    for a, b in edges:
        if lay[a] > lay[b]:
            a, b = b, a
        by.setdefault(lay[a], []).append((pos[a], pos[b]))
    total = 0
    for pairs in by.values():
        pairs.sort()
        total += _inversions([q for _p, q in pairs])
    return total


def _extent(lay, nodes, edges):
    xs, ys = [], []
    for n in nodes:
        c = lay.cards[n]
        xs += [c.x, c.x + c.w]
        ys += [c.y, c.y + c.h]
    for _a, _b, rid in edges:
        for x, y in lay.routes.get(rid, ()):
            xs.append(x)
            ys.append(y)
    for n in nodes:
        for r in lay.model.rels:
            if r.self_loop and r.src == n:
                xs.append(lay.cards[n].x + lay.cards[n].w + LOOP_W + 6 * LANE +
                          (lay.metrics.width(r.label, "label") + 10 if lay._labels_on() else 0))
    for _a, _b, rid in edges:
        lb = lay.labels.get(rid)
        if lb:
            xs += [lb[0], lb[0] + lb[2]]
            ys += [lb[1], lb[1] + lb[3]]
    return (min(xs), min(ys), max(xs), max(ys))


def _pack(lay, placed, edges):
    """Put the components side by side in rows (largest first), each moved by its extent."""
    if not placed:
        return
    area = sum((e[2] - e[0]) * (e[3] - e[1]) for _n, e in placed)
    row_w = max(max(e[2] - e[0] for _n, e in placed), math.sqrt(area) * 1.6)
    x = y = shelf = 0.0
    rid_of = {}
    for a, b, rid in edges:
        rid_of.setdefault(a, []).append(rid)
    for nodes, (x0, y0, x1, y1) in placed:
        w, h = x1 - x0, y1 - y0
        if x > 0 and x + w > row_w:
            x, y, shelf = 0.0, y + shelf + COMP_GAP, 0.0
        dx, dy = x - x0, y - y0
        for n in nodes:
            c = lay.cards[n]
            c.x += dx
            c.y += dy
            for rid in rid_of.get(n, ()):
                lay.routes[rid] = [(px + dx, py + dy) for px, py in lay.routes[rid]]
                lb = lay.labels.get(rid)
                if lb is not None:
                    lay.labels[rid] = (lb[0] + dx, lb[1] + dy, lb[2], lb[3])
        x += w + COMP_GAP
        shelf = max(shelf, h)


# -- cardinality markers (shared by the canvas and the SVG) ------------------------------------
def end_marker(pts, at_start, card):
    """The polyline of a cardinality mark at one end of a route: crow's foot for '*', a bar
    for '1'. Returns [(x, y), ...]. card: '1' or '*'."""
    if len(pts) < 2:
        return []
    (ex, ey), (px, py) = (pts[0], pts[1]) if at_start else (pts[-1], pts[-2])
    d = 1.0 if px > ex else -1.0        # towards the line, away from the card
    if abs(px - ex) < 0.01:
        d = 1.0
    if card == "*":
        fx = ex + d * 11
        return [(ex, ey - 6), (fx, ey), (ex, ey + 6), (fx, ey), (ex, ey)]
    bx = ex + d * 7
    return [(bx, ey - 6), (bx, ey + 6)]


# -- SVG -------------------------------------------------------------------------------------
def render_svg(lay, colors, title=""):
    """The layout as a standalone SVG document (deterministic, text escaped)."""
    m = TextMetrics()
    model = lay.model
    x0, y0, x1, y1 = lay.bounds
    legend_h = 30
    y0 -= legend_h
    width, height = x1 - x0, y1 - y0
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="%.1f %.1f %.1f %.1f" font-family="Segoe UI, Helvetica, Arial, sans-serif" '
           'font-size="11">' % (math.ceil(width), math.ceil(height), x0, y0, width, height)]
    if title:
        out.append("<title>%s</title>" % escape(title))
    out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#f8fafc"/>'
               % (x0, y0, width, height))
    lx, ly = x0 + MARGIN, y0 + 18
    for text, style in (("declared foreign key", "declared"), ("verified by values",
                                                               "verified"),
                        ("between databases (matched by value)", "cross"),
                        ("many-to-many (junction)", "nm")):
        if style == "cross" and not any(r.cross for r in model.rels):
            continue
        if style == "nm" and not any(r.junction is not None for r in model.rels):
            continue
        color, dash = STYLES[style]
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                   'stroke-width="1.6"%s/>' % (lx, ly - 4, lx + 26, ly - 4, color,
                                               _dash(dash)))
        out.append('<text x="%.1f" y="%.1f" fill="#475569">%s</text>' % (lx + 30, ly,
                                                                          escape(text)))
        lx += 30 + m.width(text, "col") + 18
    out.append('<text x="%.1f" y="%.1f" fill="#475569">%s</text>'
               % (lx, ly, escape("crow's foot: many; bar: one")))
    for r in model.rels:
        pts = lay.routes.get(r.rid)
        if not pts:
            continue
        color, dash = STYLES[r.style]
        out.append('<polyline class="link" points="%s" fill="none" stroke="%s" '
                   'stroke-width="1.5"%s><title>%s</title></polyline>'
                   % (" ".join("%.1f,%.1f" % p for p in pts), color, _dash(dash),
                      escape("%s\n%s, %s" % (r.title(), r.kind_text(), r.card_text()))))
        for at_start, card in ((True, r.src_card), (False, r.dst_card)):
            mk = end_marker(pts, at_start, card)
            if mk:
                out.append('<polyline class="card" points="%s" fill="none" stroke="%s" '
                           'stroke-width="1.5"/>' % (" ".join("%.1f,%.1f" % p for p in mk),
                                                     color))
    for r in model.rels:
        lb = lay.labels.get(r.rid)
        if lb:
            x, y, w, h = lb
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#f8fafc" '
                       'fill-opacity="0.9"/>' % (x, y, w, h))
            out.append('<text x="%.1f" y="%.1f" fill="#475569" font-size="10">%s</text>'
                       % (x + 3, y + h - 3, escape(r.label)))
    for key in model.cards():
        c = lay.cards[key]
        item = model.card(key)
        out.append('<g class="card">')
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="4" fill="#ffffff" '
                   'stroke="#cbd5e1"/>' % (c.x, c.y, c.w, c.h))
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="4" fill="#e9eef6" '
                   'stroke="#cbd5e1"/>' % (c.x, c.y, c.w, HEAD_H))
        edge = valid_color(colors.get(key[0]), None) if isinstance(key, tuple) else None
        if edge:
            out.append('<rect x="%.1f" y="%.1f" width="%d" height="%.1f" fill="%s"/>'
                       % (c.x, c.y, EDGE_W, c.h, escape(edge)))
        out.append('<text x="%.1f" y="%.1f" font-weight="600" font-size="12" fill="#1e3a8a">'
                   '%s</text>' % (c.x + PAD + EDGE_W, c.y + HEAD_H / 2.0 + 4,
                                  escape(c.title)))
        if c.sub:
            out.append('<text x="%.1f" y="%.1f" font-size="10" fill="#475569" '
                       'text-anchor="end">%s</text>' % (c.x + c.w - PAD, c.y + HEAD_H / 2.0 + 4,
                                                        escape(c.sub)))
        for i, row in enumerate(c.rows):
            ry = c.y + HEAD_H + ROW_H * i
            cy = ry + ROW_H / 2.0 + 4
            if row.kind == "col":
                col = row.col
                if col.pk:
                    out.append('<text x="%.1f" y="%.1f" font-size="8" font-weight="700" '
                               'fill="#b45309">PK</text>' % (c.x + PAD, cy - 1))
                if col.fk:
                    out.append('<text x="%.1f" y="%.1f" font-size="8" font-weight="700" '
                               'fill="#1e40af">FK</text>' % (c.x + PAD + 18, cy - 1))
                out.append('<text x="%.1f" y="%.1f"%s fill="#0f172a">%s</text>'
                           % (c.x + MARK_W, cy, ' font-weight="600"' if col.pk else "",
                              escape(row.text)))
                if row.type:
                    out.append('<text x="%.1f" y="%.1f" font-size="10" fill="#475569" '
                               'text-anchor="end">%s</text>' % (c.x + c.w - PAD, cy,
                                                                escape(row.type)))
            elif row.kind == "member":
                out.append('<text x="%.1f" y="%.1f" fill="#0f172a">%s</text>'
                           % (c.x + PAD, cy, escape(row.text)))
                out.append('<text x="%.1f" y="%.1f" font-size="10" fill="#475569" '
                           'text-anchor="end">%s</text>' % (c.x + c.w - PAD, cy,
                                                            escape(row.type)))
            else:
                out.append('<text x="%.1f" y="%.1f" fill="#475569"%s>%s</text>'
                           % (c.x + (PAD if key in model.groups else MARK_W), cy,
                              ' font-style="italic"' if row.kind in ("more", "note", "toggle")
                              else "", escape(row.text)))
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _dash(dash):
    return ' stroke-dasharray="%s"' % ",".join(str(x) for x in dash) if dash else ""
