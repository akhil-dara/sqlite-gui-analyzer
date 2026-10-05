"""Structured problems found while reading a database.

The engine never swallows a failure silently: anything it had to skip,
substitute or guess is recorded as an Issue that the UI can show.
"""


class Issue(object):
    __slots__ = ("kind", "detail", "where", "severity")

    def __init__(self, kind, detail="", where="", severity="warning"):
        self.kind = kind          # short machine-readable tag, e.g. "invalid_text"
        self.detail = detail      # human-readable explanation
        self.where = where        # location, e.g. "page 12 offset 3001" or a table name
        self.severity = severity  # "info" | "warning" | "error"

    def as_dict(self):
        return {"kind": self.kind, "detail": self.detail,
                "where": self.where, "severity": self.severity}

    def __repr__(self):
        return "Issue(%s, %r, %r)" % (self.kind, self.detail, self.where)


class IssueLog(object):
    """Append-only list of Issues with a cap so hostile files can't exhaust memory: the limit
    issues_kept (Limits…), unless a cap is given; the ones past it are counted in `dropped`
    and the Issues window says so."""

    def __init__(self, cap=None):
        if cap is None:
            from . import limits
            cap = limits.get("issues_kept")
        self.items = []
        self.cap = cap
        self.dropped = 0
        self._distinct = set()      # the same row read twice (browse, then search) logs twice

    def add(self, kind, detail="", where="", severity="warning"):
        if len(self._distinct) < self.cap:
            self._distinct.add((severity, kind, where, detail))
        if len(self.items) < self.cap:
            self.items.append(Issue(kind, detail, where, severity))
        else:
            self.dropped += 1

    def distinct_count(self):
        """How many different issues were logged (repeats of one issue count once)."""
        return len(self._distinct)

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)
