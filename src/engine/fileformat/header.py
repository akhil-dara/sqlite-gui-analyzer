"""The 100-byte database header."""

import struct

from .record import ENCODINGS

MAGIC = b"SQLite format 3\x00"
HEADER_SIZE = 100


class HeaderError(ValueError):
    """Not an SQLite 3 database, or an unusable header."""


def signature_only_damaged(buf):
    """True when the 16-byte signature is wrong but every other fixed field of the header
    is what SQLite always writes (a valid page size, file format versions 1-2, payload
    fractions 64/32/32, schema format 1-4, text encoding 1-3) and, when more than the
    header is given, page 1 is a table b-tree page: a damaged or wiped signature on an
    otherwise readable database."""
    if len(buf) < HEADER_SIZE:
        return False
    raw_ps = struct.unpack_from(">H", buf, 16)[0]
    ps = 65536 if raw_ps == 1 else raw_ps
    if ps < 512 or ps > 65536 or ps & (ps - 1):
        return False
    if buf[18] not in (1, 2) or buf[19] not in (1, 2) or tuple(buf[21:24]) != (64, 32, 32):
        return False
    schema_format = struct.unpack_from(">I", buf, 44)[0]
    encoding = struct.unpack_from(">I", buf, 56)[0]
    if schema_format not in (0, 1, 2, 3, 4) or encoding not in (0, 1, 2, 3):
        return False
    # page 1's b-tree page type, when the caller passed more than the header
    return len(buf) <= HEADER_SIZE or buf[HEADER_SIZE] in (0x05, 0x0D)


class DbHeader(object):
    __slots__ = ("page_size", "write_version", "read_version", "reserved",
                 "change_counter", "header_page_count", "freelist_trunk",
                 "freelist_count", "schema_cookie", "schema_format",
                 "largest_root", "text_encoding", "encoding", "user_version",
                 "incremental_vacuum", "application_id", "version_valid_for",
                 "sqlite_version", "signature_damaged")

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
        if len(buf) < HEADER_SIZE:
            raise HeaderError("missing 'SQLite format 3' magic")
        damaged = bytes(buf[:16]) != MAGIC
        if damaged and not signature_only_damaged(buf):
            raise HeaderError("missing 'SQLite format 3' magic")
        h = cls()
        h.signature_damaged = damaged
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
