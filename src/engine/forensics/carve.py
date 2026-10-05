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
import re
import struct
import time

from ..fileformat.btree import INDEX_INTERIOR, INDEX_LEAF, TABLE_LEAF, parse_page_header
from ..fileformat.record import InvalidText, decode_value, serial_size
from ..fileformat.wal import CURRENT
from ..issues import IssueLog
from .cellparse import parse_intact, reconstruct_all, stale_header
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
MAX_REBUILT_READINGS = 256  # readings of one rebuilt cell weighed against each other
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
            if self.progress is not None and i % 64 == 0:
                self.progress(i, total)
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
            self.stats["raw_pages"] += 1
            try:
                job()
            except Exception as e:
                self._failed(e, "raw page")
        if self.progress is not None:
            self.progress(total, total)
        self._finish_copies()
        self.stats["records"] = len(self.records)
        self.stats["seconds"] = round(time.time() - t0, 3)
        self.stats["stopped"] = self.stopped
        return CarveResult(self.records, self.stats, not self.stopped)

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
            for i in range(h.cell_count):
                off = _U16.unpack_from(data, h.ptr_start + 2 * i)[0]
                if ptr_end <= off <= usable - 4:
                    cells = self._intact_cells(data, off, usable, kinds)
                    if cells:
                        self._take_intact(data, off, cells, site, site.intact_source)
        # on freelist / orphan pages everything counts as that page's source; elsewhere the
        # free areas are the 'freeblock' and 'unallocated' sources
        whole = site.area_source is not None
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
                prev, fb = fb, _U16.unpack_from(data, fb)[0]
        gap_end = min(h.content_start, usable)
        if gap_end - ptr_end >= 4 and (whole or UNALLOCATED in self.sources):
            self._area(data, ptr_end, gap_end, False, kinds or RAW_KINDS, site, UNALLOCATED)

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
            idx = bisect.bisect_left(order, took) if took is not None and took > pos else idx + 1

    # -- choosing ----------------------------------------------------------
    def _take_intact(self, data, pos, cells, site, source):
        """Accept the best reading of the intact cell(s) at pos. Returns the cell's end offset,
        or None when no reading is acceptable."""
        cands = []
        for cell in cells:
            key = (bytes(data[cell.start:cell.end]), site.owner, site.file) \
                if cell.ovfl is None else None
            cached = self._seen_cells.get(key) if key is not None else None
            if cached is not None:
                if cached != "skip":
                    self._emit(cached.clone(cell), source, site)
                return cell.end
            cands.extend(self._match(data, cell, site))
        if not cands:
            best = self._unattributed(data, cells[0], site) if self.unattributed else None
            if best is None:
                return None
        else:
            cands.sort(key=lambda c: c.score)
            best = cands[0]
            others = sorted(set(c.tpl.name for c in cands
                                if c.tpl is not best.tpl and c.score[:6] == best.score[:6]))
            if others:
                best.notes.append("also fits: %s" % ", ".join(others))
                if best.tpl.name != site.owner:
                    best.flags.add("ambiguous_table")
        best.notes.insert(0, "header intact")
        return self._accept(best, site, source, data)

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
        for e, is_strong in [(e, True) for e in strong] + [(e, False) for e in weak]:
            cands = []
            for kind in kinds:
                if kind == IINT_KIND:
                    continue
                index = kind == INDEX_KIND
                tpls = [t for t in self.templates if t.index_tree == index]
                own = [t for t in tpls if t.name == site.owner]
                # the page's own table first: a freed cell of a page belongs to its table
                for group in ([own, [t for t in tpls if t.name != site.owner]] if own else [tpls]):
                    cells = reconstruct_all(data, pos, e, self.usable, index, group,
                                            cache=caches[kind])
                    for tpl, cell in cells[:MAX_REBUILT_READINGS]:
                        if is_strong or not cell.solved:
                            for c in self._match(data, cell, site, only=tpl, dcache=dcache):
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
            return self._accept(best, site, source, data)
        return None

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
            more, chain, complete, note = self._overflow(cell.ovfl, need, site)
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

    def _overflow(self, first, need, site):
        """Follow an overflow chain: (bytes, pages, complete, note). Stops at a page that a
        live tree or the freelist trunk list now uses (its content is no longer ours)."""
        view, pmap = site.view, site.pmap
        per = self.usable - 4
        max_pages = need // per + 2
        parts, chain, seen = [], [], set()
        nxt, note = first, None
        while need > 0:
            if view is None or nxt in seen or len(chain) >= max_pages:
                note = "overflow chain ends early"
                break
            if pmap is not None and nxt in pmap.owner:
                note = "overflow page %d is now used by %s" % (nxt, pmap.owner[nxt][0])
                break
            if pmap is not None and nxt in pmap.trunks:
                note = "overflow page %d became a freelist trunk" % nxt
                break
            page = view.safe_page(nxt)
            if page is None:
                note = "overflow page %d does not exist" % nxt
                break
            seen.add(nxt)
            chain.append(nxt)
            take = min(need, per)
            parts.append(bytes(page[4:4 + take]))
            need -= take
            nxt = _U32.unpack_from(page, 0)[0]
            if need > 0 and nxt == 0:
                note = "overflow chain ends early"
                break
        complete = need <= 0
        return b"".join(parts), chain, complete, None if complete else note

    # -- emitting ----------------------------------------------------------
    def _accept(self, cand, site, source, data):
        """Live check and confidence, then emit. Returns the cell's end offset."""
        cell = cand.cell
        if self._stop():
            return cell.end             # stopping: the live check may be incomplete now
        self.stats["candidates"] += 1
        key = (bytes(data[cell.start:cell.end]), site.owner, site.file) \
            if cell.ovfl is None and cell.rebuilt == 0 else None
        if cand.tpl is not None:
            if cand.tpl.kind == "master" and not self.include_schema:
                if key is not None:
                    self._seen_cells[key] = "skip"
                return cell.end
            live = self.fx.live.status(cand.tpl, cand.rowid, cand.row)
            if live == "live":
                self.stats["live_skipped"] += 1
                if key is not None:
                    self._seen_cells[key] = "skip"
                return cell.end
            if live == "prior_version":
                cand.flags.add("prior_version")
                cand.notes.append("its key is live with other values: an older version")
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
                   for i, v in stored if i >= cell.lost):
            return False            # the surviving bytes must hold some of the content
        return atypical < 2 and atypical * 2 <= nonnull
    return atypical < 2 or atypical * 2 <= nonnull
