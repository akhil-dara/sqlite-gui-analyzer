"""Consistency and anti-forensics checks over the header, freelist, WAL, journal and b-trees.

audit(fx) returns Findings: level ('info' | 'warning' | 'error'), a stable machine-readable
code, a one-line message and a details dict (JSON-safe). 'info' reports facts an examiner
records (versions, ids, free space), 'warning' something that changes how the evidence must
be read (hot journal, stale WAL frames, reserved bytes), 'error' a structure SQLite itself
would not have written (inconsistent header fields, broken trees, shared root pages).
Checks are cheap: one pass over the live b-tree pages and the freelist, bounded by a time
limit.
"""

import re
import struct
import time

from ..fileformat.btree import (INDEX_INTERIOR, INDEX_LEAF, TABLE_INTERIOR, TABLE_LEAF,
                                parse_page_header)
from ..fileformat.wal import STALE, UNCOMMITTED
from .pages import MASTER

INFO, WARNING, ERROR = "info", "warning", "error"
_NONZERO = re.compile(b"[^\x00]")
_U16 = struct.Struct(">H")
MAX_LISTED = 20


class Finding(object):
    __slots__ = ("level", "code", "message", "details")

    def __init__(self, level, code, message, details=None):
        self.level, self.code, self.message = level, code, message
        self.details = details or {}

    def as_dict(self):
        return {"level": self.level, "code": self.code, "message": self.message,
                "details": self.details}

    def __repr__(self):
        return "Finding(%s, %s, %r)" % (self.level, self.code, self.message)


def audit(fx, time_limit=30.0, dropped=None):
    out = []
    deadline = time.time() + time_limit if time_limit else None
    s = fx.session
    for check in (_header, _freelist, _wal, _journal, _schema_roots):
        try:
            check(fx, out)
        except Exception as e:          # a hostile file must not stop the audit
            out.append(Finding(WARNING, "AUDIT_CHECK_FAILED", "%s check failed: %s"
                               % (check.__name__.strip("_"), e)))
    try:
        _btrees(fx, out, deadline)
    except Exception as e:
        out.append(Finding(WARNING, "AUDIT_CHECK_FAILED", "b-tree check failed: %s" % e))
    if dropped:
        out.append(Finding(WARNING, "DROPPED_SCHEMA",
                           "%d dropped or redefined schema object(s) recovered" % len(dropped),
                           {"objects": ["%s %s (%s)" % (d.type, d.name, d.status)
                                        for d in dropped[:MAX_LISTED]]}))
    if s.issues.items:
        kinds = {}
        for i in s.issues:
            kinds[i.kind] = kinds.get(i.kind, 0) + 1
        out.append(Finding(INFO, "READ_ISSUES", "%d problem(s) logged while reading the database"
                           % len(s.issues), {"by_kind": kinds}))
    return out


