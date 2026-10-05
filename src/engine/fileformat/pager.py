"""Read-only page source: the main database file plus an optional WAL overlay."""

import mmap
import os
import threading
from collections import OrderedDict

from .header import DbHeader, HeaderError, HEADER_SIZE


class PageError(ValueError):
    pass


class Pager(object):
    """page(n) returns the effective bytes of page n (1-based).

    With a WalFile and overlay=True, pages come from the WAL's `current` frames
    when present, otherwise from the main file. Nothing is ever written.
    """

    def __init__(self, main_path, wal=None, overlay=True, issues=None, cache_pages=256):
        self.main_path = main_path
        self.wal = wal if overlay else None
        self.issues = issues
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._cache_pages = cache_pages
        self.main_size = os.path.getsize(main_path)
        self._f = open(main_path, "rb")
        self._mm = None
        if self.main_size > 0:
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            self._init_geometry()
        except Exception:
            self.close()
            raise

    def _init_geometry(self):
        wal = self.wal
        if wal is not None and 1 in wal.overlay:
            first = wal.page_data(wal.overlay[1])
        elif self.main_size >= HEADER_SIZE:
            first = self._mm[:HEADER_SIZE]
        else:
            raise HeaderError("main file has no header and the WAL has no committed page 1")
        self.header = DbHeader.parse(first)
        self.page_size = self.header.page_size
        if wal is not None and wal.page_size != self.page_size:
            raise PageError("WAL page size %d != database page size %d"
                            % (wal.page_size, self.page_size))
        self.usable_size = self.header.usable_size
        self.encoding = self.header.encoding
        if wal is not None and wal.db_size_pages:
            declared = wal.db_size_pages
        else:
            declared = self.header.page_count(self.main_size)
        # The size the header (or the last WAL commit) declares is written by whoever made
        # the file: everything sized or looped from the page count uses at most the pages the
        # files hold (with a little slack for a file cut short), and the rest is reported.
        # A WAL frame's page_no is attacker-declared too: the WAL's bytes bound the pages it
        # can really supply (one page per frame), so its largest page number is capped at
        # main-file pages + its frame count, not the declared page_no.
        existing = max(1, -(-self.main_size // self.page_size))
        if wal is not None and wal.overlay:
            existing = max(existing, min(max(wal.overlay), existing + wal.frames_total))
        allowed = existing + max(16, existing // 100)
        self.declared_page_count = declared
        self.page_count = min(declared, allowed)
        if declared > allowed and self.issues is not None:
            self.issues.add("page_count_clamped",
                            "the %s declares %d pages but the files hold %d; pages after %d are "
                            "treated as missing" % ("WAL's last commit" if wal is not None and
                                                    wal.db_size_pages else "header",
                                                    declared, existing, allowed),
                            "header", "warning")

    @property
    def overlaid_pages(self):
        return set(self.wal.overlay) if self.wal is not None else set()

    def page(self, n):
        if n < 1 or n > self.page_count:
            raise PageError("page %d outside 1..%d" % (n, self.page_count))
        with self._lock:
            data = self._cache.get(n)
            if data is not None:
                self._cache.move_to_end(n)
                return data
        data = self._read(n)
        with self._lock:
            self._cache[n] = data
            if len(self._cache) > self._cache_pages:
                self._cache.popitem(last=False)
        return data

    def _read(self, n):
        wal = self.wal
        if wal is not None and n in wal.overlay:
            return wal.page_data(wal.overlay[n])
        start = (n - 1) * self.page_size
        end = start + self.page_size
        data = bytes(self._mm[start:end]) if self._mm is not None and start < self.main_size else b""
        if len(data) < self.page_size:
            if self.issues is not None:
                self.issues.add("short_page", "page %d missing from main file; zero-filled" % n,
                                "page %d" % n)
            data = data + b"\x00" * (self.page_size - len(data))
        return data

    def main_page(self, n):
        """Page n exactly as stored in the main file (no WAL frame applied), uncached.

        Pages past the end of the file come back zero-filled; n may exceed page_count (a file
        can hold more pages than its header or the WAL says the database has).
        """
        if n < 1:
            raise PageError("page %d outside the main file" % n)
        start = (n - 1) * self.page_size
        mm = self._mm
        data = bytes(mm[start:start + self.page_size]) if mm is not None and start < self.main_size else b""
        if len(data) < self.page_size:
            data = data + b"\x00" * (self.page_size - len(data))
        return data

    def close(self):
        mm, self._mm = self._mm, None
        if mm is not None:
            mm.close()
        f, self._f = self._f, None
        if f is not None:
            f.close()
