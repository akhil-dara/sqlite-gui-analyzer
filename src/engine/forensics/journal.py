"""Rollback journal (<db>-journal) reader: the pre-transaction content of changed pages.

Layout (big-endian): a header padded to the sector size
    magic d9d505f920a163d7 | record count | checksum nonce | initial db size (pages) |
    sector size | page size
followed by page records: page number (4) | original page image (page size) | checksum (4),
where checksum = nonce + page[size-200] + page[size-400] + ... (bytes, 32-bit wrap), the sum
SQLite's pager uses. A journal can hold several such segments, each starting on a sector
boundary.

Journals are read whether or not they are hot:
  * hot (magic present): an interrupted transaction; the main file may hold part of it and
    the journal holds what the changed pages looked like before it began;
  * magic zeroed (PERSIST mode after commit, or a transaction not yet synced): the records of
    the last transaction are usually still there. The nonce is then derived from the first
    record's checksum and every other record is checked against it.
The file is only ever read (a short-lived read-only handle per read).
"""

import os
import struct
from collections import OrderedDict

from .pages import OverlayView

JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
HEADER_BYTES = 28
_SIZES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
MAX_RECORDS = "journal_records"     # engine.limits name: page records read at most


def journal_checksum(data, nonce, page_size):
    """SQLite pager checksum of one page image."""
    total = nonce
    i = page_size - 200
    while i > 0:
        total += data[i]
        i -= 200
    return total & 0xFFFFFFFF


class JournalRecord(object):
    __slots__ = ("page_no", "offset", "checksum", "checksum_ok", "segment", "beyond_count")

    def __init__(self, page_no, offset, checksum, checksum_ok, segment, beyond_count):
        self.page_no, self.offset, self.checksum = page_no, offset, checksum
        self.checksum_ok, self.segment, self.beyond_count = checksum_ok, segment, beyond_count

    def as_dict(self):
        return {"page": self.page_no, "offset": self.offset, "checksum": self.checksum,
                "checksum_ok": self.checksum_ok, "segment": self.segment,
                "beyond_count": self.beyond_count}


class JournalSegment(object):
    __slots__ = ("offset", "magic_ok", "declared", "nonce", "nonce_derived", "initial_pages",
                 "sector_size", "page_size", "records")

    def as_dict(self):
        return dict((k, getattr(self, k)) for k in self.__slots__)