# -- header ------------------------------------------------------------------
def _header(fx, out):
    s = fx.session
    main = fx.main
    h = main.header
    size = s.pager.main_size
    raw = s.pager.main_page(1)[:100] if size >= 100 else b""
    details = {"page_size": h.page_size, "reserved_bytes": h.reserved,
               "text_encoding": h.text_encoding, "schema_format": h.schema_format,
               "user_version": h.user_version, "application_id": h.application_id,
               "application_id_hex": "0x%08x" % h.application_id,
               "sqlite_version_number": h.sqlite_version, "change_counter": h.change_counter,
               "version_valid_for": h.version_valid_for,
               "header_page_count": h.header_page_count, "file_size": size,
               "write_version": h.write_version, "read_version": h.read_version,
               "freelist_count": h.freelist_count, "largest_root_page": h.largest_root,
               "incremental_vacuum": h.incremental_vacuum}
    out.append(Finding(INFO, "HEADER", "page size %d, %s, schema format %d, last written by "
                       "SQLite %s" % (h.page_size, h.encoding, h.schema_format,
                                      _version(h.sqlite_version)), details))
    out.append(Finding(INFO, "APPLICATION_ID", "application_id %d (0x%08x), user_version %d"
                       % (h.application_id, h.application_id, h.user_version),
                       {"application_id": h.application_id, "user_version": h.user_version}))
    if h.reserved:
        out.append(Finding(WARNING, "RESERVED_BYTES",
                           "%d reserved bytes at the end of every page: an encryption or "
                           "checksum extension may have written this file" % h.reserved,
                           {"reserved_bytes": h.reserved}))
    if raw and tuple(bytearray(raw[21:24])) != (64, 32, 32):
        out.append(Finding(ERROR, "PAYLOAD_FRACTIONS",
                           "header bytes 21-23 are %s instead of 64/32/32 (SQLite refuses the file)"
                           % "/".join(str(b) for b in bytearray(raw[21:24]))))
    # both are 0 until the first object is created: only a problem once a schema exists
    empty = not s.schema.entries
    if h.schema_format not in (1, 2, 3, 4) and not (empty and h.schema_format == 0):
        out.append(Finding(WARNING, "SCHEMA_FORMAT", "schema format number %d is not 1-4"
                           % h.schema_format))
    if h.text_encoding not in (1, 2, 3) and not (empty and h.text_encoding == 0):
        out.append(Finding(WARNING, "TEXT_ENCODING", "text encoding %d is not 1-3 (UTF-8 assumed)"
                           % h.text_encoding))
    file_pages = size // h.page_size
    if size % h.page_size:
        out.append(Finding(WARNING, "FILE_SIZE", "file size %d is not a multiple of the page size"
                           % size, {"remainder": size % h.page_size}))
    if h.version_valid_for != h.change_counter:
        out.append(Finding(INFO, "HEADER_PAGE_COUNT_STALE",
                           "in-header page count not valid (version-valid-for %d != change "
                           "counter %d): page count taken from the file size"
                           % (h.version_valid_for, h.change_counter)))
    elif h.header_page_count and h.header_page_count != file_pages:
        more = file_pages > h.header_page_count
        out.append(Finding(WARNING if more else ERROR, "HEADER_PAGE_COUNT",
                           "header says %d pages, the file holds %d%s"
                           % (h.header_page_count, file_pages,
                              " (data beyond the database end)" if more else " (file truncated)"),
                           {"header_pages": h.header_page_count, "file_pages": file_pages,
                            "extra_bytes": max(0, size - h.header_page_count * h.page_size)}))
    if h.incremental_vacuum and not h.largest_root:
        out.append(Finding(WARNING, "INCREMENTAL_VACUUM",
                           "incremental-vacuum flag set without auto-vacuum"))
    if h.largest_root:
        mode = "incremental" if h.incremental_vacuum else "full"
        from .. import limits
        import itertools
        ptrmap = fx.eff_map.ptrmap
        cap = limits.get("audit_ptrmap_pages")
        ptr = list(itertools.islice(iter(ptrmap), max(cap, MAX_LISTED)))
        bad = [p for p in ptr[:cap] if s.pager.page_count >= p and
               fx.main.safe_page(p) is not None and fx.main.page(p)[0] not in (0, 1, 2, 3, 4, 5)]
        checked = ("" if len(ptrmap) <= cap else
                   " (the first %s were checked: limit audit_ptrmap_pages)" % format(cap, ","))
        out.append(Finding(INFO, "AUTO_VACUUM",
                           "auto_vacuum=%s: %d pointer-map page(s)%s; freed pages are %s"
                           % (mode, len(ptrmap), checked,
                              "moved to the end and truncated at every commit"
                              if mode == "full" else "kept until an incremental vacuum"),
                           {"mode": mode, "ptrmap_pages": ptr[:MAX_LISTED],
                            "ptrmap_pages_checked": min(len(ptrmap), cap)}))
        if bad:
            out.append(Finding(ERROR, "PTRMAP", "%d pointer-map page(s) hold invalid entry types%s"
                               % (len(bad), checked), {"pages": bad[:MAX_LISTED]}))
    wal_header = h.write_version == 2 or h.read_version == 2
    if s.wal is not None and not wal_header:
        out.append(Finding(WARNING, "WAL_WITH_ROLLBACK_HEADER",
                           "a -wal file exists but the header says rollback-journal mode: SQLite "
                           "would not read the WAL (its frames are shown here anyway)"))
    elif wal_header and s.evidence.path("wal") is None:
        out.append(Finding(INFO, "WAL_MODE_NO_WAL",
                           "header is in WAL mode but there is no -wal file (normal after a "
                           "clean close; the WAL may not have been collected)"))


