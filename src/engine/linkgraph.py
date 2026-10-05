"""The relationship map as a picture (no Tk): the links, the tables they join and a
deterministic, non-overlapping layout, drawn as hand-written SVG.

relations_tab.RelationsTab draws it on a canvas and exports it; engine.datamap puts the same
SVG into the Database Map. Tables are identified as (database, table): DB ('main') when one
database is open; in a case of several, the database's name. The links between databases
(engine.crossdb, always 'matched by value') are ValueLinks, drawn in their own style (orange,
short dashes); in a case each table's box can be outlined in its database's colour.

The layout (LinkGraph.layout) puts every box in a cell of a grid: columns of boxes with gaps
between them ('gutters') and rows with gaps between them ('channels'). Boxes are sized to
their text (TextMetrics: measured with the canvas font by the tab, estimated for SVG), so no
two boxes can overlap. Lines run only through the gaps, at right angles, from the row of a
column in one box to the row of a column in the other; lines to the same column share a lane,
so many links into one key form one trunk instead of a starburst.

- Focused on a table (the 'hub'): the hub in the middle, the tables that refer to it in
  columns on the left, the tables it refers to on the right, the tables sharing a value
  ('peer' links, same-named columns) in rows below.
- The whole database: only tables with links, one block per group of connected tables (the
  largest first), the most linked table of each block in its middle.
- Groups: when at least diagram_group_min tables (engine.limits) are linked to the same table
  in exactly the same way (142 tables with message_row_id -> message._id), they are drawn as
  one group box that names the count and the link; expanded, it lists the tables in a grid
  (the SVG always lists them, so nothing is lost).
"""

import html
import math

from . import limits
from .relations import ROWID, is_confident, plain_reason
from .tags import valid_color


def escape(text):
    """Text for SVG content or a quoted attribute value: & < > " ' all escaped."""
    return html.escape(str(text), quote=True)

DB = "main"                 # the database dimension of a table's identity
PAD = 8                     # text inset in a box
HEAD_H, LINE_H = 22, 16     # (estimated) title and column line heights of a table box
BOX_W_MIN = 96
LANE = 4                    # spacing of lines side by side in a gap
GAP_X, GAP_Y = 48, 26       # least gap between two columns / two rows of boxes
MEMBER_GAP = 6              # between the table boxes listed in an expanded group
COMP_GAP = 80               # between two blocks of connected tables
MARGIN = 30                 # around the whole layout

CROSS_COLOR = "#c25100"     # links between databases (matched by value)
# (colour key, dash) of a line: solid declared keys, dashed checked links, dotted weaker ones;
# links between databases in orange with short dashes
LINE_STYLES = {"declared": ("accent", ()), "verified": ("accent", (6, 4)),
               "weaker": ("text2", (2, 3)), "value": ("orange", (3, 2))}
CROSS_STYLE = ("orange", (3, 2))

# text kinds of the layout: a table's name, a column, a key column, a group's title, a table
# listed in a group, a line's label
KINDS = ("name", "col", "key", "group", "member", "label")


def col_name(c):
    return "rowid" if c is ROWID else c


def node_label(node):
    """A table's name in the diagram: 'table', or in a case 'wa.db › table'."""
    return node[1] if node[0] == DB else "%s › %s" % (node[0], node[1])


def _n(v):
    return "?" if v is None else format(v, ",")


EMPTY = "unverified — table is empty"


