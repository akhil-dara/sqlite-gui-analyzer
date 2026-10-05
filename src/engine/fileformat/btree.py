"""B-tree traversal: table trees (rowid order) and index trees (key order).

WITHOUT ROWID tables are index trees whose interior cells also hold full rows,
so iter_index() yields interior-cell records in-order between child subtrees.
Every cell comes with provenance (page, offset, overflow pages) for later phases.
"""

import struct

from .. import limits
from .record import read_varint, to_signed64, RecordError

TABLE_INTERIOR, TABLE_LEAF, INDEX_INTERIOR, INDEX_LEAF = 0x05, 0x0D, 0x02, 0x0A
BTREE_TYPES = (TABLE_INTERIOR, TABLE_LEAF, INDEX_INTERIOR, INDEX_LEAF)
MAX_DEPTH = 64              # the default of limits 'btree_max_depth'


class CellRef(object):
    __slots__ = ("page", "offset", "overflow")

    def __init__(self, page, offset, overflow=()):
        self.page, self.offset, self.overflow = page, offset, tuple(overflow)

    def __repr__(self):
        return "CellRef(page=%d, offset=%d, overflow=%r)" % (self.page, self.offset, self.overflow)


class PageHeader(object):
    __slots__ = ("page_no", "hdr_off", "type", "first_freeblock", "cell_count",
                 "content_start", "fragmented", "right_child", "ptr_start")


def parse_page_header(data, page_no):
    """Parse the B-tree page header; raises RecordError if the type byte is not a B-tree type."""
    off = 100 if page_no == 1 else 0
    h = PageHeader()
    h.page_no, h.hdr_off, h.type = page_no, off, data[off]
    if h.type not in BTREE_TYPES:
        raise RecordError("page %d: not a b-tree page (type 0x%02x)" % (page_no, h.type))
    h.first_freeblock, h.cell_count, cs = struct.unpack_from(">HHH", data, off + 1)
    h.content_start = 65536 if cs == 0 else cs
    h.fragmented = data[off + 7]
    interior = h.type in (TABLE_INTERIOR, INDEX_INTERIOR)
    h.right_child = struct.unpack_from(">I", data, off + 8)[0] if interior else None
    h.ptr_start = off + (12 if interior else 8)
    return h


def cell_pointers(data, h, usable):
    ptrs = []
    for i in range(h.cell_count):
        p = h.ptr_start + 2 * i
        if p + 2 > usable:
            break
        ptrs.append(struct.unpack_from(">H", data, p)[0])
    return ptrs


