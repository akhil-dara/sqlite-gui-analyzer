"""The 100-byte database header."""

import struct

from .record import ENCODINGS

MAGIC = b"SQLite format 3\x00"
HEADER_SIZE = 100


class HeaderError(ValueError):
    """Not an SQLite 3 database, or an unusable header."""


class DbHeader(object):
    __slots__ = ("page_size", "write_version", "read_version", "reserved",
                 "change_counter", "header_page_count", "freelist_trunk",
                 "freelist_count", "schema_cookie", "schema_format",
                 "largest_root", "text_encoding", "encoding", "user_version",
                 "incremental_vacuum", "application_id", "version_valid_for",
                 "sqlite_version")

    @property
    def usable_size(self):
        return self.page_size - self.reserved

    @property
    def is_wal_mode(self):
        return self.write_version == 2 or self.read_version == 2

    def page_count(self, file_size):
        """Pages in the main file: header value if trustworthy, else from file size."""
        if self.header_page_count and self.version_valid_for == self.change_counter:
            return self.header_page_count
        return file_size // self.page_size

    @classmethod
    def parse(cls, buf):
        if len(buf) < HEADER_SIZE or bytes(buf[:16]) != MAGIC:
            raise HeaderError("missing 'SQLite format 3' magic")
        h = cls()
        raw_ps = struct.unpack_from(">H", buf, 16)[0]
        h.page_size = 65536 if raw_ps == 1 else raw_ps
        if h.page_size < 512 or h.page_size > 65536 or h.page_size & (h.page_size - 1):
            raise HeaderError("invalid page size %d" % raw_ps)
        h.write_version, h.read_version, h.reserved = buf[18], buf[19], buf[20]
        if h.page_size - h.reserved < 480:
            raise HeaderError("reserved bytes %d leave too little usable space" % h.reserved)
        (h.change_counter, h.header_page_count, h.freelist_trunk, h.freelist_count,
         h.schema_cookie, h.schema_format, _cache, h.largest_root, h.text_encoding,
         h.user_version, h.incremental_vacuum, h.application_id) = struct.unpack_from(
            ">12I", buf, 24)
        h.version_valid_for, h.sqlite_version = struct.unpack_from(">2I", buf, 92)
        h.encoding = ENCODINGS.get(h.text_encoding, "utf-8")
        return h
