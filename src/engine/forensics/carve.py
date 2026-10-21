"""Carving deleted records from every place SQLite leaves them.

Where records are looked for:
  * live b-tree pages: freeblocks (freed cells, first 4 bytes overwritten) and the
    unallocated gap between the cell pointer array and the cell content area (a cell freed at
    the start of the content area stays intact there; an emptied page keeps all of them);
  * freelist trunk and leaf pages, and b-tree pages nothing references (orphans);
  * main-file pages the WAL replaces, every WAL frame's page copy and every journal page
    image: their cells are older versions of the pages (source 'replaced' / 'wal' /
    'journal'), and their freeblocks and gaps are carved too.

How a candidate is accepted: see cellparse (the header parses, sizes add up exactly, the cell
fits its space) and templates (every value is one SQLite could have stored in the table).
Inside free space, cell boundaries come from intact cells and from the (possibly stale)
freeblock headers SQLite wrote over each freed cell: a freeblock header records the size of
the free run it started, so the end of a rebuilt cell is taken from those boundaries.
Candidates identical to a live row are dropped (copies left behind by page splits and moves);
a live key with other values is kept and flagged 'prior_version'. Identical records found in
several places become one Record whose `copies` lists the other places.

Confidence:
  high    header intact, sizes exact, every value typical for its column, one table fits
          (or the page's owner), payload complete;
  medium  first bytes rebuilt from the schema (freeblock), or unusual values, or several
          tables fit, or a lost value that can only be narrowed down (NULL/0/1);
  low     no table fits, several readings fit, payload partial (broken overflow chain), or
          the cell's end had to be taken from a weaker boundary.
Work is bounded: cancel(), a deadline and a record cap stop the scan (the result then says
it is incomplete); a damaged page is logged and skipped, never raised.
"""

import bisect
import hashlib
import re
import struct
import time

from ..fileformat.btree import (INDEX_INTERIOR, INDEX_LEAF, TABLE_LEAF, local_payload_size,
                                parse_page_header)
from ..fileformat.record import InvalidText, decode_value, serial_size
from ..fileformat.wal import CURRENT
from .. import uiyield
from ..issues import IssueLog
from .cellparse import parse_intact, reconstruct_all, self_ends, stale_header, varint
from .pages import AsOfFrameView, is_btree_page
from .provenance import (FREEBLOCK, FREELIST, HIGH, JOURNAL, JOURNAL_FILE, LOW, MAIN_FILE,
                         MEDIUM, ORPHAN, REPLACED, UNALLOCATED, WAL, WAL_FILE, CARVE_SOURCES,
                         Provenance, Record, value_key, worst)
from .templates import BLOB, TEXT, printable, type_class

TABLE_KIND, INDEX_KIND, IINT_KIND = "table", "index", "index-interior"
RAW_KINDS = (TABLE_KIND, INDEX_KIND)
MAX_RECORDS = 500000            # the default of the limit 'carve_max_records'
MAX_PAYLOAD = 256 << 20
MAX_END_TRIES = 48          # boundaries looked at after a rebuilt cell's start ...
MAX_STRONG_ENDS = 16        # ... of which at most this many strong ones are tried
MAX_WEAK_ENDS = 8           # ... and this many weak ones
PARTIAL_MIN = 16             # characters of text a partial record must keep
MAX_REBUILT_READINGS = 256  # readings of one rebuilt cell weighed against each other
FREEBLOCK_OWN_ONLY = True   # a live page's freeblocks are read as its own table's records only
NOINFO_NOISE = True         # a rebuilt reading whose values say nothing (NULL, 0, empty, all-zero
                            # bytes) is a header read over zeros, not a row
FOREIGN_ODD_NOISE = True    # a rebuilt reading as another table than the page's, with a blob /
                            # damaged text where it declares text or numbers: noise
_NONZERO = re.compile(b"[^\x00]")
_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")
_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}


class CarveResult(list):
    """List of Records plus `stats` (dict) and `complete` (False when cancelled, out of time
    or capped)."""

    def __init__(self, records=(), stats=None, complete=True):
        list.__init__(self, records)
        self.stats = stats or {}
        self.complete = complete


class _Site(object):
    """Where the page image being carved comes from."""
    __slots__ = ("file", "page", "frame", "frame_state", "commit_group", "owner", "view",
                 "pmap", "intact_source", "area_source", "records")

    def __init__(self, file, page, owner=None, view=None, pmap=None, intact_source=None,
                 area_source=None, frame=None, frame_state=None, commit_group=None):
        self.file, self.page, self.owner, self.view, self.pmap = file, page, owner, view, pmap
        self.intact_source, self.area_source = intact_source, area_source
        self.frame, self.frame_state, self.commit_group = frame, frame_state, commit_group
        self.records = []

    def prov(self, source, cell, chain):
        return Provenance(source, self.file, self.page, cell.start, cell.end - cell.start,
                          self.frame, self.frame_state, self.commit_group, chain)


class _Cand(object):
    __slots__ = ("tpl", "cell", "row", "rowid", "atypical", "notes", "flags", "chain",
                 "complete", "score", "uncertain", "conf")

    def clone(self, cell):
        c = _Cand()
        for k in self.__slots__:
            setattr(c, k, getattr(self, k))
        c.cell, c.notes, c.flags = cell, list(self.notes), set(self.flags)
        return c