class Link(object):
    """One link of the map: src (db, table), src column -> dst (db, table), dst column.

    A table without rows cannot confirm a link by its values: a link found by names or values
    that touches one is never confident (kind 'weaker', reason 'unverified — table is empty');
    a declared foreign key stays, marked 'declared, no rows'."""
    __slots__ = ("src", "src_col", "dst", "dst_col", "kind", "confident", "score", "reason",
                 "overlap", "src_rows", "dst_rows", "relation", "direction", "cross", "sampled")

    def __init__(self, rel, db=DB, rows=None):
        self.relation = rel
        self.direction = rel.direction
        self.cross = False
        self.src, self.src_col = (db, rel.table), rel.column
        self.dst, self.dst_col = (db, rel.other), rel.other_column
        self.confident = is_confident(rel)
        self.kind = "declared" if rel.kind == "fk" else ("verified" if self.confident else
                                                         "weaker")
        self.score = rel.score
        self.reason = plain_reason(rel) or "; ".join(rel.why())
        ovs = [l.overlap for l in rel.links if l.overlap is not None and l.overlap.sampled]
        self.overlap = min(ov.fraction for ov in ovs) if ovs else None
        self.sampled = min(ov.sampled for ov in ovs) if ovs else None
        rows = rows or (lambda t: None)
        self.src_rows, self.dst_rows = rows(rel.table), rows(rel.other)
        self._empty_rule()

    def _empty_rule(self):
        if self.empty() and self.kind != "declared":
            self.confident = False
            self.kind = "weaker"
            if not self.reason.startswith(EMPTY):
                self.reason = EMPTY + ("; " + self.reason if self.reason else "")

    def empty(self):
        """True when a table of the link is known to hold no rows."""
        return self.src_rows == 0 or self.dst_rows == 0

    def kind_text(self):
        if self.kind == "declared" and self.empty():
            return "declared, no rows"
        return {"declared": "declared foreign key", "verified": "verified by values",
                "weaker": "weaker", "value": "matched by value"}[self.kind] + \
            (" (weaker)" if self.cross and not self.confident else "")

    def strength(self):
        """'Declared', 'Strong' (values found for at least 95% of the sample), 'Likely' (other
        trusted links) or 'Weak'."""
        if self.kind == "declared":
            return "Declared"
        if not self.confident:
            return "Weak"
        return "Strong" if self.overlap is not None and self.overlap >= 0.95 else "Likely"

    def evidence(self):
        """'97% of 200 sampled', '' when no values were checked."""
        if self.overlap is None:
            return ""
        pct = "%d%%" % round(100 * self.overlap)
        return "%s of %s sampled" % (pct, format(self.sampled, ",")) if self.sampled else pct

    def where(self, node, column):
        """'table.column', or in a case 'wa.db › table.column'."""
        name = "%s.%s" % (node[1], col_name(column))
        return name if node[0] == DB else "%s › %s" % (node[0], name)

    def databases(self):
        """'wa.db' for a link inside a database, 'wa.db → msgstore.db' across two."""
        if self.src[0] == DB:
            return ""
        return self.src[0] if self.src[0] == self.dst[0] else "%s → %s" % (self.src[0],
                                                                                 self.dst[0])

    def found_text(self):
        return "%d%%" % round(100 * self.overlap) if self.overlap is not None else ""

    def pair_text(self):
        """'message_row_id → _id' ('=' for two columns holding the same values)."""
        return "%s %s %s" % (col_name(self.src_col), "=" if self.direction == "peer" else "→",
                             col_name(self.dst_col))

    def row(self):
        """The list line: from, to, strength, link, reason, overlap, rows, databases."""
        return (self.where(self.src, self.src_col), self.where(self.dst, self.dst_col),
                "%.2f" % self.score, self.kind_text(), self.reason, self.found_text(),
                "%s / %s" % (_n(self.src_rows), _n(self.dst_rows)), self.databases())


class ValueLink(Link):
    """A link between two databases of a case (engine.crossdb.CrossLink): always 'matched by
    value'; kind 'value' when trusted, else 'weaker'."""
    __slots__ = ()

    def __init__(self, link, names, rows=None):
        self.relation = None
        self.direction = "out"
        self.cross = True
        self.src, self.src_col = (names[link.src_db], link.src_table), link.src_col
        self.dst, self.dst_col = (names[link.dst_db], link.dst_table), link.dst_col
        self.confident = link.confident
        self.kind = "value" if link.confident else "weaker"
        self.score = link.score
        self.reason = link.reason()
        self.overlap = link.fraction if link.overlap is not None else None
        self.sampled = link.overlap.sampled if link.overlap is not None else None
        rows = rows or (lambda db, t: None)
        self.src_rows = rows(link.src_db, link.src_table)
        self.dst_rows = rows(link.dst_db, link.dst_table)
        self._empty_rule()


# -- text sizes -------------------------------------------------------------------------------
class TextMetrics(object):
    """Text sizes without a screen (the SVG, tests): generous widths per character. The tab
    passes one that measures with the canvas fonts. width(text, kind) and line(kind) are in
    pixels; px(kind) is the font's pixel size (the SVG's font-size)."""
    SIZES = {"name": (12, 7.6), "col": (11, 6.8), "key": (11, 6.8), "group": (12, 7.6),
             "member": (11, 6.8), "label": (10, 6.2)}

    def width(self, text, kind):
        per = self.SIZES[kind][1]
        # wide (East Asian) characters take about two
        return sum(per * (2 if ord(ch) > 0x2e80 else 1) for ch in text)

    def line(self, kind):
        return self.SIZES[kind][0] + 5

    def px(self, kind):
        return self.SIZES[kind][0]


# -- the pieces of a layout -------------------------------------------------------------------
class Group(object):
    """Tables linked to the same table (anchor) in exactly the same way: drawn as one box."""
    __slots__ = ("gid", "anchor", "members", "pattern", "side", "links")

    def __init__(self, anchor, pattern, members):
        self.anchor = anchor
        self.pattern = pattern          # ((role, member column, anchor column, kind, cross,
        #                                    direction), ...) the same for every member
        self.members = sorted(members, key=lambda n: (node_label(n).lower(), n))
        self.gid = "group|%s|%s|%r" % (anchor[0], anchor[1], pattern)
        self.links = [[] for _p in pattern]     # per pattern line: every member's link
        refers = any(p[0] == "src" and p[5] != "peer" for p in pattern)
        referred = any(p[0] == "dst" and p[5] != "peer" for p in pattern)
        self.side = "in" if refers else ("out" if referred else "peer")

    def title(self):
        return "%d tables" % len(self.members)

    def lines(self):
        """One line per link of the pattern: 'message_row_id → message._id'."""
        name = node_label(self.anchor)
        out = []
        for role, mine, theirs, _kind, _cross, direction in self.pattern:
            if direction == "peer":
                out.append("%s = %s.%s" % (mine, name, theirs))
            elif role == "src":
                out.append("%s → %s.%s" % (mine, name, theirs))
            else:
                out.append("%s.%s → %s" % (name, theirs, mine))
        return out

    def text(self):
        """'142 tables · message_row_id → message._id' (tooltips, the side panel)."""
        return "%s · %s" % (self.title(), ", ".join(self.lines()))


