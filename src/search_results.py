"""Search results grouped by row: one line per row however many of its cells match.

A table search yields one hit per matching cell, and a WAL search one per matching cell of
each row version kept in WAL frames (identical copies in several frames are already one
version, with every frame listed). ResultGrouper turns those hits into RowGroups:

- the hits of one database row form one group;
- the hits of one WAL row version form one group, unless that version is identical to a
  database row that also matched: then it is the same row, and its frames are folded into the
  database row's group ("DB + WAL x3") instead of repeating it.
"""

WAL_STATE_ORDER = ("current", "superseded", "uncommitted", "stale")


def is_wal(hit):
    return hit.get("source", "DB").startswith("WAL")


def source_kind(hit):
    """"DB", "WAL" or "Freelist" (records still held by freed pages)."""
    src = hit.get("source", "DB")
    if src.startswith("WAL"):
        return "WAL"
    if src.startswith("Freelist"):
        return "Freelist"
    return "DB"


def _frames_of(hit):
    frames = hit.get("frames")
    if frames:
        return list(frames)
    if hit.get("frame_idx") is not None:
        return [(hit["frame_idx"], hit.get("page_num"), hit.get("category", ""))]
    return []


def match_label(hit):
    """Type column text: the value's type, plus how and where a BLOB matched
    ("BLOB utf-16le @30", "blob_hex hex @4", "BLOB decoded")."""
    enc = hit.get("encoding")
    if not enc or enc == "text":
        return hit["type"]
    off = hit.get("offset")
    return "%s %s%s" % (hit["type"], enc, "" if off is None else " @%d" % off)


def frames_json(hit):
    """Every WAL frame holding a hit's row version, for exports ([] for a database hit)."""
    return [{"frame": f[0], "page": f[1], "state": f[2]} for f in hit.get("frames") or ()]


def _same_row(a, b):
    """Whether two rows hold the same values (1 and 1.0 are the same number, as in SQLite)."""
    if a is None or b is None or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x is None or y is None:
            if x is not y:
                return False
        elif type(x) in (int, float) and type(y) in (int, float):
            if x != y:
                return False
        elif type(x) is not type(y) or x != y:
            return False
    return True


class RowGroup(object):
    __slots__ = ("table", "locator", "rowid", "source", "row", "hits", "frames", "order",
                 "dbid", "database")

    def __init__(self, hit, order):
        # the database the row is in: a case member's uid and name (None / '' for a search
        # of one database)
        self.dbid, self.database = hit.get("dbid"), hit.get("database", "")
        self.table = hit["table"]
        self.locator = hit.get("locator")
        self.rowid = hit["rowid"]
        self.source = source_kind(hit)
        self.row = hit.get("row")
        self.hits = []
        self.frames = []        # (frame index, page, state) of each WAL copy of this row version
        self.order = order

    @property
    def first(self):
        return self.hits[0]

    @property
    def columns(self):
        seen = []
        for h in self.hits:
            if h["column"] not in seen:
                seen.append(h["column"])
        return seen

    def states(self):
        present = set(f[2] for f in self.frames)
        return [s for s in WAL_STATE_ORDER if s in present] + sorted(present - set(WAL_STATE_ORDER))

    @property
    def category(self):
        """Frame state that colours a WAL line: that of the newest frame holding the version."""
        return max(self.frames)[2] if self.source == "WAL" and self.frames else ""

    def source_label(self):
        n = len(self.frames)
        if self.source == "DB":
            return "DB + WAL ×%d" % n if n else "DB"
        if self.source == "Freelist":
            first = self.hits[0] if self.hits else {}
            return "Freelist p.%s" % first.get("page", "?")
        label = "WAL " + "/".join(self.states())
        return label + (" ×%d" % n if n > 1 else "")

    def columns_label(self, most=3):
        cols = self.columns
        if len(cols) <= most:
            return ", ".join(cols)
        return "%s +%d" % (", ".join(cols[:most]), len(cols) - most)

    def frames_label(self, most=12):
        shown = ["#%d %s" % (f[0], f[2]) for f in sorted(self.frames)[:most]]
        more = len(self.frames) - most
        return ", ".join(shown) + (", +%d more" % more if more > 0 else "")


class ResultGrouper(object):
    """Adds hits one by one (in the order a search yields them) and keeps the groups."""

    def __init__(self):
        self.groups = []
        self._by_key = {}
        self._db_rows = {}          # (table, locator) -> database row group
        self._folded = set()        # WAL versions folded into a database row group

    def add(self, hit):
        """File `hit` under its group and return that group."""
        loc = hit.get("locator")
        if loc is None:             # a column-name match: nothing to group by
            return self._new(hit, None)
        dbid = hit.get("dbid")      # rows of different databases never share a group
        if source_kind(hit) == "Freelist":
            # one record of a freed page (its locator carries the record itself)
            key = (dbid, "Freelist", hit.get("page"), hit.get("cell_offset"), hit["table"], loc)
            g = self._by_key.get(key) or self._new(hit, key)
            g.hits.append(hit)
            return g
        if not is_wal(hit):
            key = (dbid, "DB", hit["table"], loc)
            g = self._by_key.get(key) or self._new(hit, key)
            self._db_rows.setdefault((dbid, hit["table"], loc), g)
            g.hits.append(hit)
            return g
        try:
            key = (dbid, "WAL", hit["table"], loc, tuple(hit.get("row") or ()))
            hash(key)
        except TypeError:
            key = (dbid, "WAL", hit["table"], loc, id(hit.get("row")))
        g = self._by_key.get(key)
        if g is None:
            db = self._db_rows.get((dbid, hit["table"], loc))
            if db is not None and _same_row(db.row, hit.get("row")):
                if key not in self._folded:
                    self._folded.add(key)
                    db.frames.extend(_frames_of(hit))
                return db
            g = self._new(hit, key)
            g.frames = _frames_of(hit)
        g.hits.append(hit)
        return g

    def _new(self, hit, key):
        g = RowGroup(hit, len(self.groups))
        if key is None:
            g.hits.append(hit)
        else:
            self._by_key[key] = g
        self.groups.append(g)
        return g


def group_hits(hits):
    grouper = ResultGrouper()
    for h in hits:
        grouper.add(h)
    return grouper.groups
