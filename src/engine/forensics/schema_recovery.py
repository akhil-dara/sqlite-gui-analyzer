"""Dropped (or redefined) tables, indexes, views and triggers.

DROP TABLE deletes the object's sqlite_master row like any other row, so the row survives in
the free space of the schema pages (page 1 and the other sqlite_master pages), in WAL copies
of those pages (an object created and dropped while the WAL was live is only there), in
journal images and on freed pages. Those rows are carved with the fixed five-column
sqlite_master layout (type, name, tbl_name, rootpage, sql) and every one that the current
schema does not hold is returned:
  status 'dropped'  no current object has that type and name;
  status 'changed'  an object with that name exists with another definition (e.g. before an
                    ALTER TABLE).
For a dropped table the root page is examined: when it is still a b-tree page of the right
kind and no live tree uses it, its rows can be read natively (DroppedObject.rows()).
"""

import re

from ..backends import NativeTable
from ..fileformat.btree import (INDEX_INTERIOR, INDEX_LEAF, TABLE_INTERIOR, TABLE_LEAF,
                                parse_page_header)
from ..issues import IssueLog
from ..schema import TableInfo, collation_names, describe_table
from .carve import Carver
from .pages import MASTER
from .provenance import FREELIST, JOURNAL, ORPHAN, REPLACED, UNALLOCATED, WAL, FREEBLOCK
from .templates import Template

OBJECT_TYPES = ("table", "index", "view", "trigger")
_CREATE = re.compile(r"^\s*CREATE\s", re.IGNORECASE)
# a free page can only hold a schema row if it holds a CREATE statement (UTF-8 or UTF-16)
_CREATE_BYTES = re.compile(b"(?i)c\x00?r\x00?e\x00?a\x00?t\x00?e\x00?\\s")


class DroppedObject(object):
    """One recovered schema object. `record` is the carved sqlite_master Record (with its
    provenance and copies); `info` a TableInfo for tables whose CREATE statement parses."""

    def __init__(self, record, status):
        self.record = record
        self.type, self.name, self.tbl_name, self.rootpage, self.sql = \
            (list(record.values) + [None] * 5)[:5]
        self.status = status
        self.info = None
        self.root_status = ""
        self.readable = False
        self._view = None

    def as_dict(self):
        return {"type": self.type, "name": self.name, "tbl_name": self.tbl_name,
                "rootpage": self.rootpage, "sql": self.sql, "status": self.status,
                "root_status": self.root_status, "readable": self.readable,
                "columns": self.info.column_names if self.info is not None else [],
                "record": self.record.as_dict()}

    def rows(self, limit=None, issues=None):
        """[(locator, row, flags)] read natively from the dropped table's b-tree, when its root
        page still holds one (`readable`); [] otherwise."""
        if not self.readable or self.info is None:
            return []
        nt = NativeTable(self.info, self._view, issues if issues is not None else IssueLog())
        out = []
        for item in nt.iter_all():
            out.append(item)
            if limit is not None and len(out) >= limit:
                break
        return out

    def __repr__(self):
        return "DroppedObject(%s %s, %s)" % (self.type, self.name, self.status)


def _master_pages(fx):
    pages = set([1])
    for pmap in (fx.eff_map, fx.main_map):
        pages.update(p for p, (name, _kind) in pmap.owner.items() if name == MASTER)
    return pages


def recover(fx, cancel=None, deadline=None):
    """DroppedObjects for every schema row found outside the current schema."""
    master_pages = _master_pages(fx)

    def wanted(file, page, status):
        if page in master_pages:
            return True
        return file == "main" and status in ("freelist", "trunk", "other", "beyond")

    carver = Carver(fx, [Template.master()],
                    sources=(FREEBLOCK, UNALLOCATED, FREELIST, ORPHAN, WAL, REPLACED, JOURNAL),
                    cancel=cancel, deadline=deadline, include_schema=True, unattributed=False,
                    page_filter=wanted, free_filter=lambda data: bool(_CREATE_BYTES.search(data)))
    result = carver.run()
    current = {}
    for e in fx.session.schema.entries:
        current[(e.type, e.name)] = e
    out, seen = [], {}
    for rec in result:
        if not _valid(rec.values):
            continue
        typ, name, _tbl, _root, sql = rec.values
        key = (typ, name, sql)
        if key in seen:
            seen[key].record.copies.extend([rec.prov] + rec.copies)
            continue
        cur = current.get((typ, name))
        if cur is not None and (cur.sql or None) == sql:
            continue
        obj = DroppedObject(rec, "changed" if cur is not None else "dropped")
        seen[key] = obj
        out.append(obj)
    colls = fx.session.schema.collations | collation_names(o.sql for o in out)
    for obj in out:
        _examine(fx, obj, colls)
    out.sort(key=lambda o: (o.status, o.type, o.name))
    return out, result


def _valid(values):
    if len(values) != 5:
        return False
    typ, name, tbl, root, sql = values
    if typ not in OBJECT_TYPES or not isinstance(name, str) or not name:
        return False
    if not isinstance(tbl, str) or not isinstance(root, int) or root < 0:
        return False
    if sql is None:
        return typ == "index" and name.startswith("sqlite_autoindex_")
    return isinstance(sql, str) and bool(_CREATE.match(sql))


def _examine(fx, obj, collations):
    if obj.type == "table" and obj.sql and not obj.sql.lstrip().upper().startswith("CREATE VIRTUAL"):
        t = TableInfo(obj.name, "table", obj.rootpage, obj.sql)
        try:
            describe_table(t, collations)
        except Exception:
            t.columns = []
        if t.columns:
            obj.info = t
    if obj.type not in ("table", "index") or not obj.rootpage:
        obj.root_status = "no b-tree"
        return
    view = fx.session.pager
    root = obj.rootpage
    if root > view.page_count:
        obj.root_status = "root page %d is beyond the end of the database" % root
        return
    owner = fx.eff_map.owner.get(root)
    if owner is not None and owner[0] != obj.name:
        obj.root_status = "root page %d is now used by %s" % (root, owner[0])
        return
    if root in fx.eff_map.trunks:
        obj.root_status = "root page %d became a freelist trunk (overwritten)" % root
        return
    try:
        h = parse_page_header(view.page(root), root)
    except Exception:
        obj.root_status = "root page %d is no longer a b-tree page" % root
        return
    wr = obj.info is not None and obj.info.without_rowid
    expect = (INDEX_LEAF, INDEX_INTERIOR) if (obj.type == "index" or wr) else \
        (TABLE_LEAF, TABLE_INTERIOR)
    if h.type not in expect:
        obj.root_status = "root page %d holds another kind of b-tree page" % root
        return
    where = "on the freelist" if root in fx.eff_map.leaves else \
        ("still in use" if owner is not None else "unreferenced")
    obj.root_status = "root page %d is still a b-tree page (%s)" % (root, where)
    if obj.type == "table" and obj.info is not None:
        obj._view = view
        obj.readable = True
        try:
            sample = obj.rows(limit=5)
        except Exception:
            sample = None
        if sample is None or any("damaged_record" in flags or "extra_values" in flags
                                 for _l, _r, flags in sample):
            obj.readable = False
            obj.root_status += "; its cells do not decode as this table"