def _version(n):
    if not n:
        return "unknown"
    return "%d.%d.%d" % (n // 1000000, (n // 1000) % 1000, n % 1000)


# -- freelist ----------------------------------------------------------------
def _freelist(fx, out):
    pmap = fx.eff_map
    h = fx.session.pager.header
    walked = len(pmap.trunks) + len(pmap.leaves)
    if walked != h.freelist_count:
        out.append(Finding(WARNING, "FREELIST_COUNT",
                           "header freelist count %d, the freelist chain holds %d page(s)"
                           % (h.freelist_count, walked),
                           {"header": h.freelist_count, "walked": walked}))
    if not walked:
        return
    zero, nonzero = [], []
    for n in sorted(pmap.leaves):
        data = fx.session.pager.page(n)
        (nonzero if _NONZERO.search(data, 0, fx.session.pager.usable_size) else zero).append(n)
    out.append(Finding(INFO, "FREELIST", "%d free page(s): %d trunk, %d leaf (%d all zero)"
                       % (walked, len(pmap.trunks), len(pmap.leaves), len(zero)),
                       {"trunks": sorted(pmap.trunks)[:MAX_LISTED],
                        "leaves_nonzero": nonzero[:MAX_LISTED], "leaves_zero": len(zero)}))
    if pmap.leaves and not nonzero:
        out.append(Finding(WARNING, "FREELIST_ZEROED",
                           "every freelist leaf page is zero-filled: secure_delete was likely on "
                           "(deleted content was overwritten)", {"pages": len(zero)}))


# -- WAL -----------------------------------------------------------------------
def _wal(fx, out):
    s = fx.session
    if s.wal_problem:
        out.append(Finding(WARNING, "WAL_UNREADABLE", "the -wal file could not be read: %s"
                           % s.wal_problem))
    wal = s.wal
    if wal is None:
        return
    h = wal.header
    counts = wal.state_counts()
    out.append(Finding(INFO, "WAL", "WAL: %d frame(s), %d commit(s), checkpoint sequence %d, "
                       "salts %08x/%08x" % (len(wal.frames), wal.commit_count, h.checkpoint_seq,
                                            h.salt1, h.salt2),
                       {"frames": len(wal.frames), "commits": wal.commit_count,
                        "checkpoint_seq": h.checkpoint_seq, "salt1": h.salt1, "salt2": h.salt2,
                        "page_size": h.page_size, "big_endian_checksums": h.big_endian_words,
                        "states": counts, "db_size_pages": wal.db_size_pages}))
    if not h.checksum_ok:
        out.append(Finding(ERROR, "WAL_HEADER_CHECKSUM", "WAL header checksum does not verify: "
                           "SQLite would ignore every frame"))
    if counts.get(STALE):
        salts = sorted(set((f.salt1, f.salt2) for f in wal.frames if f.state == STALE))
        out.append(Finding(WARNING, "WAL_STALE_FRAMES",
                           "%d frame(s) from %d earlier WAL generation(s): older page versions "
                           "SQLite no longer reads" % (counts[STALE], len(salts)),
                           {"frames": counts[STALE],
                            "salts": ["%08x/%08x" % s_ for s_ in salts[:MAX_LISTED]]}))
    if counts.get(UNCOMMITTED):
        out.append(Finding(WARNING, "WAL_UNCOMMITTED_FRAMES",
                           "%d frame(s) after the last commit: a transaction in progress or "
                           "rolled back" % counts[UNCOMMITTED], {"frames": counts[UNCOMMITTED]}))
    bad = [f.index for f in wal.frames if f.checksum_ok is False and f.state != UNCOMMITTED]
    if bad:
        out.append(Finding(WARNING, "WAL_CHECKSUM", "%d committed-generation frame(s) fail their "
                           "checksum" % len(bad), {"frames": bad[:MAX_LISTED]}))
    main_pages = fx.main.page_count
    if wal.db_size_pages and wal.db_size_pages != main_pages:
        out.append(Finding(INFO, "WAL_DB_SIZE", "after the last WAL commit the database has %d "
                           "page(s); the main file %d" % (wal.db_size_pages, main_pages),
                           {"wal": wal.db_size_pages, "main": main_pages}))


# -- journal -------------------------------------------------------------------
def _journal(fx, out):
    j = fx.journal()
    path = fx.session.evidence.path("journal")
    if j is None:
        if path:
            out.append(Finding(INFO, "JOURNAL_EMPTY", "a -journal file exists but holds no page "
                               "records"))
        return
    info = j.summary()
    if j.hot:
        out.append(Finding(WARNING, "HOT_JOURNAL",
                           "hot rollback journal: the main file may hold part of an interrupted "
                           "transaction; %d page(s) of pre-transaction content available"
                           % len(j.pages()), info))
    else:
        out.append(Finding(INFO, "JOURNAL_RESIDUE",
                           "rollback journal with %s header still holds %d page record(s): the "
                           "content before the last transaction"
                           % ("a zeroed" if j.header_zeroed else "no valid", len(j.records)), info))
    bad = [r.page_no for r in j.records if not r.checksum_ok]
    if bad:
        out.append(Finding(WARNING, "JOURNAL_CHECKSUM", "%d journal record(s) fail their checksum"
                           % len(bad), {"pages": bad[:MAX_LISTED]}))


# -- schema / b-trees ------------------------------------------------------------
def _schema_roots(fx, out):
    s = fx.session
    count = s.pager.page_count
    by_root = {}
    for e in s.schema.entries:
        if e.type not in ("table", "index") or not e.rootpage:
            continue
        by_root.setdefault(e.rootpage, []).append(e.name)
        if e.rootpage < 0 or e.rootpage > count:
            out.append(Finding(ERROR, "ROOT_PAGE_RANGE", "%s %s: root page %d outside 1..%d"
                               % (e.type, e.name, e.rootpage, count)))
            continue
        try:
            t = parse_page_header(s.pager.page(e.rootpage), e.rootpage).type
        except Exception:
            out.append(Finding(ERROR, "ROOT_PAGE_TYPE", "%s %s: root page %d is not a b-tree page"
                               % (e.type, e.name, e.rootpage)))
            continue
        info = s.schema.get(e.name)
        index_tree = e.type == "index" or (info is not None and info.without_rowid)
        if (t in (INDEX_LEAF, INDEX_INTERIOR)) != index_tree:
            out.append(Finding(ERROR, "ROOT_PAGE_TYPE", "%s %s: root page %d is a %s page"
                               % (e.type, e.name, e.rootpage,
                                  "index" if t in (INDEX_LEAF, INDEX_INTERIOR) else "table")))
    for root, names in sorted(by_root.items()):
        if len(names) > 1:
            out.append(Finding(ERROR, "ROOT_PAGE_SHARED", "objects %s share root page %d"
                               % (", ".join(names), root), {"root": root, "objects": names}))
    try:
        t1 = parse_page_header(s.pager.page(1), 1).type
        if t1 not in (TABLE_LEAF, TABLE_INTERIOR):
            out.append(Finding(ERROR, "PAGE1_TYPE", "page 1 is not a table b-tree page (0x%02x)"
                               % t1))
    except Exception as e:
        out.append(Finding(ERROR, "PAGE1_TYPE", "page 1 has no b-tree header: %s" % e))


def _btrees(fx, out, deadline):
    s = fx.session
    usable = s.pager.usable_size
    anomalies = {}
    free_bytes, free_pages, zero_blocks, data_blocks = 0, 0, 0, 0
    pages = sorted(fx.eff_map.owner)
    done = 0
    for n in pages:
        if deadline is not None and done % 256 == 0 and time.time() > deadline:
            break
        done += 1
        try:
            data = s.pager.page(n)
            h = parse_page_header(data, n)
        except Exception:
            anomalies.setdefault("not a b-tree page", []).append(n)
            continue
        ptr_end = h.ptr_start + 2 * h.cell_count
        if ptr_end > usable:
            anomalies.setdefault("cell pointer array runs past the page", []).append(n)
            continue
        if h.cell_count and h.content_start < ptr_end:
            anomalies.setdefault("content area overlaps the cell pointers", []).append(n)
        if h.fragmented > 60:
            anomalies.setdefault("more than 60 fragmented bytes", []).append(n)
        for i in range(h.cell_count):
            off = _U16.unpack_from(data, h.ptr_start + 2 * i)[0]
            if off < h.content_start or off > usable - 4:
                anomalies.setdefault("cell pointer outside the content area", []).append(n)
                break
        page_free = 0
        fb, prev, hops = h.first_freeblock, 0, 0
        while fb and hops < usable // 4:
            hops += 1
            if fb <= prev or fb < ptr_end or fb > usable - 4:
                anomalies.setdefault("freeblock chain out of order or range", []).append(n)
                break
            size = _U16.unpack_from(data, fb + 2)[0]
            if size < 4 or fb + size > usable:
                anomalies.setdefault("freeblock with an impossible size", []).append(n)
                break
            if _NONZERO.search(data, fb + 4, fb + size):
                data_blocks += 1
                page_free += size
            else:
                zero_blocks += 1
            prev, fb = fb, _U16.unpack_from(data, fb)[0]
        gap_end = min(h.content_start, usable)
        if gap_end > ptr_end and _NONZERO.search(data, ptr_end, gap_end):
            page_free += gap_end - ptr_end
        if page_free:
            free_pages += 1
            free_bytes += page_free
    if done < len(pages):
        out.append(Finding(INFO, "BTREE_SCAN_PARTIAL", "b-tree check stopped at the time limit "
                           "after %d of %d page(s)" % (done, len(pages))))
    for what, pgs in sorted(anomalies.items()):
        out.append(Finding(ERROR, "BTREE_ANOMALY", "%d page(s): %s" % (len(pgs), what),
                           {"pages": pgs[:MAX_LISTED]}))
    if free_pages:
        out.append(Finding(INFO, "FREE_SPACE", "%d byte(s) of non-zero free space in %d live "
                           "page(s) may hold deleted records" % (free_bytes, free_pages),
                           {"bytes": free_bytes, "pages": free_pages,
                            "freeblocks_with_data": data_blocks, "zeroed_freeblocks": zero_blocks}))
    if zero_blocks >= 3 and not data_blocks:
        out.append(Finding(WARNING, "SECURE_DELETE_LIKELY",
                           "all %d freeblocks are zero-filled: secure_delete was likely on"
                           % zero_blocks))
    names = set(name for name, _k in fx.eff_map.owner.values())
    if MASTER not in names:
        out.append(Finding(ERROR, "MASTER_UNREADABLE", "sqlite_master could not be walked"))
