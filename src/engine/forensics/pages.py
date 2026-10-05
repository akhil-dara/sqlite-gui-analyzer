"""Read-only page views over the evidence, and who owns which page.

A view is a Pager look-alike (page(n), page_count, page_size, usable_size, encoding, header)
that BTreeReader, SchemaModel.read_master and NativeTable accept. Views never open files of
their own: main-file pages come from the session pager's read-only map, WAL pages from the
session's WalFile, journal pages from the Journal reader.

  MainFileView     the main file exactly as stored (no WAL frame applied)
  WalSnapshotView  the database as of the end of one WAL commit group
  AsOfFrameView    the database as SQLite would have seen it right after one WAL frame
  OverlayView      another view with some pages replaced (journal pre-state, one frame copy)
"""

import bisect
import struct
from collections import OrderedDict

from ..fileformat.btree import BTreeReader, BTREE_TYPES, parse_page_header
from ..fileformat.freelist import freelist_pages
from ..fileformat.header import DbHeader, HEADER_SIZE
from ..fileformat.pager import PageError
from ..issues import IssueLog

MASTER = "sqlite_master"


class _View(object):
    page_size = usable_size = page_count = 0
    encoding = "utf-8"
    header = None
    label = ""

    def page(self, n):
        raise NotImplementedError

    def safe_page(self, n):
        """page(n), or None when the page does not exist or cannot be read."""
        try:
            return self.page(n)
        except Exception:
            return None


class MainFileView(_View):
    """The main database file as stored. page_count follows the main file's own header;
    file_pages counts every whole or partial page the file holds (it may be larger)."""

    label = "main file"

    def __init__(self, pager, cache_pages=512):
        self._pager = pager
        self.page_size, self.usable_size = pager.page_size, pager.usable_size
        self.encoding = pager.encoding
        self.header = pager.header
        size = pager.main_size
        self.file_pages = (size + self.page_size - 1) // self.page_size if size else 0
        self.page_count = self.file_pages
        if size >= HEADER_SIZE:
            try:
                h = DbHeader.parse(pager.main_page(1)[:HEADER_SIZE])
                if h.page_size == self.page_size:
                    self.header = h
                    self.page_count = clamp_page_count(h.page_count(size), self.file_pages)
            except Exception:
                pass
        self._cache = OrderedDict()
        self._cache_pages = cache_pages

    def page(self, n):
        if n < 1 or n > max(self.page_count, self.file_pages):
            raise PageError("page %d outside the main file (1..%d)"
                            % (n, max(self.page_count, self.file_pages)))
        data = self._cache.get(n)
        if data is not None:
            self._cache.move_to_end(n)
            return data
        data = self._pager.main_page(n)
        self._cache[n] = data
        if len(self._cache) > self._cache_pages:
            self._cache.popitem(last=False)
        return data


class OverlayView(_View):
    """`base` with the pages in `pages` ({page_no: bytes}) replaced."""

    def __init__(self, base, pages, page_count=None, label="", header=None):
        self._base, self._pages = base, pages
        self.page_size, self.usable_size = base.page_size, base.usable_size
        self.encoding = base.encoding
        self.page_count = page_count if page_count else base.page_count
        self.label = label
        self.header = header or base.header
        if header is None and 1 in pages:
            try:
                h = DbHeader.parse(pages[1][:HEADER_SIZE])
                if h.page_size == self.page_size:
                    self.header = h
            except Exception:
                pass

    def page(self, n):
        data = self._pages.get(n)
        if data is not None:
            if n < 1 or n > self.page_count:
                raise PageError("page %d outside 1..%d" % (n, self.page_count))
            return data
        if n > self.page_count:
            raise PageError("page %d outside 1..%d" % (n, self.page_count))
        return self._base.page(n)


class WalTimeline(object):
    """Index of the WAL's frames by page, for time-travel views.

    Frames of the current generation (salts equal to the header's) are kept in order;
    commit_ends[g] is the index of the frame that commits group g.
    """

    def __init__(self, wal):
        self.wal = wal
        self.by_page = {}           # page -> [frame index, ...] (current generation, any state)
        self.commit_ends = []
        self.generation = {}        # (salt1, salt2) -> {page: [frame index, ...]}
        if wal is None:
            return
        for fr in wal.frames:
            self.generation.setdefault((fr.salt1, fr.salt2), {}).setdefault(
                fr.page_no, []).append(fr.index)
            if fr.salt_match:
                self.by_page.setdefault(fr.page_no, []).append(fr.index)
            if fr.commit_group is not None and fr.is_commit:
                self.commit_ends.append(fr.index)

    def frame_at(self, page_no, upto, salts=None):
        """Index of the newest frame for page_no with index <= upto (of the given salt
        generation, default the current one), or None."""
        seq = self.by_page.get(page_no) if salts is None else \
            self.generation.get(salts, {}).get(page_no)
        if not seq:
            return None
        i = bisect.bisect_right(seq, upto)
        return seq[i - 1] if i else None


class WalSnapshotView(_View):
    """The database as it was committed at the end of WAL commit group `group`."""

    def __init__(self, main, timeline, group):
        self._main, self._tl = main, timeline
        self.page_size, self.usable_size = main.page_size, main.usable_size
        self.encoding = main.encoding
        self.group = group
        self.end = timeline.commit_ends[group]
        existing = max(main.page_count, getattr(main, "file_pages", 0),
                       _max_page(timeline, (timeline.wal.header.salt1, timeline.wal.header.salt2)))
        self.page_count = clamp_page_count(timeline.wal.frames[self.end].db_size, existing)
        self.label = "WAL commit %d" % group
        self.header = main.header
        first = self.frame_for(1)
        if first is not None:
            try:
                self.header = DbHeader.parse(timeline.wal.page_data(first)[:HEADER_SIZE])
            except Exception:
                pass

    def frame_for(self, n):
        return self._tl.frame_at(n, self.end)

    def page(self, n):
        if n < 1 or n > self.page_count:
            raise PageError("page %d outside 1..%d" % (n, self.page_count))
        idx = self.frame_for(n)
        if idx is not None:
            return self._tl.wal.page_data(idx)
        return self._main.page(n)