class Box(object):
    """A box of the layout: a table ('table') or a group ('group'). lines: [(text, kind,
    port)], one per text line under the title; a line's port names what a link attaches to
    (a column of a table, a pattern line of a group)."""
    __slots__ = ("key", "kind", "title", "title_kind", "lines", "x", "y", "w", "h", "head_h",
                 "row_h", "group", "expanded", "members", "member_rects", "note", "matched",
                 "align")

    def __init__(self, key, kind, title, title_kind, lines):
        self.key, self.kind, self.title, self.title_kind = key, kind, title, title_kind
        self.lines = lines
        self.x = self.y = self.w = self.h = 0.0
        self.head_h = self.row_h = 0.0
        self.group = None
        self.expanded = False
        self.members = []           # the tables an expanded group lists (after Find)
        self.member_rects = []      # [(node, x, y, w, h)] once placed
        self.note = ""
        self.matched = False
        self.align = "center"

    def rect(self):
        return (self.x, self.y, self.w, self.h)

    def port_y(self, port):
        for i, (_t, _k, p) in enumerate(self.lines):
            if p == port:
                return self.y + self.head_h + self.row_h * i + self.row_h / 2.0
        return self.y + self.head_h / 2.0


class Edge(object):
    """A line of the layout: from box a (port pa) to box b (port pb), carrying one link, or
    for a group every member's link of one pattern line."""
    __slots__ = ("a", "b", "pa", "pb", "links", "kind", "cross", "confident", "peer", "points",
                 "label", "label_rect", "route")

    def __init__(self, a, pa, b, pb, links):
        self.a, self.pa, self.b, self.pb = a, pa, b, pb
        self.links = links
        l = links[0]
        self.kind, self.cross, self.confident = l.kind, l.cross, l.confident
        self.peer = l.direction == "peer"
        self.points = []
        self.label = l.pair_text() if len(links) == 1 else "%d links: %s" % (len(links),
                                                                             l.pair_text())
        self.label_rect = None      # (x, y, w, h) when the label is drawn on the line
        self.route = None

    def tip(self):
        """What hovering the line tells: the columns, the kind, the values found."""
        l = self.links[0]
        if len(self.links) == 1:
            found = l.found_text()
            return "%s → %s\n%s%s" % (l.where(l.src, l.src_col), l.where(l.dst, l.dst_col),
                                      l.kind_text(), ", values found %s" % found if found else "")
        fr = [x.overlap for x in self.links if x.overlap is not None]
        found = (", values found %d–%d%%" % (round(100 * min(fr)), round(100 * max(fr)))
                 if fr else "")
        return "%d links, %s\n%s%s" % (len(self.links), l.pair_text(), l.kind_text(), found)


class Layout(object):
    """A computed picture: boxes (key -> Box), edges, the groups, the bounds, and what was
    left out (hidden: links not drawn because a table of theirs is inside a group)."""

    def __init__(self, focus):
        self.focus = focus
        self.boxes = {}
        self.edges = []
        self.groups = []
        self.bounds = (0.0, 0.0, 10.0, 10.0)
        self.hidden = 0
        self.hidden_tables = 0      # two links away, reached only through a grouped table
        self.hops = 1
        self.matches = []           # tables whose name or a linked column matched Find
        self.components = 0
        self.seconds = 0.0

    def member_of(self):
        """node -> group box key of every table inside a group."""
        out = {}
        for g in self.groups:
            for n in g.members:
                out[n] = g.gid
        return out

    def rects(self):
        """Every box and listed table as (key, x, y, w, h), for overlap checks."""
        out = []
        for b in self.boxes.values():
            out.append((b.key, b.x, b.y, b.w, b.h))
        return out


def rects_overlap(a, b, gap=0.0):
    """True when two (x, y, w, h) rectangles share any area (touching edges do not)."""
    return (a[0] < b[0] + b[2] + gap and b[0] < a[0] + a[2] + gap and
            a[1] < b[1] + b[3] + gap and b[1] < a[1] + a[3] + gap)


def overlaps(rects):
    """Pairs of overlapping rectangles in [(key, x, y, w, h)] (sweep over x)."""
    order = sorted(rects, key=lambda r: r[1])
    out = []
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            if b[1] >= a[1] + a[3]:
                break
            if rects_overlap(a[1:], b[1:]):
                out.append((a[0], b[0]))
    return out