class Carver(object):
    stage_label = "Reading the pages"     # the step the window shows while this runs

    def __init__(self, fx, templates, sources=None, cancel=None, progress=None, deadline=None,
                 max_records=MAX_RECORDS, include_schema=False, unattributed=True,
                 page_filter=None, page_hints=None, free_filter=None):
        """page_filter(file, page_no, status) -> bool limits the pages carved (status is the
        main-file page's role: 'tree', 'freelist', 'trunk', 'ptrmap', 'other', 'beyond';
        None for WAL and journal pages). page_hints {page: table} names the table a free or
        unreferenced page last belonged to (e.g. the pages of a dropped table).
        free_filter(page_bytes) -> bool skips freelist / unreferenced pages not worth a scan."""
        self.fx = fx
        self.page_filter = page_filter
        self.free_filter = free_filter
        self.hints = page_hints or {}
        self.max_cols = max([t.n for t in templates] or [1]) + (8 if unattributed else 0)
        self.usable = fx.session.pager.usable_size
        self.encoding = fx.session.pager.encoding
        self.by_n = {}                  # intact cells: every template, so schema rows are known
        for t in templates:
            self.by_n.setdefault((t.n, t.index_tree), []).append(t)
        if not include_schema:          # rebuilt cells: schema rows are not wanted here
            templates = [t for t in templates if t.kind != "master"]
        self.templates = templates
        self.sources = set(sources or CARVE_SOURCES)
        self.cancel, self.progress, self.deadline = cancel, progress, deadline
        self.max_records = max_records
        self.include_schema, self.unattributed = include_schema, unattributed
        self.issues = IssueLog()
        self.records = []
        self._identity = {}
        self._loose = {}
        self._seen_cells = {}
        self._images = {}
        self.stats = {"pages": 0, "frames": 0, "journal_pages": 0, "raw_pages": 0,
                      "candidates": 0, "live_skipped": 0, "merged": 0, "errors": 0}
        self.stopped = None

    # -- driver ------------------------------------------------------------
    def _stop(self):
        if self.stopped:
            return True
        if self.cancel is not None and self.cancel():
            self.stopped = "cancelled"
        elif self.deadline is not None and time.time() > self.deadline:
            self.stopped = "time limit reached"
        elif len(self.records) >= self.max_records:
            self.stopped = "record limit reached"
        return bool(self.stopped)

    def run(self):
        t0 = time.time()
        fx = self.fx
        units = [("main", n) for n in range(1, fx.main.file_pages + 1)]
        wal = fx.session.wal
        if wal is not None:
            units.extend(("wal", fr.index) for fr in wal.frames)
        journal = fx.journal()
        if journal is not None and journal.page_size == fx.main.page_size:
            units.extend(("journal", r) for r in journal.records)
        deferred = []
        total = len(units)
        for i, (kind, arg) in enumerate(units):
            if self._stop():
                break
            uiyield.pause()             # a recovery in the background gives way to the window
            if i % 64 == 0:
                if self.progress is not None:
                    self.progress(i, total)
                set_stage = getattr(fx, "set_stage", None)
                if set_stage is not None:
                    set_stage(self.stage_label, i, total)
            try:
                if kind == "main":
                    self._main_page(arg, deferred)
                elif kind == "wal":
                    self._wal_frame(arg)
                else:
                    self._journal_record(journal, arg)
            except Exception as e:          # a hostile page must not stop the carve
                self._failed(e, "%s %s" % (kind, getattr(arg, "page_no", arg)))
        for job in deferred:
            if self._stop():
                break
            uiyield.pause()
            self.stats["raw_pages"] += 1
            try:
                job()
            except Exception as e:
                self._failed(e, "raw page")
        if self.progress is not None:
            self.progress(total, total)
        self._finish_copies()
        self._infer_rowids_by_page()
        self.stats["records"] = len(self.records)
        self.stats["seconds"] = round(time.time() - t0, 3)
        self.stats["stopped"] = self.stopped
        return CarveResult(self.records, self.stats, not self.stopped)

    GAP_SPAN = 100000           # widest rowid range of a page looked through for the gap

    def _infer_rowids_by_page(self):
        """A freed cell loses its rowid with its first 4 bytes. When it is the only such
        record of a live table leaf and exactly one rowid is missing between the page's live
        rows (and the rowids of the other records recovered there), that rowid was its own:
        a b-tree leaf holds one contiguous range of rowids. Said, never certain."""
        from ..fileformat.btree import cell_pointers, parse_page_header
        groups = {}
        for r in self.records:
            if "rowid_unknown" in r.flags and r.prov.file == MAIN_FILE and \
                    r.prov.source == FREEBLOCK and r.table:
                groups.setdefault((r.prov.page, r.table), []).append(r)
        for (page, table), recs in groups.items():
            if len(recs) != 1:
                continue                # two lost rowids on a page: which is which is unknown
            rec = recs[0]
            try:
                owner = self.fx.eff_map.owner.get(page)
                info = self.fx.session.schema.get(table)
                if owner is None or owner[0] != table or info is None or info.without_rowid:
                    continue
                data = self.fx.main.page(page)
                h = parse_page_header(data, page)
                if h.type != TABLE_LEAF:
                    continue
                live = set()
                for ptr in cell_pointers(data, h, self.usable):
                    a = varint(data, ptr, self.usable)
                    b = varint(data, a[1], self.usable) if a is not None else None
                    if b is not None:
                        live.add(b[0] - (1 << 64) if b[0] >= (1 << 63) else b[0])
            except Exception:           # noqa: BLE001 - a page that does not read: no guess
                continue
            if len(live) < 2 or max(live) - min(live) > self.GAP_SPAN:
                continue
            known = set(r.rowid for r in self.records if r.rowid is not None and
                        r.table == table and r.prov.page == page and r.prov.file == MAIN_FILE)
            gaps = [k for k in range(min(live), max(live) + 1)
                    if k not in live and k not in known]
            if len(gaps) != 1:
                continue
            rid = gaps[0]
            try:                        # a row moved to another page (a split) is no gap
                if self.fx.live._reader.find_rowid(info.root_page, rid) is not None:
                    continue
            except Exception:           # noqa: BLE001 - cannot tell: no guess
                continue
            rec.rowid = rid
            alias = info.rowid_alias
            if alias is not None and alias < len(rec.values) and rec.values[alias] is None:
                rec.values[alias] = rid
            rec.flags.discard("rowid_unknown")
            rec.flags.add("rowid_inferred")
            rec.reasons.append("rowid lost with the cell's first bytes; inferred as %d: the only "
                               "rowid missing between the live rows of its page (%d to %d)"
                               % (rid, min(live), max(live)))
            self.stats["rowids_inferred"] = self.stats.get("rowids_inferred", 0) + 1

    def _failed(self, e, where):
        self.stats["errors"] += 1
        self.issues.add("carve_failed", "%s: %s" % (type(e).__name__, e), where, "info")

    # -- units -------------------------------------------------------------
    def _main_page(self, n, deferred):
        fx = self.fx
        if n == fx.lock_byte_page:
            return
        wal = fx.session.wal
        replaced = wal is not None and n in wal.overlay
        pmap = fx.main_map if replaced else fx.eff_map
        status = pmap.status(n) if n <= pmap.view.page_count else "beyond"
        if self.page_filter is not None and not self.page_filter("main", n, status):
            return
        self.stats["pages"] += 1
        data = fx.main.page(n)
        if status not in ("tree", "ptrmap") and self.free_filter is not None \
                and not self.free_filter(data):
            return
        src = self.sources
        usable = self.usable
        if status == "tree":
            owner = pmap.owner[n]
            if owner[1] == "index" or not is_btree_page(data, n, usable):
                return
            site = _Site(MAIN_FILE, n, owner[0], fx.main, pmap,
                         intact_source=REPLACED if replaced else None)
            self._btree(data, site, replaced and REPLACED in src)
        elif status == "freelist":
            if FREELIST not in src:
                return
            site = _Site(MAIN_FILE, n, self.hints.get(n), fx.main, pmap, FREELIST, FREELIST)
            if is_btree_page(data, n, usable):
                self._btree(data, site, True)
            elif self._overflow_filled(data):
                self.stats["overflow_pages_skipped"] = self.stats.get("overflow_pages_skipped",
                                                                      0) + 1
            elif _NONZERO.search(data, 0, usable):
                deferred.append(lambda: self._area(data, 0, usable, False, RAW_KINDS, site,
                                                   raw=True))
        elif status == "trunk":
            if FREELIST not in src:
                return
            site = _Site(MAIN_FILE, n, self.hints.get(n), fx.main, pmap, FREELIST, FREELIST)
            self._area(data, 8 + 4 * pmap.trunk_leaf_count(n), usable, False, RAW_KINDS, site)
        elif status in ("other", "beyond"):
            if ORPHAN not in src:
                return
            site = _Site(MAIN_FILE, n, self.hints.get(n), fx.main, pmap, ORPHAN, ORPHAN)
            if is_btree_page(data, n, usable):
                self._btree(data, site, True)
            elif status == "beyond" and _NONZERO.search(data, 0, usable):
                deferred.append(lambda: self._area(data, 0, usable, False, RAW_KINDS, site,
                                                   raw=True))

    def _overflow_filled(self, data):
        """A page that was an overflow page other than the last of its chain: it starts with
        the next page's number and every other byte is payload content, so it cannot hold
        records (the last page of a chain can: its unused tail keeps older bytes)."""
        nxt = _U32.unpack_from(data, 0)[0]
        return 0 < nxt <= max(self.fx.main.file_pages, self.fx.session.pager.page_count)

    def _wal_frame(self, index):
        fx = self.fx
        wal = fx.session.wal
        fr = wal.frames[index]
        n = fr.page_no
        if n < 1 or (self.page_filter is not None and not self.page_filter("wal", n, None)):
            return
        data = wal.page_data(index)
        self.stats["frames"] += 1
        want_intact = fr.state != CURRENT and WAL in self.sources
        owner = fx.eff_map.owner.get(n) or fx.main_map.owner.get(n)
        site = _Site(WAL_FILE, n, owner[0] if owner else None, None, None,
                     intact_source=WAL, frame=index, frame_state=fr.state,
                     commit_group=fr.commit_group)
        if self._duplicate(data, want_intact, site):
            return
        site.view = AsOfFrameView(fx.main, fx.timeline, index)
        if is_btree_page(data, n, self.usable):
            if owner is None or owner[1] != "index":
                self._btree(data, site, want_intact)
        elif (n in fx.eff_map.trunks or n in fx.main_map.trunks) and FREELIST in self.sources:
            site.intact_source = site.area_source = FREELIST
            count = min(_U32.unpack_from(data, 4)[0], self.usable // 4 - 2)
            self._area(data, 8 + 4 * count, self.usable, False, RAW_KINDS, site)

    def _journal_record(self, journal, rec):
        fx = self.fx
        n = rec.page_no
        if self.page_filter is not None and not self.page_filter("journal", n, None):
            return
        data = journal.record_page(rec)
        self.stats["journal_pages"] += 1
        want_intact = JOURNAL in self.sources
        owner = fx.eff_map.owner.get(n) or fx.main_map.owner.get(n)
        site = _Site(JOURNAL_FILE, n, owner[0] if owner else None, fx.journal_view(), None,
                     intact_source=JOURNAL)
        if self._duplicate(data, want_intact, site):
            return
        if is_btree_page(data, n, self.usable) and not (owner and owner[1] == "index"):
            self._btree(data, site, want_intact)

    def _duplicate(self, data, want_intact, site):
        """True when the identical page image was carved already (its records then list this
        location as a copy)."""
        key = (hash(data), want_intact, site.page)
        first = self._images.get(key)
        if first is None:
            self._images[key] = [site]
            return False
        first.append(site)
        return True

    def _finish_copies(self):
        for sites in self._images.values():
            first = sites[0]
            for other in sites[1:]:
                for rec in first.records:
                    p = rec.prov if rec.prov.file == first.file and rec.prov.page == first.page \
                        else None
                    if p is None:
                        continue
                    rec.copies.append(Provenance(p.source, other.file, other.page, p.offset,
                                                 p.length, other.frame, other.frame_state,
                                                 other.commit_group, p.overflow))

    # -- page structure ----------------------------------------------------
    def _btree(self, data, site, want_intact):
        usable = self.usable
        h = parse_page_header(data, site.page)
        kinds = {TABLE_LEAF: (TABLE_KIND,), INDEX_LEAF: (INDEX_KIND,),
                 INDEX_INTERIOR: (IINT_KIND,)}.get(h.type)
        ptr_end = h.ptr_start + 2 * h.cell_count
        if ptr_end > usable:
            self._area(data, h.ptr_start, usable, False, RAW_KINDS, site, raw=True)
            return
        if want_intact and kinds:
            same = self._current_cells(site) if site.intact_source == REPLACED else None
            for i in range(h.cell_count):
                off = _U16.unpack_from(data, h.ptr_start + 2 * i)[0]
                if ptr_end <= off <= usable - 4:
                    cells = self._intact_cells(data, off, usable, kinds)
                    if cells and same is not None and cells[0].ovfl is None:
                        # the very same cell is on the current version of the page (a WAL
                        # frame replaced it): a live row, not decoded again
                        b = bytes(data[cells[0].start:cells[0].end])
                        if same[0].find(b) in same[1]:
                            self.stats["live_skipped"] += 1
                            continue
                    if cells:
                        self._take_intact(data, off, cells, site, site.intact_source)
        # on freelist / orphan pages everything counts as that page's source; elsewhere the
        # free areas are the 'freeblock' and 'unallocated' sources
        whole = site.area_source is not None
        freeblocks = []
        if kinds and (whole or FREEBLOCK in self.sources):
            fb, prev, hops = h.first_freeblock, 0, 0
            while fb and hops < usable // 4:
                hops += 1
                if fb <= prev or fb < ptr_end or fb > usable - 4:
                    self.issues.add("bad_freeblock", "freeblock at %d out of order or range" % fb,
                                    "page %d" % site.page, "info")
                    break
                size = _U16.unpack_from(data, fb + 2)[0]
                if size < 4 or fb + size > usable:
                    self.issues.add("bad_freeblock", "freeblock at %d has size %d" % (fb, size),
                                    "page %d" % site.page, "info")
                    break
                self._area(data, fb, fb + size, True, kinds, site, FREEBLOCK)
                freeblocks.append((fb, fb + size))
                prev, fb = fb, _U16.unpack_from(data, fb)[0]
        gap_end = min(h.content_start, usable)
        if gap_end - ptr_end >= 4 and (whole or UNALLOCATED in self.sources):
            self._area(data, ptr_end, gap_end, False, kinds or RAW_KINDS, site, UNALLOCATED)
        if kinds and (whole or UNALLOCATED in self.sources):
            for a, b in self._dead_space(data, h, ptr_end, gap_end, freeblocks):
                self._area(data, a, b, False, kinds, site, UNALLOCATED)

    def _dead_space(self, data, h, ptr_end, content_start, freeblocks):
        """Parts of the cell content area that no live cell and no (reachable) freeblock
        covers: older cells SQLite left there when it rewrote the page (auto-vacuum moving
        cells, a freeblock chain that lost entries). [] when a live cell's size cannot be
        worked out (nothing is guessed: those bytes may belong to a live cell)."""
        usable = self.usable
        start = max(content_start, ptr_end)
        if usable - start < 4:
            return []
        covered = list(freeblocks)
        for i in range(h.cell_count):
            off = _U16.unpack_from(data, h.ptr_start + 2 * i)[0]
            if not start <= off < usable:
                return []
            end = _cell_end(data, off, usable, h.type)
            if end is None:
                return []
            covered.append((off, end))
        covered.sort()
        out, pos = [], start
        for a, b in covered:
            if a > pos and a - pos >= 4:
                out.append((pos, a))
            pos = max(pos, b)
        if usable - pos >= 4:
            out.append((pos, usable))
        return out

    def _intact_cells(self, data, pos, end, kinds):
        out = []
        usable, cols = self.usable, self.max_cols
        for kind in kinds:
            if kind == IINT_KIND:
                c = parse_intact(data, pos + 4, end, usable, True, cols)
                if c is not None:
                    c.start = pos
                    out.append(c)
            else:
                c = parse_intact(data, pos, end, usable, kind == INDEX_KIND, cols)
                if c is not None:
                    out.append(c)
        return out

    def _area(self, data, start, end, head_clobbered, kinds, site, default_source=None,
              raw=False):
        """Carve records from a region that holds no live cells. raw: the bytes have no page
        structure at all (stricter about what counts as a freed cell)."""
        if end - start < 4:
            return
        body = start + 4 if head_clobbered else start
        if _NONZERO.search(data, body, end) is None:
            return                      # only zero bytes (secure delete wiped it): no record
        source = site.area_source or default_source or UNALLOCATED
        usable = self.usable
        probe = set(m.start() for m in _NONZERO.finditer(data, start, end - 3))
        stale = {}
        for m in _NONZERO.finditer(data, start + 2, end - 1):
            q = m.start()
            for r in (q - 2, q - 3):
                if r >= start and r not in stale:
                    s = stale_header(data, r, end, usable)
                    if s:
                        stale[r] = s
        if head_clobbered:
            probe.add(start)
        probe.update(stale)
        intact = {}
        for r in probe:
            cells = self._intact_cells(data, r, end, kinds)
            if cells:
                intact[r] = cells
        heads = set(stale)
        if head_clobbered:
            heads.add(start)
        order = sorted(set(intact) | heads)
        if not order:
            return
        ends = sorted(set(order) | {end})
        rebuildable = TABLE_KIND in kinds or INDEX_KIND in kinds
        idx = 0
        last_end = start
        while idx < len(order):
            if idx % 32 == 0 and self._stop():
                return
            pos = order[idx]
            took = None
            if pos in intact:
                took = self._take_intact(data, pos, intact[pos], site, source)
            if took is None and rebuildable and pos in heads:
                # an intact cell starting inside these 4 bytes wins over a rebuilt reading
                for q in (pos + 1, pos + 2, pos + 3):
                    if q in intact:
                        took = self._take_intact(data, q, intact[q], site, source)
                        if took is not None:
                            break
                if took is None:
                    took = self._take_rebuilt(data, pos, end, ends, kinds, site, source, stale,
                                              intact, raw, start)
            if took is not None and took > pos:
                last_end = max(last_end, took)
            idx = bisect.bisect_left(order, took) if took is not None and took > pos else idx + 1
        if source == UNALLOCATED and not raw and rebuildable:
            self._partial_tail(data, last_end, end, kinds, site, source, probe)

    def _start_of_live(self, cand):
        """True when a partial record is only the start of the live row with its rowid
        (a cell left behind by a page split, cut): nothing in it the live row lacks."""
        live = self.fx.live.live_row(cand.tpl, cand.rowid) if cand.tpl is not None else None
        if live is None:
            return False
        cut = max((i for i, v in enumerate(cand.row) if v is not None), default=-1)
        for i in range(cut + 1):
            v, lv = cand.row[i], live[i] if i < len(live) else None
            if v is None or v == lv:
                continue
            if i == cut and ((isinstance(v, str) and isinstance(lv, str)) or
                             (isinstance(v, (bytes, bytearray)) and
                              isinstance(lv, (bytes, bytearray)))) and lv.startswith(v):
                continue                # the cut value: text or a BLOB, the live one's start
            return False
        return True

    def _partial_tail(self, data, start, end, kinds, site, source, probe):
        """The last record of an unallocated area whose tail a live cell overwrote (the
        area ends where the cell content begins): its whole header lies in the area, its
        body runs past the end. Only the first such record after the records taken is
        tried, and kept on the partial rule (see _take_partial)."""
        for r in sorted(q for q in probe if q >= start):
            for kind in kinds:
                if kind not in (TABLE_KIND, INDEX_KIND):
                    continue
                index = kind == INDEX_KIND
                cell = parse_intact(data, r, end, self.usable, index, self.max_cols, cut=True)
                if cell is None or cell.payload_len <= cell.local:
                    continue
                cands = []
                tpls = [t for t in self.templates if t.index_tree == index and
                        (not site.owner or t.name == site.owner)]
                for tpl in tpls:
                    for c in self._match(data, cell, site, only=tpl):
                        kept = [v for v in c.row if isinstance(v, str)]
                        if "truncated" in c.flags and _partial_ok(kept):
                            cands.append(c)
                if cands:
                    cands.sort(key=lambda c: c.score)
                    best = cands[0]
                    if self._start_of_live(best):
                        self.stats["live_skipped"] += 1
                        return
                    best.conf = LOW
                    best.flags.add("partial")
                    best.notes.insert(0, "the last record of the free space: a live record "
                                         "now starts where its end was; only what survives "
                                         "is shown")
                    self._accept(best, site, source, data)
                    return

    # -- choosing ----------------------------------------------------------
    def _current_cells(self, site):
        """(current page bytes, its cell offsets) of a main-file page the WAL replaced, or
        None (no current version, or not a b-tree page now)."""
        try:
            # only a page of a live tree now: a page the WAL freed keeps its old cell pointers,
            # and those rows are deleted, not live
            if self.fx.eff_map.status(site.page) != "tree":
                return None
            cur = bytes(self.fx.session.pager.page(site.page))
            h = parse_page_header(cur, site.page)
            if h.ptr_start + 2 * h.cell_count > self.usable:
                return None
            return cur, set(_U16.unpack_from(cur, h.ptr_start + 2 * i)[0]
                            for i in range(h.cell_count))
        except Exception:               # noqa: BLE001 - every cell is then checked as before
            return None

    def _take_intact(self, data, pos, cells, site, source):
        """Accept the best reading of the intact cell(s) at pos. Returns the cell's end offset,
        or None when no reading is acceptable."""
        cands = []
        for cell in cells:
            key = (_cell_digest(data, cell), site.owner, site.file) \
                if cell.ovfl is None else None
            cached = self._seen_cells.get(key) if key is not None else None
            if cached is not None:
                if cached != "skip":
                    self._emit(cached.clone(cell), source, site)
                return cell.end
            cands.extend(self._match(data, cell, site))
        alts = ()
        if not cands:
            best = self._unattributed(data, cells[0], site) if self.unattributed else None
            if best is None:
                return None
        else:
            cands.sort(key=lambda c: c.score)
            best = cands[0]
            alts = [c for c in cands if c.tpl is not best.tpl and c.score[:6] == best.score[:6]]
            others = sorted(set(c.tpl.name for c in alts))
            if others:
                best.notes.append("also fits: %s" % ", ".join(others))
                if best.tpl.name != site.owner:
                    best.flags.add("ambiguous_table")
        best.notes.insert(0, "header intact")
        return self._accept(best, site, source, data, alts=alts)

    def _take_rebuilt(self, data, pos, end, ends, kinds, site, source, stale, intact, raw=False,
                      area_start=None):
        """Rebuild the cell whose first bytes a freeblock header overwrote at pos.

        The cell ends at a boundary after pos. Strong boundaries are tried first, nearest
        first: the end of the free area, the start of an intact cell, the end of the free run
        pos's own header describes, or another header describing the same run (cells freed
        one after the other: every header then ends where the run ends). A reading that ends
        at a weaker boundary is only taken when no column size had to be inferred."""
        own = stale.get(pos)
        run_end = pos + own if own else None
        if raw and pos != area_start:
            # unstructured bytes (a page with no b-tree header): only a header whose free run
            # ends on a boundary is taken as a freed cell; others are chance byte patterns
            i = bisect.bisect_left(ends, run_end) if run_end is not None else len(ends)
            if i >= len(ends) or ends[i] != run_end:
                return None
        strong, weak = [], []
        i = bisect.bisect_left(ends, pos + 4)
        for e in ends[i:i + MAX_END_TRIES]:
            if e == end or e in intact or e == run_end or \
                    (run_end is not None and e in stale and e + stale[e] == run_end):
                if len(strong) < MAX_STRONG_ENDS:
                    strong.append(e)
            elif len(weak) < MAX_WEAK_ENDS:
                weak.append(e)
        if end not in strong and end - pos >= 4:
            strong.append(end)
        caches = {TABLE_KIND: {}, INDEX_KIND: {}}
        dcache = {}

        def end_tries():
            for e in strong:
                yield e, True
            for e in weak:
                yield e, False
            # none fitted: the ends the cell's own header gives (its next neighbour lost its
            # start too, so no boundary ends it) - worked out only now, they cost
            seen = set(strong) | set(weak)
            for kind in kinds:
                if kind == IINT_KIND:
                    continue
                index = kind == INDEX_KIND
                # a freed cell of a page is its table's: that table's column count only (all
                # of them on a page no table owns)
                mine = [t for t in self.templates if t.index_tree == index and
                        t.name == site.owner]
                counts = sorted(set(t.n for t in (mine or [t for t in self.templates
                                                           if t.index_tree == index])))
                for e in self_ends(data, pos, run_end or end, self.usable, index, counts):
                    if e not in seen:       # None: weaker still - the reading set this end
                        seen.add(e)
                        yield e, None
        for e, is_strong in end_tries():
            cands = []
            for kind in kinds:
                if kind == IINT_KIND:
                    continue
                index = kind == INDEX_KIND
                tpls = [t for t in self.templates if t.index_tree == index]
                own = [t for t in tpls if t.name == site.owner]
                # the page's own table first: a freed cell of a page belongs to its table. A
                # freeblock of a live page holds only its own table's cells (freeblocks are
                # made by deletes in the page's tree; a reused page starts without any), so
                # there no other table is tried: one with untyped columns (sqlite_sequence)
                # would read header bytes as blobs at a nearer end
                if own and source == FREEBLOCK and FREEBLOCK_OWN_ONLY:
                    groups = [own]
                else:
                    groups = [own, [t for t in tpls if t.name != site.owner]] if own else [tpls]
                for group in groups:
                    cells = reconstruct_all(data, pos, e, self.usable, index, group,
                                            cache=caches[kind])
                    for tpl, cell in cells[:MAX_REBUILT_READINGS]:
                        if is_strong or not cell.solved:
                            for c in self._match(data, cell, site, only=tpl, dcache=dcache):
                                if is_strong is None and any(
                                        isinstance(v, InvalidText) or
                                        (isinstance(v, str) and not printable(v))
                                        for v in c.row):
                                    continue    # an end of its own and unreadable text: noise
                                if _odd_class(c.tpl, c.row) and (not is_strong or (
                                        FOREIGN_ODD_NOISE and site.owner and
                                        c.tpl.name != site.owner)):
                                    continue    # a guessed end, or another table than the
                                    #             page's, and a blob / damaged text where the
                                    #             column holds text or numbers: noise
                                if NOINFO_NOISE and not _informative(c.row):
                                    continue    # every value NULL / 0 / empty / zero bytes:
                                    #             a header rebuilt over zeros, not a row
                                if is_strong or _intact_content(c.tpl, c.cell, c.row) >= 2:
                                    cands.append(c)
                    if cands:
                        break
            if not cands:
                continue
            if site.owner and any(c.tpl.name == site.owner for c in cands):
                cands = [c for c in cands if c.tpl.name == site.owner]
            cands.sort(key=lambda c: c.score)
            best = cands[0]
            top = [c for c in cands if c.score[:5] == best.score[:5]]
            others = sorted(set(c.tpl.name for c in top if c.tpl is not best.tpl))
            readings = set(tuple(value_key(v) for v in c.row) for c in top if c.tpl is best.tpl)
            best.notes.insert(0, "first %d bytes overwritten by a freeblock header; rebuilt from "
                                 "the schema" % best.cell.rebuilt)
            if not is_strong:
                best.notes.append("end taken from the next record boundary")
                best.flags.add("end_guessed")
            if others:
                best.notes.append("also fits: %s" % ", ".join(others))
                best.flags.add("ambiguous_table")
            if len(readings) > 1:
                cols = best.tpl.columns
                differ = [cols[i] for i in range(len(cols))
                          if len(set(value_key(c.row[i]) if i < len(c.row) else None
                                     for c in top if c.tpl is best.tpl)) > 1]
                best.notes.append("%d readings fit; the most plausible is shown (uncertain: %s)"
                                  % (len(readings), ", ".join(differ)))
                best.flags.add("ambiguous_reading")
                best.flags.add("uncertain_value")
            return self._accept(best, site, source, data,
                                alts=[c for c in top if c.tpl is not best.tpl])
        if source == FREEBLOCK and run_end is not None:
            # the old cell ends where its run ends - or where an intact cell inside the run
            # begins (its space was reused by a cell that was itself freed later, and the
            # freeblocks merged): the start of the old cell survives up to there
            inner = [e for e in intact if pos + 4 < e < run_end]
            if pos + 4 < end < run_end:
                inner.append(end)       # the next cell boundary found in the run
            return self._take_partial(data, pos, min(inner) if inner else run_end, kinds,
                                      site, source)
        return None

    def _take_partial(self, data, pos, run_end, kinds, site, source):
        """The start of a freed cell whose tail a newer cell overwrote (an update to a
        shorter value): kept only when the value it cuts is printable text with at least
        PARTIAL_MIN characters left, flagged 'partial', low confidence."""
        cands = []
        for kind in kinds:
            if kind == IINT_KIND:
                continue
            index = kind == INDEX_KIND
            tpls = [t for t in self.templates if t.index_tree == index and
                    (not site.owner or t.name == site.owner)]
            for tpl, cell in reconstruct_all(data, pos, run_end, self.usable, index, tpls,
                                             partial=True)[:MAX_REBUILT_READINGS]:
                if cell.payload_len <= cell.local:
                    continue
                for c in self._match(data, cell, site, only=tpl):
                    kept = [v for v in c.row if isinstance(v, str)]
                    if "truncated" in c.flags and _partial_ok(kept):
                        cands.append(c)
        if not cands:
            return None
        cands.sort(key=lambda c: c.score)
        best = cands[0]
        if self._start_of_live(best):
            return None
        best.conf = LOW
        best.flags.add("partial")
        best.notes.insert(0, "the start of a freed record: its first %d bytes were overwritten "
                             "by a freeblock header and its end by a newer record (an update "
                             "to a shorter value leaves this); only what survives is shown"
                          % best.cell.rebuilt)
        return self._accept(best, site, source, data)

    @staticmethod
    def _ordered(tpls, owner):
        if not owner:
            return tpls
        return sorted(tpls, key=lambda t: t.name != owner)

    def _match(self, data, cell, site, only=None, dcache=None):
        """A candidate for every template the cell fits. dcache: decoded payloads shared by
        readings of one position (same bytes, same serial types decode the same)."""
        n = len(cell.types)
        tpls = [only] if only is not None else \
            self._ordered(self.by_n.get((n, cell.index), ()), site.owner)
        out = []
        decoded = None
        for tpl in tpls:
            if tpl.n != n or tpl.index_tree != cell.index:
                continue
            ok, atypical = tpl.check_types(cell.types)
            if not ok:
                continue
            if decoded is None:
                key = (cell.body, cell.local, cell.pstart, cell.payload_len, cell.ovfl,
                       tuple(cell.types)) if dcache is not None else None
                decoded = dcache.get(key) if key is not None else None
                if decoded is None:
                    decoded = self._decode(data, cell, site)
                    if key is not None:
                        dcache[key] = decoded or ()
                if not decoded:
                    return out
            values, truncated, chain, complete, why = decoded
            good, more, notes = tpl.check_values(values, cell.types[:len(values)], cell.solved)
            plausible = getattr(tpl, "plausible", None) or \
                (lambda cell, values, atypical, tpl=tpl: _plausible(tpl, cell, values, atypical))
            if not good or not plausible(cell, values, atypical + more):
                continue
            c = _Cand()
            c.tpl, c.cell, c.chain, c.complete, c.conf = tpl, cell, chain, complete, LOW
            c.atypical = atypical + more
            c.notes = list(notes) + ([why] if why else [])
            c.flags = set(("truncated",)) if truncated else set()
            if any(isinstance(v, InvalidText) for v in values):
                c.flags.add("damaged_text")
            c.uncertain = self._uncertain(tpl, cell)
            c.rowid = cell.rowid
            c.row, flags = tpl.info.record_to_row(cell.rowid, values, damaged=truncated)
            c.flags.update(f for f in flags if f in ("pre_alter", "extra_values"))
            prior = 0                       # how well lost values match the live rows
            for pos in range(min(cell.lost, len(values))):
                v = values[pos]
                if pos in cell.solved and isinstance(v, (str, bytes)):
                    size = len(v.encode("utf-8")) if isinstance(v, str) else len(v)
                    if size in tpl.length_prior[pos]:
                        prior -= 1
                elif isinstance(v, int) and pos != tpl.alias:
                    fits = tpl.plausible_int(pos, v)
                    if fits is not None:
                        prior += -1 if fits else 1
            c.score = (0 if complete else 1, cell.rebuilt > 0, c.atypical, len(cell.solved),
                       len(c.uncertain), prior, tpl.name != site.owner, tpl.kind == "master")
            adjust = getattr(tpl, "adjust_score", None)
            if adjust is not None:
                c.score = adjust(c.score, c.row)
            out.append(c)
        return out

    @staticmethod
    def _uncertain(tpl, cell):
        notes = []
        for pos in range(cell.lost):
            st = cell.types[pos]
            if pos == tpl.alias:
                continue
            name = tpl.columns[tpl.info.storage_order[pos]]
            if st in (0, 8, 9):
                notes.append("column %s: NULL, 0 or 1 (value lost)" % name)
            elif st in (6, 7) and tpl.type_ok(pos, 6)[0] and tpl.type_ok(pos, 7)[0]:
                notes.append("column %s: 8-byte integer or real" % name)
        return notes

    def _unattributed(self, data, cell, site):
        """A well-formed record no table fits: kept (low confidence) when it carries real
        content (2+ columns, some text of printable characters)."""
        if len(cell.types) < 2 or cell.payload_len < 6:
            return None
        if not any(type_class(st) in (TEXT, BLOB) and serial_size(st) >= 2 for st in cell.types):
            return None
        decoded = self._decode(data, cell, site)
        if decoded is None:
            return None
        values, truncated, chain, complete, why = decoded
        texts = [v for v in values if isinstance(v, str)]
        if not texts or not all(printable(t) for t in texts):
            return None
        c = _Cand()
        c.tpl, c.cell, c.chain, c.complete, c.conf = None, cell, chain, complete, LOW
        c.atypical, c.uncertain = 0, []
        c.notes = ["no table's schema fits"] + ([why] if why else [])
        c.flags = set(("truncated",)) if truncated else set()
        c.rowid = cell.rowid
        c.row = list(values)
        c.score = (9,)
        return c

    # -- payload -----------------------------------------------------------
    def _decode(self, data, cell, site):
        """(values, truncated, overflow_chain, complete, note) or None when unusable."""
        if cell.payload_len > MAX_PAYLOAD:
            return None
        body = cell.local_body(data)
        chain, complete, note = [], True, None
        need = cell.payload_len - cell.local
        if cell.ovfl is not None and need > 0:
            more, chain, complete, note = self._overflow(cell.ovfl, need, site, cell.local)
            body += more
        values, off, truncated = [], 0, False
        for st in cell.types:
            size = serial_size(st)
            if off + size > len(body):
                truncated = True
                if st >= 12 and off < len(body):        # keep the part of a text/blob that survives
                    values.append(self._partial(body[off:], st))
                    note = "%s; value %d holds only its first %d of %d bytes" % (
                        note or "payload incomplete", len(values), len(body) - off, size)
                break
            values.append(decode_value(body, off, st, self.encoding))
            off += size
        return values, truncated, chain, complete, note

    def _partial(self, raw, st):
        if not st & 1:
            return bytes(raw)
        for cut in range(4):                  # the cut may split a multi-byte character
            try:
                return bytes(raw[:len(raw) - cut]).decode(self.encoding)
            except UnicodeDecodeError:
                continue
        return InvalidText(bytes(raw))

    def _overflow(self, first, need, site, local=0):
        """Follow an overflow chain: (bytes, pages, complete, note). Stops at a page that a
        live tree now uses (its content is no longer ours). A page that became a freelist
        trunk still holds the data after its list of leaf pages: that part is kept (the
        overwritten bytes zero-filled and named in the note) and the chain goes on at the
        trunk's leaf that holds raw data, as SQLite frees a chain's pages in order (the next
        one becomes a leaf of that trunk)."""
        view, pmap = site.view, site.pmap
        per = self.usable - 4
        max_pages = need // per + 2
        parts, chain, seen = [], [], set()
        nxt, note, rebuilt = first, None, []
        done = local                    # record bytes read so far
        while need > 0:
            if view is None or nxt in seen or len(chain) >= max_pages:
                note = "overflow chain ends early"
                break
            if pmap is not None and nxt in pmap.owner:
                note = "overflow page %d is now used by %s" % (nxt, pmap.owner[nxt][0])
                break
            if pmap is not None and nxt in pmap.trunks:
                page = view.safe_page(nxt)
                take = min(need, per)
                count = _U32.unpack_from(page, 4)[0] if page is not None else None
                if page is None or rebuilt or count is None or 8 + 4 * count >= 4 + take:
                    note = "overflow page %d became a freelist trunk" % nxt
                    break
                lost = 4 + 4 * count            # data bytes the trunk header and list overwrote
                seen.add(nxt)
                chain.append(nxt)
                parts.append(b"\0" * lost + bytes(page[4 + lost:4 + take]))
                rebuilt.append("overflow page %d became a freelist trunk: record bytes %d-%d "
                               "overwritten by its page list (zero bytes here)"
                               % (nxt, done, done + lost - 1))
                need -= take
                done += take
                if need <= 0:
                    break
                leaves = [_U32.unpack_from(page, 8 + 4 * i)[0] for i in range(count)]
                raw = [p for p in leaves if p not in seen and p not in pmap.owner and
                       view.safe_page(p) is not None and view.safe_page(p)[0] not in
                       (0x02, 0x05, 0x0A, 0x0D)]
                if not raw:
                    note = "overflow page %d became a freelist trunk; none of its leaf pages " \
                           "holds the rest" % nxt
                    break
                nxt = nxt + 1 if nxt + 1 in raw else raw[0]
                rebuilt.append("next page guessed: freed page %d (a leaf of that trunk "
                               "holding raw data)" % nxt)
                continue
            page = view.safe_page(nxt)
            if page is None:
                note = "overflow page %d does not exist" % nxt
                break
            seen.add(nxt)
            chain.append(nxt)
            take = min(need, per)
            parts.append(bytes(page[4:4 + take]))
            need -= take
            done += take
            nxt = _U32.unpack_from(page, 0)[0]
            if need > 0 and nxt == 0:
                note = "overflow chain ends early"
                break
        complete = need <= 0
        if rebuilt:
            # 'overflow chain rebuilt' marks the record (low confidence, flag)
            note = "overflow chain rebuilt past a freelist trunk: " + "; ".join(rebuilt) + \
                ("" if complete else "; " + (note or "overflow chain ends early"))
            return b"".join(parts), chain, complete, note
        return b"".join(parts), chain, complete, None if complete else note

    # -- emitting ----------------------------------------------------------
    def _accept(self, cand, site, source, data, alts=()):
        """Live check and confidence, then emit. Returns the cell's end offset. alts: readings
        of the same bytes for other tables that fit as well: a record live in any of them is
        a copy of that live row (left on a freed page), not a deleted one."""
        cell = cand.cell
        if self._stop():
            return cell.end             # stopping: the live check may be incomplete now
        self.stats["candidates"] += 1
        key = (_cell_digest(data, cell), site.owner, site.file) \
            if cell.ovfl is None and cell.rebuilt == 0 else None
        if cand.tpl is not None:
            if cand.tpl.kind == "master" and not self.include_schema:
                if key is not None:
                    self._seen_cells[key] = "skip"
                return cell.end
            live = self.fx.live.status(cand.tpl, cand.rowid, cand.row)
            if live != "live" and any(
                    a.tpl is not None and a.tpl.kind != "master" and
                    self.fx.live.status(a.tpl, a.rowid, a.row) == "live" for a in alts):
                live = "live"
            if live == "prior_version" and _zero_damaged_copy(
                    cand.row, self.fx.live.live_row(cand.tpl, cand.rowid), cand.tpl):
                live = "live"           # the live row with parts zeroed: nothing it lacks
            if live == "live":
                self.stats["live_skipped"] += 1
                if key is not None:
                    self._seen_cells[key] = "skip"
                return cell.end
            if live == "prior_version":
                cand.flags.add("prior_version")
                cand.notes.append("its key is live with other values: an older version")
            elif live is None and cand.rowid is None and cand.tpl.kind != "master":
                # the rowid was overwritten (freeblock header): one live row agreeing in every
                # column but one is the row this is an older version of (said, never certain)
                guess = self.fx.live.infer_rowid(cand.tpl, cand.row)
                by = None if guess is not None else \
                    self.fx.live.replaced_by(cand.tpl, None, cand.row)
                if by is not None:
                    # no single near row (or a table too large to index): its UNIQUE key is
                    # live, so it is that row with other values - an older version of it
                    cand.flags.add("prior_version")
                    cand.notes.append("rowid lost; its unique key (%s) is live as rowid %d "
                                      "with other values: an older version of it"
                                      % (", ".join(by[1]), by[0]))
                elif guess is not None:
                    rid, col = guess
                    cand.rowid = rid
                    alias = cand.tpl.info.rowid_alias
                    if alias is not None and alias < len(cand.row):
                        cand.row[alias] = rid
                    cand.flags.update(("prior_version", "rowid_inferred"))
                    cand.notes.append("rowid lost; inferred as %d: the only live row with the "
                                      "same values in every column but %s (an older version "
                                      "of it)" % (rid, col))
            elif live is None and cand.tpl.kind != "master":
                # deleted, yet the same content is live under another rowid (deleted and
                # inserted again, or a duplicate row): kept, and said where
                again = self.fx.live.rowids_with_values(cand.tpl, cand.rowid, cand.row)
                if again:
                    cand.flags.add("content_live")
                    cand.notes.append("the same values are live as rowid %s (deleted and "
                                      "inserted again, or a duplicate row)"
                                      % ", ".join(str(r) for r in again))
                else:
                    # its UNIQUE key is live under another rowid with other values: replaced
                    # (INSERT OR REPLACE deletes the row and inserts a new one)
                    by = self.fx.live.replaced_by(cand.tpl, cand.rowid, cand.row)
                    if by is not None:
                        cand.flags.add("prior_version")
                        cand.notes.append("its unique key (%s) is live as rowid %d with other "
                                          "values: an older version, replaced"
                                          % (", ".join(by[1]), by[0]))
        self._confidence(cand, site)
        if key is not None:
            self._seen_cells[key] = cand
        self._emit(cand, source, site)
        return cell.end

    @staticmethod
    def _confidence(cand, site):
        conf = HIGH if cand.cell.rebuilt == 0 else MEDIUM
        notes, flags = cand.notes, cand.flags
        if cand.tpl is None:
            conf = LOW
        else:
            notes.append("sizes add up exactly")
            if cand.atypical:
                conf = worst(conf, MEDIUM)
                notes.append("%d value(s) unusual for the declared type" % cand.atypical)
            if site.owner and cand.tpl.name != site.owner:
                conf = worst(conf, MEDIUM)
                notes.append("found on a page of %s" % site.owner)
            elif site.owner:
                notes.append("page is (or was) part of %s" % site.owner)
            if cand.uncertain:
                conf = worst(conf, MEDIUM)
                notes.extend(cand.uncertain)
                flags.add("uncertain_value")
            if cand.cell.solved:
                notes.append("size of %d lost column(s) inferred from the free space"
                             % len(cand.cell.solved))
            if "ambiguous_table" in flags:
                conf = worst(conf, MEDIUM)
            if "end_guessed" in flags or "ambiguous_reading" in flags or "damaged_text" in flags:
                conf = LOW
        if cand.rowid is None and not cand.cell.index:
            flags.add("rowid_unknown")
        if site.intact_source == ORPHAN and cand.cell.rebuilt == 0:
            # an intact cell of a page nothing references: a deleted row (a page lost from
            # the freelist), or a live row a damaged or manipulated tree no longer reaches;
            # never presented as a plain deleted row (audit: UNREFERENCED_PAGES)
            flags.add("unreferenced_page")
            notes.append("on a page that no table, index or the freelist references: a "
                         "deleted row, or a live row that a damaged tree no longer reaches")
            conf = worst(conf, MEDIUM)
        if "rowid_inferred" in flags:
            conf = worst(conf, MEDIUM)
        if any(n.startswith("overflow chain rebuilt") for n in notes):
            conf = LOW
            flags.add("overflow_rebuilt")
        if not cand.complete or "truncated" in flags:
            conf = LOW
            flags.add("overflow_partial" if cand.cell.ovfl is not None else "truncated")
        elif cand.chain:
            notes.append("overflow chain of %d page(s) followed" % len(cand.chain))
        cand.conf = conf

    def _emit(self, cand, source, site):
        tpl = cand.tpl
        columns = tpl.columns if tpl is not None else ["col%d" % i for i in range(len(cand.row))]
        rec = Record(tpl.name if tpl is not None else None, columns, cand.row,
                     site.prov(source, cand.cell, cand.chain), cand.conf, cand.notes,
                     cand.rowid, flags=cand.flags)
        also = [n for note in cand.notes if note.startswith("also fits: ")
                for n in note[len("also fits: "):].split(", ")]
        if also:
            rec.candidates = [rec.table] + also
        ident = (rec.table, rec.rowid, tuple(value_key(v) for v in rec.values))
        prev = self._identity.get(ident)
        loose = (rec.table, _loose_key(tpl, rec.values)) if tpl is not None else None
        if prev is None and loose is not None:
            other = self._loose.get(loose)
            if other is not None and (other.rowid is None or rec.rowid is None):
                prev = other
        if prev is not None:
            self._merge(prev, rec)
            site.records.append(prev)
            return
        self._identity[ident] = rec
        if loose is not None:
            self._loose.setdefault(loose, rec)
        self.records.append(rec)
        site.records.append(rec)

    def _merge(self, prev, rec):
        self.stats["merged"] += 1
        if _RANK[rec.confidence] < _RANK[prev.confidence] or \
                (rec.rowid is not None and prev.rowid is None
                 and _RANK[rec.confidence] <= _RANK[prev.confidence]):
            prev.copies.append(prev.prov)
            prev.prov, prev.confidence, prev.reasons = rec.prov, rec.confidence, rec.reasons
            prev.flags = (prev.flags - set(("rowid_unknown",))) | rec.flags \
                if rec.rowid is not None else prev.flags | rec.flags
        else:
            prev.copies.append(rec.prov)
        if prev.rowid is None and rec.rowid is not None:
            prev.rowid, prev.values = rec.rowid, list(rec.values)
            prev.flags.discard("rowid_unknown")
            self._identity[(prev.table, prev.rowid,
                            tuple(value_key(v) for v in prev.values))] = prev


def _cell_end(data, off, usable, ptype):
    """End offset of the live cell at `off` on a page of type `ptype` (its header, the part
    of the payload stored on the page and the overflow page number), or None."""
    p = off + 4 if ptype == INDEX_INTERIOR else off
    r = varint(data, p, usable)
    if r is None:
        return None
    plen, p = r
    if ptype == TABLE_LEAF:
        r = varint(data, p, usable)
        if r is None:
            return None
        p = r[1]
    local = local_payload_size(plen, usable, ptype == TABLE_LEAF)
    end = p + local + (4 if local < plen else 0)
    return end if end <= usable else None


def _loose_key(tpl, row):
    alias = tpl.info.rowid_alias
    return tuple(value_key(v) for i, v in enumerate(row) if i != alias)


def _has_content(v):
    if v is None:
        return False
    if isinstance(v, (bytes, str)):
        return len(v) > 0
    return True


def _intact_content(tpl, cell, row):
    """Columns with content whose serial type survived (row in declared order)."""
    order = tpl.info.storage_order
    return sum(1 for pos in range(cell.lost, len(order))
               if pos != tpl.alias and order[pos] < len(row) and _has_content(row[order[pos]]))


_ZEROISH = (None, 0, 0.0, "", b"")


def _zero_damaged_copy(row, live, tpl):
    """True when a record holds nothing its live row lacks but zeros: every value equals the
    live one, or is zero / empty / NULL, or is the start of the live value followed only by
    zero bytes (a copy of the live cell partly wiped - secure delete zeroes freed bytes),
    and at least one non-zero value matches. The wiping must show: some text or blob is the
    live value's start padded with zero bytes (text never ends so) - a real older version
    whose numbers were 0 before (status, a timestamp set later) is kept as one."""
    if live is None:
        return False
    alias = tpl.info.rowid_alias if tpl is not None else None
    matched = wiped = False
    for i, v in enumerate(row):
        if i == alias:
            continue
        lv = live[i] if i < len(live) else None
        if v == lv:
            if v not in _ZEROISH:
                matched = True
            continue
        if isinstance(v, bool) or (not isinstance(v, (str, bytes, bytearray)) and v in _ZEROISH):
            continue
        if isinstance(v, str) and isinstance(lv, str):
            head = v.rstrip("\x00")
            if len(head) < len(v) and lv.startswith(head):
                matched = matched or bool(head)
                wiped = True
                continue
            if not head:
                continue
            return False
        if isinstance(v, (bytes, bytearray, InvalidText)):
            b = bytes(v)
            head = b.rstrip(b"\x00")
            if not head:
                continue
            if isinstance(lv, (bytes, bytearray)) and len(head) < len(b) and \
                    bytes(lv).startswith(head):
                matched = wiped = True
                continue
            return False
        return False
    return matched and wiped


def _cell_digest(data, cell):
    """16 bytes standing for a cell's bytes in the cache of cells already decided (a large
    database repeats millions of identical live copies: their bytes were all kept)."""
    return hashlib.blake2b(data[cell.start:cell.end], digest_size=16).digest()


def _informative(row):
    """True when at least one value says something: not NULL, 0, 0.0, empty, nor bytes (or
    invalid text) that are all zero. A rebuilt record with none is the header's varints read
    over zeroed space, the kind of thing free space is full of, never a row worth showing."""
    for v in row:
        if v is None or v == 0 or v == "" or v == b"":
            continue
        if isinstance(v, (bytes, bytearray, InvalidText)):
            if bytes(v).count(0) == len(v):
                continue
        return True
    return False


def _odd_class(tpl, row):
    """A blob, or text not valid in the encoding, in a column whose affinity is not BLOB (row
    in declared order): possible in SQLite, rare in real rows, common in misreadings."""
    order = tpl.info.storage_order
    for pos, aff in enumerate(tpl.affinity):
        if aff == "BLOB" or pos == tpl.alias or order[pos] >= len(row):
            continue
        if isinstance(row[order[pos]], (bytes, bytearray, InvalidText)):
            return True
    return False


def _partial_ok(texts):
    """A partial record is kept when its text values (the cut one included) are printable
    and hold at least PARTIAL_MIN characters together: a few stray bytes are not a record."""
    return bool(texts) and all(printable(t) for t in texts) and \
        sum(len(t) for t in texts) >= PARTIAL_MIN


def _header_only_lost(tpl, cell, stored):
    """Every serial type was lost but the values themselves survived: accepted only when each
    one is printable text whose length the table's live rows use for that column."""
    for i, v in stored:
        if i not in cell.solved or not isinstance(v, str) or not v or not printable(v):
            return False
        prior = tpl.length_prior[i] if i < len(tpl.length_prior) else ()
        if len(v.encode("utf-8")) not in prior:
            return False
    return len(stored) >= 2


def _plausible(tpl, cell, values, atypical):
    """Evidence rules on top of the type checks: a record must carry some content, and a
    rebuilt one must carry it in bytes that survived (not only in inferred ones) with at most
    one value unusual for its column."""
    stored = [(i, v) for i, v in enumerate(values) if i != tpl.alias]
    if not any(_has_content(v) for _i, v in stored) or cell.payload_len <= cell.header_len:
        return False
    nonnull = sum(1 for _i, v in stored if v is not None)
    if cell.rebuilt:
        if not any(_has_content(v) and serial_size(cell.types[i])
                   for i, v in stored if i >= cell.lost) and \
                not _header_only_lost(tpl, cell, stored):
            return False            # the surviving bytes must hold some of the content
        return atypical < 2 and atypical * 2 <= nonnull
    return atypical < 2 or atypical * 2 <= nonnull