class AsOfFrameView(_View):
    """Pages as they stood right after WAL frame `index` was written: the newest frame of the
    same salt generation at or before it, else the main file. Used to follow the overflow
    chain of a record found in that frame."""

    def __init__(self, main, timeline, index):
        self._main, self._tl = main, timeline
        fr = timeline.wal.frames[index]
        self._salts = (fr.salt1, fr.salt2)
        self.index = index
        self.page_size, self.usable_size = main.page_size, main.usable_size
        self.encoding, self.header = main.encoding, main.header
        existing = max(main.page_count, getattr(main, "file_pages", 0),
                       _max_page(timeline, self._salts))
        self.page_count = max(existing, clamp_page_count(fr.db_size or 0, existing))
        self.label = "WAL frame %d" % index

    def page(self, n):
        if n < 1 or n > self.page_count:
            raise PageError("page %d outside 1..%d" % (n, self.page_count))
        idx = self._tl.frame_at(n, self.index, self._salts)
        if idx is not None:
            return self._tl.wal.page_data(idx)
        return self._main.page(n)


def _max_page(timeline, salts):
    pages = timeline.generation.get(salts, {})
    return max(pages) if pages else 0


def is_btree_page(data, page_no, usable):
    """True when the page header is a plausible b-tree header (type, counts, content start)."""
    try:
        h = parse_page_header(data, page_no)
    except Exception:
        return False
    if h.type not in BTREE_TYPES:
        return False
    ptr_end = h.ptr_start + 2 * h.cell_count
    if ptr_end > usable or h.content_start > usable:
        return False
    if h.first_freeblock and not ptr_end <= h.first_freeblock <= usable - 4:
        return False
    return not (h.cell_count and h.content_start < ptr_end)


class PtrmapPages(object):
    """The pointer-map page numbers of an auto-vacuum database, as a set-like predicate
    (n in pages, len(pages), iteration in page order): nothing is stored per page, so a
    forged page count costs nothing."""

    def __init__(self, page_count, per):
        self.page_count, self.step = max(0, page_count), per + 1

    def __contains__(self, n):
        return isinstance(n, int) and 2 <= n <= self.page_count and (n - 2) % self.step == 0

    def __len__(self):
        return 0 if self.page_count < 2 else (self.page_count - 2) // self.step + 1

    def __bool__(self):
        return len(self) > 0

    __nonzero__ = __bool__

    def __iter__(self):
        n = 2
        while n <= self.page_count:
            yield n
            n += self.step


def ptrmap_pages(header, page_count, usable):
    """Pointer-map page numbers of an auto-vacuum database (empty otherwise)."""
    if header is None or not header.largest_root:
        return PtrmapPages(0, 1)
    return PtrmapPages(page_count, usable // 5)


def clamp_page_count(declared, existing):
    """A page count declared by the file (header, WAL commit, journal), at most the pages
    that exist plus a little slack for a file cut short (1 %%, at least 16)."""
    existing = max(1, existing)
    allowed = existing + max(16, existing // 100)
    return min(declared, allowed) if declared else existing


class PageMap(object):
    """Which tree, freelist or pointer map each page of one view belongs to.

    owner[page] = (name, kind) with kind 'table' | 'index' | 'master'. Trees are walked through
    their interior pages only; overflow pages of live cells are not listed.
    """

    def __init__(self, view, entries, issues=None, cancel=None):
        self.view = view
        self.issues = issues if issues is not None else IssueLog()
        self.owner = {}
        self.roots = {}
        self.complete = True
        reader = BTreeReader(view, self.issues)
        todo = [(MASTER, "master", 1)]
        for e in entries:
            if e.type in ("table", "index") and isinstance(e.rootpage, int) and e.rootpage > 0:
                todo.append((e.name, "index" if e.type == "index" else "table", e.rootpage))
        for name, kind, root in todo:
            if cancel is not None and cancel():
                self.complete = False
                break
            self.roots.setdefault(root, (name, kind))
            try:
                pages = reader.tree_pages(root)
            except Exception as e:
                self.issues.add("forensics_tree", str(e), "%s root %d" % (name, root), "info")
                continue
            for p in pages:
                self.owner.setdefault(p, (name, kind))
        try:
            trunks, leaves = freelist_pages(view, self.issues)
        except Exception as e:
            self.issues.add("forensics_freelist", str(e), getattr(view, "label", ""), "info")
            trunks, leaves = [], []
        self.trunks, self.leaves = set(trunks), set(leaves)
        self.trunk_order = list(trunks)
        self.ptrmap = ptrmap_pages(view.header, view.page_count, view.usable_size)

    def status(self, n):
        """'tree' | 'trunk' | 'freelist' | 'ptrmap' | 'other'."""
        if n in self.owner:
            return "tree"
        if n in self.trunks:
            return "trunk"
        if n in self.leaves:
            return "freelist"
        if n in self.ptrmap:
            return "ptrmap"
        return "other"

    def trunk_leaf_count(self, n):
        """Leaf pointers a freelist trunk page holds (its first 8 + 4*count bytes are
        freelist bookkeeping, the rest is whatever the page held before)."""
        try:
            data = self.view.page(n)
        except Exception:
            return 0
        count = struct.unpack_from(">I", data, 4)[0]
        return min(count, self.view.usable_size // 4 - 2)
