"""Freelist walking and cell extraction from freed pages (the freelist's page list; the
Forensics tab's Freed Pages recovers their records with the carver).

Decodes the cells that are still referenced by a freed page's cell pointer
array; unreferenced free space is not carved.
"""

import struct

from .btree import (BTreeReader, INDEX_INTERIOR, INDEX_LEAF, TABLE_LEAF,
                    parse_page_header)


def freelist_pages(pager, issues=None):
    """Return (trunk_pages, leaf_pages) following the chain from the header."""
    h = pager.header
    trunks, leaves, seen = [], [], set()
    nxt = h.freelist_trunk
    max_leaves = pager.usable_size // 4 - 2
    while nxt and nxt not in seen:
        if nxt > pager.page_count:
            if issues is not None:
                issues.add("bad_freelist", "trunk page %d beyond end of database" % nxt)
            break
        seen.add(nxt)
        trunks.append(nxt)
        data = pager.page(nxt)
        following, count = struct.unpack_from(">II", data, 0)
        if count > max_leaves:
            if issues is not None:
                issues.add("bad_freelist", "trunk %d claims %d leaves (max %d)" % (nxt, count, max_leaves))
            count = max_leaves
        for i in range(count):
            leaf = struct.unpack_from(">I", data, 8 + 4 * i)[0]
            if 0 < leaf <= pager.page_count and leaf not in seen:
                seen.add(leaf)
                leaves.append(leaf)
        nxt = following
    return trunks, leaves


def page_cells(pager, page_no, issues=None):
    """Cells still referenced by a (freed) page: list of (kind, rowid_or_None, payload, CellRef).

    kind is 'table' for table-leaf cells and 'index' for index-leaf/interior cells
    (index entries or WITHOUT ROWID rows).
    """
    reader = BTreeReader(pager, issues)
    data = pager.page(page_no)
    try:
        h = parse_page_header(data, page_no)
    except Exception:
        return []
    out = []
    if h.type == TABLE_LEAF:
        for rowid, payload, ref in reader.read_segment(page_no, None, False):
            out.append(("table", rowid, payload, ref))
    elif h.type == INDEX_LEAF:
        for _, payload, ref in reader.read_segment(page_no, None, True):
            out.append(("index", None, payload, ref))
    elif h.type == INDEX_INTERIOR:
        for i in range(len(reader._offsets(data, h))):
            for _, payload, ref in reader.read_segment(page_no, i, True):
                out.append(("index", None, payload, ref))
    return out