class Journal(object):
    """Parsed rollback journal. `records` lists every page record found; `page(n)` returns the
    pre-transaction image of page n (first record for n with a valid checksum)."""

    def __init__(self, path, db_page_size, max_page, issues=None):
        self.path = path
        self.size = os.path.getsize(path)
        self.issues = issues
        self.db_page_size = db_page_size
        self.max_page = max_page
        self.segments, self.records = [], []
        self.header_zeroed = False
        self.hot = False
        self.page_size = db_page_size
        self.initial_pages = None
        self._first = {}                 # page -> offset of its pre-transaction image
        self._cache = OrderedDict()
        from .. import limits
        self._max_records = limits.get(MAX_RECORDS)
        self.capped = False              # stopped at the limit journal_records
        if self.size >= HEADER_BYTES:
            with open(path, "rb") as f:
                self._parse(f)

    # -- parsing -----------------------------------------------------------
    def _parse(self, f):
        head = f.read(HEADER_BYTES)
        self.hot = head[:8] == JOURNAL_MAGIC
        self.header_zeroed = head == b"\x00" * HEADER_BYTES
        _count, _nonce, initial, sector, psize = struct.unpack_from(">5I", head, 8)
        if psize in _SIZES:
            self.page_size = psize
        if initial:
            self.initial_pages = initial
        sectors = [sector] if sector in _SIZES else list(_SIZES)
        best = None
        for sec in sectors:
            got = self._parse_from(f, sec)
            if best is None or sum(1 for r in got[1] if r.checksum_ok) > \
                    sum(1 for r in best[1] if r.checksum_ok):
                best = got
            if best[1] and all(r.checksum_ok for r in best[1]):
                break
        self.segments, self.records = best
        self.capped = len(self.records) >= self._max_records     # said in summary()
        for r in self.records:
            if r.checksum_ok and r.page_no not in self._first:
                self._first[r.page_no] = r.offset + 4

    def _parse_from(self, f, sector):
        MAX_RECORDS = self._max_records             # noqa: N806 - the limit, read once
        ps = self.page_size
        rec_size = ps + 8
        segments, records = [], []
        off = 0
        while off + HEADER_BYTES <= self.size and len(records) < MAX_RECORDS:
            f.seek(off)
            head = f.read(HEADER_BYTES)
            seg = JournalSegment()
            seg.offset, seg.magic_ok = off, head[:8] == JOURNAL_MAGIC
            declared, nonce, initial, sec, psize = struct.unpack_from(">5I", head, 8)
            if off and not seg.magic_ok:
                break                    # later segments always start with the magic
            if seg.magic_ok and sec in _SIZES:
                sector = sec
            seg.declared = declared if seg.magic_ok else None
            seg.nonce, seg.nonce_derived = nonce, False
            seg.initial_pages, seg.sector_size, seg.page_size = initial, sector, psize
            seg.records = 0
            pos = off + sector
            limit = None
            if seg.magic_ok and declared not in (0, 0xFFFFFFFF):
                limit = declared
            index = len(segments)
            known_nonce = seg.magic_ok or head[12:16] != b"\x00\x00\x00\x00"
            while pos + rec_size <= self.size and len(records) < MAX_RECORDS:
                f.seek(pos)
                raw = f.read(rec_size)
                page_no = struct.unpack_from(">I", raw, 0)[0]
                if page_no < 1 or page_no > self.max_page:
                    break
                data = raw[4:4 + ps]
                cks = struct.unpack_from(">I", raw, 4 + ps)[0]
                if not known_nonce:
                    seg.nonce = (cks - journal_checksum(data, 0, ps)) & 0xFFFFFFFF
                    seg.nonce_derived = known_nonce = True
                ok = journal_checksum(data, seg.nonce, ps) == cks
                beyond = limit is not None and seg.records >= limit
                if beyond and not ok:
                    break                # past the declared count and not a valid record
                records.append(JournalRecord(page_no, pos, cks, ok, index, beyond))
                seg.records += 1
                pos += rec_size
                if not ok and limit is None:
                    break                # SQLite also stops replaying at a torn record
                if limit is not None and seg.records == limit:
                    nxt = _next_sector(pos, sector)
                    if nxt + HEADER_BYTES <= self.size:
                        f.seek(nxt)
                        if f.read(8) == JOURNAL_MAGIC:
                            pos = nxt
                            break
            segments.append(seg)
            if pos <= off or not (pos + HEADER_BYTES <= self.size):
                break
            f.seek(pos)
            if f.read(8) != JOURNAL_MAGIC:
                break
            off = pos
        return segments, records

    # -- access ------------------------------------------------------------
    @property
    def valid(self):
        return any(r.checksum_ok for r in self.records)

    @property
    def checksums_ok(self):
        return bool(self.records) and all(r.checksum_ok for r in self.records)

    def pages(self):
        """Page numbers that have a pre-transaction image."""
        return sorted(self._first)

    def page(self, n):
        """Pre-transaction image of page n, or None when the journal does not hold it."""
        off = self._first.get(n)
        if off is None:
            return None
        data = self._cache.get(n)
        if data is None:
            with open(self.path, "rb") as f:
                f.seek(off)
                data = f.read(self.page_size)
            self._cache[n] = data
            if len(self._cache) > 256:
                self._cache.popitem(last=False)
        return data

    def record_page(self, rec):
        """Image stored by one record (also records superseded by an earlier one)."""
        with open(self.path, "rb") as f:
            f.seek(rec.offset + 4)
            return f.read(self.page_size)

    def pre_state_view(self, main_view):
        """The database as it was before the journaled transaction: journal images over the
        main file, cut to the initial size. None when the page sizes differ."""
        if self.page_size != main_view.page_size:
            return None
        pages = _LazyPages(self)
        from .pages import clamp_page_count
        existing = max(main_view.page_count, getattr(main_view, "file_pages", 0),
                       max(self._first) if self._first else 0)
        count = clamp_page_count(self.initial_pages, existing)
        return OverlayView(main_view, pages, page_count=count, label="journal pre-state")

    def summary(self):
        return {"path": self.path, "size": self.size, "hot": self.hot,
                "header_zeroed": self.header_zeroed, "page_size": self.page_size,
                "initial_pages": self.initial_pages,
                "segments": [s.as_dict() for s in self.segments],
                "records": len(self.records),
                "checksum_failures": sum(1 for r in self.records if not r.checksum_ok),
                "pages": len(self._first),
                # page records past the limit journal_records were not read
                "records_capped_at": self._max_records if self.capped else None}


class _LazyPages(object):
    """{page: image} mapping that reads journal images on demand."""

    def __init__(self, journal):
        self._j = journal

    def get(self, n, default=None):
        data = self._j.page(n)
        return default if data is None else data

    def __getitem__(self, n):
        data = self._j.page(n)
        if data is None:
            raise KeyError(n)
        return data

    def __contains__(self, n):
        return n in self._j._first


def _next_sector(pos, sector):
    return ((pos + sector - 1) // sector) * sector


def open_journal(evidence, db_page_size, max_page, issues=None):
    """Journal for the evidence set, or None when there is no (usable) -journal file."""
    path = evidence.path("journal")
    if not path:
        return None
    try:
        j = Journal(path, db_page_size, max_page, issues)
    except (OSError, ValueError, struct.error) as e:
        if issues is not None:
            issues.add("journal_unreadable", str(e), path, "warning")
        return None
    return j if (j.records or j.hot) else None