def local_payload_size(payload_len, usable, is_table_leaf):
    """Bytes of payload stored on the b-tree page itself (fileformat2 §1.6)."""
    if is_table_leaf:
        max_local = usable - 35
    else:
        max_local = ((usable - 12) * 64 // 255) - 23
    if payload_len <= max_local:
        return payload_len
    min_local = ((usable - 12) * 32 // 255) - 23
    k = min_local + ((payload_len - min_local) % (usable - 4))
    return k if k <= max_local else min_local


class BTreeReader(object):
    def __init__(self, pager, issues=None):
        self.pager = pager
        self.usable = pager.usable_size
        self.issues = issues
        self.max_depth = limits.get("btree_max_depth")
        self.max_payload = limits.get("native_payload_bytes")

    # -- helpers -------------------------------------------------------
    def _issue(self, kind, detail, where):
        if self.issues is not None:
            self.issues.add(kind, detail, where)

    def _header(self, page_no):
        data = self.pager.page(page_no)
        return data, parse_page_header(data, page_no)

    def _offsets(self, data, h):
        """Cell pointers that land inside the page's cell area; others are logged and skipped."""
        lo = h.ptr_start + 2 * h.cell_count
        good = []
        for i, off in enumerate(cell_pointers(data, h, self.usable)):
            if lo <= off <= self.usable - 4:
                good.append(off)
            else:
                self._issue("bad_cell_pointer", "cell %d points to offset %d (valid %d..%d)"
                            % (i, off, lo, self.usable - 4), "page %d" % h.page_no)
        return good

    def read_payload(self, data, pos, payload_len, is_table_leaf, where=""):
        """Return (payload_bytes, overflow_page_list) for a payload starting at data[pos].

        The payload is what the cell and its overflow chain really hold: a chain that ends
        early (or a declared length beyond the database, or beyond limits
        'native_payload_bytes') gives the bytes read so far, never invented ones, and an Issue
        says how many are missing. The record decoder then marks the row damaged."""
        local = local_payload_size(payload_len, self.usable, is_table_leaf)
        if pos + local > self.usable:
            self._issue("payload_clamped", "cell payload (%d bytes local) runs past end of page; "
                        "kept the %d bytes that fit" % (local, max(0, self.usable - pos)), where)
            return bytes(data[pos:self.usable]), []
        if local == payload_len:
            return bytes(data[pos:pos + local]), []
        parts = [bytes(data[pos:pos + local])]
        need = payload_len - local
        per_page = self.usable - 4
        # an overflow chain cannot hold more than the database's pages
        most = min(self.max_payload - local,
                   max(0, self.pager.page_count - 1) * per_page)
        if need > most:
            self._issue("payload_cut", "cell declares %d payload bytes; at most %d are read (the "
                        "database holds no more, or limit native_payload_bytes)"
                        % (payload_len, local + max(0, most)), where)
        want = max(0, min(need, most))
        nxt = struct.unpack_from(">I", data, pos + local)[0]
        chain, seen = [], set()
        max_pages = want // per_page + 2
        got = 0
        while got < want:
            if nxt == 0 or nxt in seen or len(chain) >= max_pages:
                self._issue("overflow_truncated",
                            "overflow chain ended with %d bytes missing" % (need - got), where)
                break
            try:
                ov = self.pager.page(nxt)
            except Exception as e:      # noqa: BLE001 - a page number outside the file
                self._issue("overflow_truncated", "overflow page %d unreadable (%s) with %d "
                            "bytes missing" % (nxt, e, need - got), where)
                break
            seen.add(nxt)
            chain.append(nxt)
            take = min(want - got, per_page)
            parts.append(bytes(ov[4:4 + take]))
            got += take
            nxt = struct.unpack_from(">I", ov, 0)[0]
        return b"".join(parts), chain

    def _table_leaf_cell(self, data, page_no, off):
        payload_len, p = read_varint(data, off)
        rowid, p = read_varint(data, p)
        where = "page %d offset %d" % (page_no, off)
        payload, chain = self.read_payload(data, p, payload_len, True, where)
        return to_signed64(rowid), payload, CellRef(page_no, off, chain)

    def _index_cell(self, data, page_no, off, interior):
        p = off + 4 if interior else off
        payload_len, p = read_varint(data, p)
        where = "page %d offset %d" % (page_no, off)
        payload, chain = self.read_payload(data, p, payload_len, False, where)
        return payload, CellRef(page_no, off, chain)

    # -- traversal -----------------------------------------------------
    def iter_table(self, root):
        """Yield (rowid, payload, CellRef) for every row of a table b-tree, in rowid order."""
        stack, seen = [(root, 0)], set()
        while stack:
            page_no, depth = stack.pop()
            if page_no in seen or depth > self.max_depth:
                self._issue("btree_loop", "page %d revisited or too deep" % page_no, "root %d" % root)
                continue
            seen.add(page_no)
            try:
                data, h = self._header(page_no)
            except Exception as e:
                self._issue("bad_page", str(e), "page %d" % page_no)
                continue
            ptrs = self._offsets(data, h)
            if h.type == TABLE_LEAF:
                for off in ptrs:
                    try:
                        yield self._table_leaf_cell(data, page_no, off)
                    except Exception as e:
                        self._issue("bad_cell", str(e), "page %d offset %d" % (page_no, off))
            elif h.type == TABLE_INTERIOR:
                children = []
                for off in ptrs:
                    if off + 4 <= self.usable:
                        children.append(struct.unpack_from(">I", data, off)[0])
                children.append(h.right_child)
                for child in reversed(children):
                    stack.append((child, depth + 1))
            else:
                self._issue("bad_page", "index page 0x%02x inside table tree" % h.type,
                            "page %d" % page_no)

    def iter_index(self, root):
        """Yield (payload, CellRef) for every entry of an index b-tree, in key order,
        including the entries held by interior cells (WITHOUT ROWID rows live there too)."""
        stack, seen = [("page", root, 0)], set()
        while stack:
            kind, page_no, arg = stack.pop()
            if kind == "cell":
                off = arg
                try:
                    yield self._index_cell(self.pager.page(page_no), page_no, off, True)
                except Exception as e:
                    self._issue("bad_cell", str(e), "page %d offset %d" % (page_no, off))
                continue
            depth = arg
            if page_no in seen or depth > self.max_depth:
                self._issue("btree_loop", "page %d revisited or too deep" % page_no, "root %d" % root)
                continue
            seen.add(page_no)
            try:
                data, h = self._header(page_no)
            except Exception as e:
                self._issue("bad_page", str(e), "page %d" % page_no)
                continue
            ptrs = self._offsets(data, h)
            if h.type == INDEX_LEAF:
                for off in ptrs:
                    try:
                        yield self._index_cell(data, page_no, off, False)
                    except Exception as e:
                        self._issue("bad_cell", str(e), "page %d offset %d" % (page_no, off))
            elif h.type == INDEX_INTERIOR:
                stack.append(("page", h.right_child, depth + 1))
                for off in reversed(ptrs):
                    if off + 4 > self.usable:
                        continue
                    stack.append(("cell", page_no, off))
                    stack.append(("page", struct.unpack_from(">I", data, off)[0], depth + 1))
            else:
                self._issue("bad_page", "table page 0x%02x inside index tree" % h.type,
                            "page %d" % page_no)

    def segments(self, root, index_tree):
        """Ordered row segments for fast paging without decoding records.

        Returns a list of (page_no, cell_index_or_None, count): a leaf page contributes
        (page, None, cell_count); an index-interior cell contributes (page, i, 1).
        """
        out, stack, seen = [], [("page", root, 0)], set()
        while stack:
            kind, page_no, arg = stack.pop()
            if kind == "cell":
                out.append((page_no, arg, 1))
                continue
            if page_no in seen or arg > self.max_depth:
                continue
            seen.add(page_no)
            try:
                data, h = self._header(page_no)
            except Exception as e:
                self._issue("bad_page", str(e), "page %d" % page_no)
                continue
            expected = (INDEX_INTERIOR, INDEX_LEAF) if index_tree else (TABLE_INTERIOR, TABLE_LEAF)
            if h.type not in expected:
                self._issue("bad_page", "%s page 0x%02x inside %s tree"
                            % ("table" if index_tree else "index", h.type,
                               "index" if index_tree else "table"), "page %d" % page_no)
                continue
            ptrs = self._offsets(data, h)
            if h.type in (TABLE_LEAF, INDEX_LEAF):
                out.append((page_no, None, len(ptrs)))
                continue
            stack.append(("page", h.right_child, arg + 1))
            for i in range(len(ptrs) - 1, -1, -1):
                off = ptrs[i]
                if off + 4 > self.usable:
                    continue
                if index_tree:
                    stack.append(("cell", page_no, i))
                stack.append(("page", struct.unpack_from(">I", data, off)[0], arg + 1))
        return out

    def read_segment(self, page_no, cell_index, index_tree, start=0, stop=None):
        """Decode rows of one segment. Yields (rowid_or_None, payload, CellRef)."""
        data, h = self._header(page_no)
        ptrs = self._offsets(data, h)
        if cell_index is not None:
            try:
                payload, ref = self._index_cell(data, page_no, ptrs[cell_index], True)
                yield None, payload, ref
            except Exception as e:
                self._issue("bad_cell", str(e), "page %d cell %d" % (page_no, cell_index))
            return
        for off in ptrs[start:stop]:
            try:
                if index_tree:
                    payload, ref = self._index_cell(data, page_no, off, False)
                    yield None, payload, ref
                else:
                    yield self._table_leaf_cell(data, page_no, off)
            except Exception as e:
                self._issue("bad_cell", str(e), "page %d offset %d" % (page_no, off))

    def read_cell(self, page_no, offset):
        """Decode the single cell at (page_no, offset). Returns (rowid_or_None, payload, CellRef)."""
        data, h = self._header(page_no)
        if h.type == TABLE_LEAF:
            return self._table_leaf_cell(data, page_no, offset)
        if h.type in (INDEX_LEAF, INDEX_INTERIOR):
            payload, ref = self._index_cell(data, page_no, offset, h.type == INDEX_INTERIOR)
            return None, payload, ref
        raise RecordError("page %d holds no row cells (type 0x%02x)" % (page_no, h.type))

    def find_rowid(self, root, rowid):
        """Descend a table b-tree to the cell with this rowid. Returns (rowid, payload, CellRef) or None."""
        page_no = root
        for _ in range(self.max_depth + 1):
            data, h = self._header(page_no)
            ptrs = self._offsets(data, h)
            if h.type == TABLE_LEAF:
                for off in ptrs:
                    _, p = read_varint(data, off)
                    rid, _ = read_varint(data, p)
                    if to_signed64(rid) == rowid:
                        return self._table_leaf_cell(data, page_no, off)
                return None
            if h.type != TABLE_INTERIOR:
                return None
            nxt = h.right_child
            for off in ptrs:
                key, _ = read_varint(data, off + 4)
                if rowid <= to_signed64(key):
                    nxt = struct.unpack_from(">I", data, off)[0]
                    break
            page_no = nxt
        return None

    def seek_index(self, root, compare, depth=0):
        """Yield (payload, CellRef) for every entry of an index b-tree (ascending keys) whose
        key compare(payload) says is the one sought (0), reading only the pages that can hold
        it: compare returns < 0 for an entry before the key sought and > 0 after it."""
        if depth > self.max_depth:
            self._issue("btree_loop", "index too deep", "root %d" % root)
            return
        try:
            data, h = self._header(root)
        except Exception as e:
            self._issue("bad_page", str(e), "page %d" % root)
            return
        ptrs = self._offsets(data, h)
        if h.type == INDEX_LEAF:
            for off in ptrs:
                payload, ref = self._index_cell(data, root, off, False)
                c = compare(payload)
                if c == 0:
                    yield payload, ref
                elif c > 0:
                    return
            return
        if h.type != INDEX_INTERIOR:
            self._issue("bad_page", "table page 0x%02x inside index tree" % h.type,
                        "page %d" % root)
            return
        prev = -1                   # before the first cell: every key is smaller
        for off in ptrs:
            if off + 4 > self.usable:
                continue
            payload, ref = self._index_cell(data, root, off, True)
            c = compare(payload)
            if prev <= 0 <= c:      # the left child holds keys from the previous cell's to this
                for hit in self.seek_index(struct.unpack_from(">I", data, off)[0], compare,
                                           depth + 1):
                    yield hit
            if c == 0:
                yield payload, ref
            elif c > 0:
                return
            prev = c
        for hit in self.seek_index(h.right_child, compare, depth + 1):
            yield hit

    def tree_pages(self, root):
        """All interior + leaf page numbers of a tree (no overflow pages)."""
        pages, stack = set(), [(root, 0)]
        while stack:
            page_no, depth = stack.pop()
            if page_no in pages or depth > self.max_depth:
                continue
            pages.add(page_no)
            try:
                data, h = self._header(page_no)
            except Exception as e:
                self._issue("bad_page", str(e), "page %d" % page_no)
                continue
            if h.right_child is None:
                continue
            for off in self._offsets(data, h):
                if off + 4 <= self.usable:
                    stack.append((struct.unpack_from(">I", data, off)[0], depth + 1))
            stack.append((h.right_child, depth + 1))
        return pages