# -- the graph --------------------------------------------------------------------------------
class LinkGraph(object):
    """The links shown, the tables they join, and where each table's box goes."""

    def __init__(self, links, weaker=False):
        self.links = [l for l in links if weaker or l.confident]
        self.nodes = {}         # (db, table) -> set of linked columns
        self.keys = {}          # (db, table) -> set of its columns other links refer to
        self.adj = {}           # (db, table) -> [links touching it]
        for l in self.links:
            self.nodes.setdefault(l.src, set()).add(col_name(l.src_col))
            self.nodes.setdefault(l.dst, set()).add(col_name(l.dst_col))
            if l.kind != "weaker" and l.direction == "out":
                self.keys.setdefault(l.dst, set()).add(col_name(l.dst_col))
            self.adj.setdefault(l.src, []).append(l)
            if l.dst != l.src:
                self.adj.setdefault(l.dst, []).append(l)
        self.pos = {}           # (db, table) -> (x, y, w, h) of every table drawn as a box
        self.current = Layout(None)
        self.metrics = TextMetrics()

    def degree(self, node):
        return len(self.adj.get(node, ()))

    def neighbours(self, node):
        out = set()
        for l in self.adj.get(node, ()):
            out.add(l.dst if l.src == node else l.src)
        out.discard(node)
        return out

    def most_linked(self):
        if not self.nodes:
            return None
        return min(self.nodes, key=lambda n: (-self.degree(n), n))

    def column_lines(self, node):
        cols = sorted(self.nodes[node], key=lambda c: (c.lower(), c))
        keys = self.keys.get(node, ())
        return [(c + (" (key)" if c in keys else ""), "key" if c in keys else "col", c)
                for c in cols]

    def matches(self, node, term):
        """True when Find's words are in the table's name or one of its linked columns."""
        if not term:
            return False
        return term in node_label(node).lower() or any(term in c.lower()
                                                       for c in self.nodes.get(node, ()))

    # -- groups --------------------------------------------------------------------------------
    @staticmethod
    def _signature(links, member):
        sig = []
        for l in links:
            role = "src" if l.src == member else "dst"
            mine = col_name(l.src_col if role == "src" else l.dst_col)
            theirs = col_name(l.dst_col if role == "src" else l.src_col)
            sig.append((role, mine, theirs, l.kind, l.cross, l.direction))
        return tuple(sorted(set(sig)))

    def _between(self, a, b):
        return [l for l in self.adj.get(a, ()) if (l.src, l.dst) in ((a, b), (b, a))]

    def _group(self, candidates, minimum):
        """Groups of the candidates {member: anchor} linked to their anchor alike."""
        buckets = {}
        for n, anchor in candidates.items():
            links = self._between(n, anchor)
            if not links:
                continue
            buckets.setdefault((anchor, self._signature(links, n)), []).append(n)
        out = []
        for (anchor, sig), members in sorted(buckets.items(), key=lambda kv: repr(kv[0])):
            if len(members) >= minimum:
                g = Group(anchor, sig, members)
                for m in g.members:
                    for l in self._between(m, anchor):
                        g.links[list(sig).index(self._signature([l], m)[0])].append(l)
                out.append(g)
        return out

    # -- layout --------------------------------------------------------------------------------
    def layout(self, focus=None, expanded=(), find="", metrics=None, expand_all=False,
               group_min=None, hops=1):
        """Lay the diagram out: focused on a table (its neighbours around it, and with hops=2
        theirs further out) or the whole database (focus None). expanded: group ids listed
        table by table; find: words whose tables are marked (and whose groups are expanded to
        list only them); expand_all: every group lists all its tables (the SVG).
        Deterministic: the same links give the same picture. Returns the Layout (also kept in
        self.current; self.pos has every table box)."""
        import time
        t0 = time.time()
        if metrics is not None:
            self.metrics = metrics
        term = (find or "").strip().lower()
        minimum = group_min if group_min is not None else limits.get("diagram_group_min")
        lay = Layout(focus if focus in self.nodes else None)
        lay.hops = hops
        self._parent = {}
        if lay.focus is not None:
            shown = set([focus]) | self.neighbours(focus)
            groups = self._group(dict((n, focus) for n in shown if n != focus), minimum)
            if hops >= 2:
                grouped = set(m for g in groups for m in g.members)
                outer = set()
                for n in shown:
                    outer |= self.neighbours(n)
                outer -= shown
                for n in sorted(outer):
                    near = sorted((self.neighbours(n) & shown) - grouped - set([focus]),
                                  key=lambda p: (node_label(p).lower(), p))
                    if near:
                        self._parent[n] = near[0]
                    else:
                        lay.hidden_tables += 1      # only linked through a grouped table
                groups += self._group(self._parent, minimum)
                shown |= set(self._parent)
        else:
            shown = set(self.nodes)
            leaves = {}
            for n in shown:
                nb = self.neighbours(n)
                if len(nb) == 1 and not any(l.src == l.dst for l in self.adj.get(n, ())):
                    leaves[n] = next(iter(nb))
            groups = self._group(leaves, minimum)
        lay.groups = groups
        in_group = {}
        for g in groups:
            for m in g.members:
                in_group[m] = g
        # the boxes
        for n in sorted(shown):
            if n in in_group:
                continue
            b = Box(n, "table", node_label(n), "name", self.column_lines(n))
            b.matched = self.matches(n, term)
            self._size_table(b)
            lay.boxes[n] = b
        for g in groups:
            b = Box(g.gid, "group", g.title(), "group",
                    [(t, "col", i) for i, t in enumerate(g.lines())])
            b.group = g
            hits = [m for m in g.members if self.matches(m, term)]
            b.matched = bool(hits)
            b.expanded = expand_all or g.gid in set(expanded or ()) or bool(hits)
            if b.expanded:
                b.members = g.members if (expand_all or not hits) else hits
                if hits and not expand_all:
                    b.note = "%d of %d match “%s”" % (len(hits), len(g.members), term)
            elif hits:
                b.note = "%d match “%s”" % (len(hits), term)
            if b.note:
                b.lines = b.lines + [(b.note, "col", None)]
            self._size_group(b)
            lay.boxes[g.gid] = b
            lay.matches.extend(hits)
        lay.matches.extend(n for n, b in lay.boxes.items() if b.kind == "table" and b.matched)
        lay.matches = sorted(set(lay.matches))
        # the lines
        seen_group = set()
        for l in self.links:
            if l.src not in shown or l.dst not in shown:
                continue
            ga, gb = in_group.get(l.src), in_group.get(l.dst)
            if ga is None and gb is None:
                lay.edges.append(Edge(l.src, col_name(l.src_col), l.dst, col_name(l.dst_col),
                                      [l]))
                continue
            g = ga or gb
            member = l.src if ga is not None else l.dst
            other = l.dst if ga is not None else l.src
            if (ga is not None and gb is not None) or other != g.anchor:
                lay.hidden += 1         # joins a grouped table to one it is not grouped by
                continue
            i = list(g.pattern).index(self._signature([l], member)[0])
            if (g.gid, i) in seen_group:
                continue
            seen_group.add((g.gid, i))
            if ga is not None:
                lay.edges.append(Edge(g.gid, i, g.anchor, g.pattern[i][2], g.links[i]))
            else:
                lay.edges.append(Edge(g.anchor, g.pattern[i][2], g.gid, i, g.links[i]))
        # the cells
        if lay.focus is not None:
            blocks = [self._hub_cells(lay, in_group)]
        else:
            blocks = self._component_cells(lay)
        lay.components = len(blocks)
        placed = []
        for cells in blocks:
            placed.append(self._place(lay, cells))
        self._pack(lay, placed)
        self._labels(lay)
        self._bounds(lay)
        self.pos = {}
        for b in lay.boxes.values():
            if b.kind == "table":
                self.pos[b.key] = b.rect()
            for n, x, y, w, h in b.member_rects:
                self.pos[n] = (x, y, w, h)
        lay.seconds = time.time() - t0
        self.current = lay
        return lay

    def _size_table(self, b):
        m = self.metrics
        b.head_h = m.line("name") + 8
        b.row_h = m.line("col") + 2
        widths = [m.width(b.title, "name")] + [m.width(t, k) for t, k, _p in b.lines]
        b.w = max(BOX_W_MIN, max(widths) + 2 * PAD)
        b.h = b.head_h + b.row_h * len(b.lines) + 4

    def _size_group(self, b):
        m = self.metrics
        b.head_h = m.line("group") + 8
        b.row_h = m.line("col") + 2
        title = b.title + ("  ▾" if b.expanded else "  ▸")
        widths = [m.width(title, "group")] + [m.width(t, k) for t, k, _p in b.lines]
        w = max(widths) + 2 * PAD
        h = b.head_h + b.row_h * len(b.lines) + 4
        if b.expanded and b.members:
            mw = max(m.width(node_label(n), "member") for n in b.members) + 2 * 6
            mh = m.line("member") + 6
            k = len(b.members)
            cols = max(1, min(k, int(math.ceil(math.sqrt(k * mh * 2.5 / mw)))))
            rows = int(math.ceil(k / float(cols)))
            gw = cols * mw + (cols - 1) * MEMBER_GAP
            gh = rows * mh + (rows - 1) * MEMBER_GAP
            w = max(w, gw + 2 * PAD)
            h = h + gh + PAD
            b.member_rects = [(n, (i % cols) * (mw + MEMBER_GAP), (i // cols) * (mh + MEMBER_GAP),
                               mw, mh) for i, n in enumerate(b.members)]   # relative, for now
        b.w, b.h = max(BOX_W_MIN, w), h

    # cells: {box key: (row, col)}, and which column the hub is in (None: the whole database)
    def _hub_cells(self, lay, in_group):
        focus = lay.focus
        sides = {"in": [], "out": [], "peer": []}
        side_of = {}
        outer = []
        for key, b in lay.boxes.items():
            if key == focus:
                continue
            parent = b.group.anchor if b.kind == "group" else self._parent.get(key)
            if parent is not None and parent != focus:
                outer.append((b, parent))       # two links away: placed beyond its parent
                continue
            if b.kind == "group":
                side = b.group.side
            else:
                links = self._between(key, focus)
                if any(l.src == key and l.direction != "peer" for l in links):
                    side = "in"
                elif any(l.dst == key and l.direction != "peer" for l in links):
                    side = "out"
                else:
                    side = "peer"
            sides[side].append(b)
            side_of[key] = side
        far = {"in": [], "out": [], "peer": []}
        for b, parent in outer:
            far[side_of.get(parent, "peer")].append(b)
        order = lambda b: (0 if b.kind == "group" else 1,                       # noqa: E731
                           -len(b.group.members) if b.kind == "group" else 0,
                           b.title.lower(), repr(b.key))
        for d in (sides, far):
            for k in d:
                d[k].sort(key=order)

        def grid(items):
            """(columns, rows) of a block of boxes beside the hub."""
            n = len(items)
            if not n:
                return 0, 0
            cols = max(1, int(round(math.sqrt(n / 2.5))))
            return cols, int(math.ceil(n / float(cols)))
        (li, ri), (lo, ro) = grid(sides["in"]), grid(far["in"])
        (oi, rio), (oo, roo) = grid(sides["out"]), grid(far["out"])
        nrows = max(ri, ro, rio, roo, 1)
        hub_row, hub_col = (nrows - 1) // 2, li + lo
        cells = {focus: (hub_row, hub_col)}
        lay.boxes[focus].align = "center"

        def put(items, rows, first_col, step):
            start = hub_row - (rows - 1) // 2 if rows else 0
            for i, b in enumerate(items):
                cells[b.key] = (start + i % rows, first_col + step * (i // rows))
                b.align = "right" if step < 0 else "left"
        put(sides["in"], ri, hub_col - 1, -1)
        put(far["in"], ro, hub_col - 1 - li, -1)
        put(sides["out"], rio, hub_col + 1, 1)
        put(far["out"], roo, hub_col + 1 + oi, 1)
        total = hub_col + 1 + oi + oo
        near = sorted(range(total), key=lambda c: (abs(c - hub_col), c))
        below = sides["peer"] + far["peer"]
        for i, b in enumerate(below):
            cells[b.key] = (nrows + i // total, near[i % total])
        return cells

    def _component_cells(self, lay):
        """The whole database: one block of cells per group of connected boxes (largest
        first), each with its most linked box in the middle."""
        nbrs = dict((k, set()) for k in lay.boxes)
        for e in lay.edges:
            if e.a != e.b:
                nbrs[e.a].add(e.b)
                nbrs[e.b].add(e.a)
        label = dict((k, (b.title.lower(), repr(k))) for k, b in lay.boxes.items())
        seen, comps = set(), []
        for k in sorted(lay.boxes, key=lambda k: (-len(nbrs[k]), label[k])):
            if k in seen:
                continue
            # breadth first from the most linked box
            order, queue = [k], [k]
            seen.add(k)
            while queue:
                cur = queue.pop(0)
                for n in sorted(nbrs[cur] - seen, key=lambda n: (-len(nbrs[n]), label[n])):
                    seen.add(n)
                    order.append(n)
                    queue.append(n)
            comps.append(order)
        comps.sort(key=lambda o: (-len(o), label[o[0]]))
        blocks = []
        for order in comps:
            n = len(order)
            ncols = int(math.ceil(math.sqrt(n)))
            nrows = int(math.ceil(n / float(ncols)))
            cr, cc = (nrows - 1) / 2.0, (ncols - 1) / 2.0
            spots = sorted(((r, c) for r in range(nrows) for c in range(ncols)),
                           key=lambda rc: ((rc[0] - cr) ** 2 + (rc[1] - cc) ** 2, rc))
            blocks.append(dict(zip(order, spots)))
        return blocks

    def _place(self, lay, cells):
        """Size the columns, rows and gaps of one block, place its boxes and route its lines.
        Returns (keys, edges, width, height) with the block's top-left at (0, 0)."""
        nrows = max(r for r, _c in cells.values()) + 1
        ncols = max(c for _r, c in cells.values()) + 1
        colw, rowh = [0.0] * ncols, [0.0] * nrows
        for k, (r, c) in cells.items():
            b = lay.boxes[k]
            colw[c] = max(colw[c], b.w)
            rowh[r] = max(rowh[r], b.h)
        # route every line of the block through the gaps (by index), counting the lanes
        gut = [dict() for _i in range(ncols + 1)]      # gutter index -> {lane key: lane}
        chan = [dict() for _i in range(nrows + 1)]     # channel index -> {lane key: lane}
        edges = [e for e in lay.edges if e.a in cells and e.b in cells]

        def lane(table, key):
            return table.setdefault(key, len(table))
        for i, e in enumerate(edges):
            (ra, ca), (rb, cb) = cells[e.a], cells[e.b]
            if e.a == e.b:
                ea, eb, ga, gb = "r", "r", ca + 1, ca + 1
            elif ca < cb:
                ea, eb, ga, gb = "r", "l", ca + 1, cb
            elif ca > cb:
                ea, eb, ga, gb = "l", "r", ca, cb + 1
            else:
                ea, eb, ga, gb = "r", "r", ca + 1, ca + 1
            to = ("to", e.b, e.pb)
            if e.a == e.b:
                e.route = (ea, eb, ga, lane(gut[ga], ("self", i)), None, None, None, None)
            elif ga == gb:
                e.route = (ea, eb, ga, lane(gut[ga], to), None, None, None, None)
            else:
                ch = ra + 1 if rb >= ra else ra
                e.route = (ea, eb, ga, lane(gut[ga], ("from", e.a, e.pa, ch)), ch,
                           lane(chan[ch], to), gb, lane(gut[gb], to))
        gw = [max(GAP_X, (len(g) + 1) * LANE + 16) for g in gut]
        ch_h = [max(GAP_Y, (len(c) + 1) * LANE + 12) for c in chan]
        xs, x = [], gw[0]
        for c in range(ncols):
            xs.append(x)
            x += colw[c] + gw[c + 1]
        width = x
        ys, y = [], ch_h[0]
        for r in range(nrows):
            ys.append(y)
            y += rowh[r] + ch_h[r + 1]
        height = y
        for k, (r, c) in cells.items():
            b = lay.boxes[k]
            if b.align == "right":
                b.x = xs[c] + colw[c] - b.w
            elif b.align == "left":
                b.x = xs[c]
            else:
                b.x = xs[c] + (colw[c] - b.w) / 2.0
            b.y = ys[r] + (rowh[r] - b.h) / 2.0
            if b.member_rects:
                top = b.y + b.head_h + b.row_h * len(b.lines) + 4
                b.member_rects = [(n, b.x + PAD + rx, top + ry, w, h)
                                  for n, rx, ry, w, h in b.member_rects]

        def gutter_x(g, ln):
            left = (xs[g] - gw[g]) if g < ncols else xs[ncols - 1] + colw[ncols - 1]
            n = len(gut[g])
            return left + gw[g] / 2.0 + (ln - (n - 1) / 2.0) * LANE

        def channel_y(ch, ln):
            top = (ys[ch] - ch_h[ch]) if ch < nrows else ys[nrows - 1] + rowh[nrows - 1]
            n = len(chan[ch])
            return top + ch_h[ch] / 2.0 + (ln - (n - 1) / 2.0) * LANE
        for e in edges:
            a, b = lay.boxes[e.a], lay.boxes[e.b]
            ea, eb, ga, la, ch, lc, gb, lb = e.route
            ya, yb = a.port_y(e.pa), b.port_y(e.pb)
            xa = a.x + a.w if ea == "r" else a.x
            xb = b.x + b.w if eb == "r" else b.x
            if e.a == e.b and abs(ya - yb) < 1:
                yb = ya + 6
            gx = gutter_x(ga, la)
            if ch is None:
                pts = [(xa, ya), (gx, ya), (gx, yb), (xb, yb)]
            else:
                cy, gx2 = channel_y(ch, lc), gutter_x(gb, lb)
                pts = [(xa, ya), (gx, ya), (gx, cy), (gx2, cy), (gx2, yb), (xb, yb)]
            e.points = _simplify(pts)
        return (list(cells), edges, width, height)

    def _pack(self, lay, placed):
        """Put the blocks side by side, wrapping into rows (largest first, already sorted)."""
        if not placed:
            return
        area = sum(w * h for _k, _e, w, h in placed)
        row_w = max(max(w for _k, _e, w, _h in placed), math.sqrt(area) * 1.6)
        x = y = shelf = 0.0
        for keys, edges, w, h in placed:
            if x > 0 and x + w > row_w:
                x, y, shelf = 0.0, y + shelf + COMP_GAP, 0.0
            for k in keys:
                b = lay.boxes[k]
                b.x += x
                b.y += y
                b.member_rects = [(n, rx + x, ry + y, rw, rh) for n, rx, ry, rw, rh in
                                  b.member_rects]
            for e in edges:
                e.points = [(px + x, py + y) for px, py in e.points]
            x += w + COMP_GAP
            shelf = max(shelf, h)

    def _labels(self, lay):
        """Write 'column → column' on the lines when there are few (limit
        diagram_edge_labels), only where the label covers no box and no other label."""
        if len(lay.edges) > limits.get("diagram_edge_labels"):
            return
        m = self.metrics
        taken = [(b.x, b.y, b.w, b.h) for b in lay.boxes.values()]
        h = m.line("label")
        for e in sorted(lay.edges, key=lambda e: (repr(e.a), repr(e.pa), repr(e.b),
                                                  repr(e.pb))):
            w = m.width(e.label, "label") + 6
            spots = []
            for (x1, y1), (x2, y2) in zip(e.points, e.points[1:]):
                if abs(y1 - y2) < 0.5:
                    lo, hi = min(x1, x2), max(x1, x2)
                    mid = (lo + hi) / 2.0
                    for cx in (mid - w / 2.0, lo + 2, hi - w - 2):
                        spots.append((cx, y1 - h - 1))
                        spots.append((cx, y1 + 1))
            for sx, sy in spots:
                r = (sx, sy, w, h)
                if not any(rects_overlap(r, t) for t in taken):
                    e.label_rect = r
                    taken.append(r)
                    break

    def _bounds(self, lay):
        xs, ys = [], []
        for b in lay.boxes.values():
            xs += [b.x, b.x + b.w]
            ys += [b.y, b.y + b.h]
        for e in lay.edges:
            xs += [p[0] for p in e.points]
            ys += [p[1] for p in e.points]
            if e.label_rect:
                x, y, w, h = e.label_rect
                xs += [x, x + w]
                ys += [y, y + h]
        if not xs:
            lay.bounds = (0.0, 0.0, 10.0, 10.0)
            return
        lay.bounds = (min(xs) - MARGIN, min(ys) - MARGIN, max(xs) + MARGIN, max(ys) + MARGIN)

    # -- what the tab and the SVG ask ---------------------------------------------------------
    def shown(self):
        """The lines drawn (each carries one link, or a group's links of one pattern line)."""
        return list(self.current.edges)

    def edge_of(self, link):
        for e in self.current.edges:
            if any(x is link for x in e.links):
                return e
        return None

    def points(self, link):
        """The line of a link as points (a self-reference loops out of its box and back)."""
        e = self.edge_of(link)
        return list(e.points) if e is not None else []

    def svg(self, colors=None):
        """The diagram as a standalone SVG document (text): the current focus, with every
        group listing all its tables. colors: {database name: '#rrggbb'} outlines each
        table's box in its database's colour (a case)."""
        cur = self.current
        keep = (self.current, dict(self.pos), self.metrics)
        try:
            self.metrics = TextMetrics()
            lay = self.layout(cur.focus, expand_all=True)
        finally:
            self.current, self.pos, self.metrics = keep
        return render_svg(lay, self, colors or {})


def _simplify(pts):
    """Drop repeated points and middle points of straight runs."""
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


def render_svg(lay, graph, colors):
    """A Layout as an SVG document."""
    m = TextMetrics()
    if not lay.boxes:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>\n'
    x0, y0, x1, y1 = lay.bounds
    legend_h = 26
    y0 -= legend_h
    width, height = x1 - x0, y1 - y0
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="%.1f %.1f %.1f %.1f" font-family="Segoe UI, Helvetica, sans-serif" '
           'font-size="%d">' % (width, height, x0, y0, width, height, m.px("col")),
           '<defs><marker id="arrow" viewBox="0 0 8 8" refX="8" refY="4" markerWidth="7" '
           'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" '
           'fill="context-stroke"/></marker></defs>',
           '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#ffffff"/>'
           % (x0, y0, width, height)]
    # legend
    lx, ly = x0 + MARGIN, y0 + 16
    for text, color, dash in (("declared foreign key", "#0052cc", ""),
                              ("verified by values", "#0052cc", "6,4"),
                              ("between databases (matched by value)", CROSS_COLOR, "3,2"),
                              ("weaker", "#8993a4", "2,3")):
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                   'stroke-width="1.6"%s/>' % (lx, ly - 4, lx + 26, ly - 4, color,
                                               ' stroke-dasharray="%s"' % dash if dash else ""))
        out.append('<text x="%.1f" y="%.1f" fill="#5e6c84">%s</text>' % (lx + 30, ly,
                                                                          escape(text)))
        lx += 30 + m.width(text, "col") + 18
    out.append('<rect x="%.1f" y="%.1f" width="22" height="12" rx="3" fill="#fff4e5" '
               'stroke="#8a6d3b"/>' % (lx, ly - 10))
    out.append('<text x="%.1f" y="%.1f" fill="#5e6c84">group: tables linked alike</text>'
               % (lx + 26, ly))
    for e in lay.edges:
        dash = LINE_STYLES[e.kind][1] if not e.cross else CROSS_STYLE[1]
        dash = ' stroke-dasharray="%s"' % ",".join(str(x) for x in dash) if dash else ""
        color = "#8993a4" if e.kind == "weaker" else (CROSS_COLOR if e.cross else "#0052cc")
        title = escape(e.tip())
        out.append('<polyline class="link" points="%s" fill="none" stroke="%s" '
                   'stroke-width="%s"%s%s><title>%s</title></polyline>'
                   % (" ".join("%.1f,%.1f" % p for p in e.points), color,
                      "1.8" if e.cross else "1.4", dash,
                      "" if e.peer else ' marker-end="url(#arrow)"', title))
    for e in lay.edges:
        if e.label_rect:
            x, y, w, h = e.label_rect
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#ffffff" '
                       'fill-opacity="0.9"/>' % (x, y, w, h))
            out.append('<text x="%.1f" y="%.1f" fill="#5e6c84" font-size="%d">%s</text>'
                       % (x + 3, y + h - 4, m.px("label"), escape(e.label)))
    for key in sorted(lay.boxes, key=repr):
        b = lay.boxes[key]
        if b.kind == "group":
            edge, fill, head = "#8a6d3b", "#fffaf0", "#fff4e5"
        else:
            edge = valid_color(colors.get(key[0]), "#5e6c84")
            fill, head = "#f7f8fa", "#deebff"
        strong = b.kind == "table" and key[0] in colors
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="4" '
                   'fill="%s" stroke="%s"%s/>' % (b.x, b.y, b.w, b.h, fill, escape(edge),
                                                  ' stroke-width="2"' if strong else ""))
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="4" '
                   'fill="%s" stroke="%s"/>' % (b.x, b.y, b.w, b.head_h, head, escape(edge)))
        out.append('<text x="%.1f" y="%.1f" font-weight="bold" font-size="%d">%s</text>'
                   % (b.x + PAD, b.y + b.head_h / 2.0 + m.px("name") * 0.35, m.px("name"),
                      escape(b.title)))
        for i, (text, kind, _p) in enumerate(b.lines):
            out.append('<text x="%.1f" y="%.1f"%s>%s</text>'
                       % (b.x + PAD, b.y + b.head_h + b.row_h * i + b.row_h / 2.0 +
                          m.px("col") * 0.35, ' font-style="italic"' if kind == "key" else "",
                          escape(text)))
        for n, x, y, w, h in b.member_rects:
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" '
                       'fill="#ffffff" stroke="#8a6d3b"/>' % (x, y, w, h))
            out.append('<text x="%.1f" y="%.1f">%s</text>'
                       % (x + 6, y + h / 2.0 + m.px("member") * 0.35, escape(node_label(n))))
    out.append("</svg>")
    return "\n".join(out) + "\n"
