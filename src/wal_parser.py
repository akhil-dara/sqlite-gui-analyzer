"""WAL (Write-Ahead Log) binary parser for SQLite forensic analysis.

Reads the raw .db-wal file directly using mmap — no sqlite3 dependency.
Extracts frame headers, classifies frames (committed / uncommitted / old),
parses b-tree leaf pages to recover cell records, and supports full-text
search across all WAL data.

WAL file layout:
    [32-byte WAL header]
    [24-byte frame header + page_size bytes of page data] × N frames

References:
    https://www.sqlite.org/walformat.html
    https://www.sqlite.org/fileformat2.html (b-tree page format, varint, serial types)
"""

import mmap
import os
import re
import struct
from collections import namedtuple

from constants import (WAL_MAGIC_BE, WAL_MAGIC_LE, WAL_HEADER_SIZE,
                       WAL_FRAME_HEADER_SIZE, PAGE_TYPES)


# ── Data structures ──────────────────────────────────────────────────────

WALHeader = namedtuple("WALHeader", [
    "magic", "version", "page_size", "checkpoint_seq",
    "salt1", "salt2", "checksum1", "checksum2",
])

WALFrame = namedtuple("WALFrame", [
    "index",          # 0-based frame index
    "offset",         # byte offset of frame header in WAL file
    "page_num",       # database page number this frame overwrites
    "commit_size",    # >0 on last frame of a committed transaction
    "salt1", "salt2", # frame salt values
    "checksum1", "checksum2",
    "category",       # 'committed' | 'uncommitted' | 'old'
    "page_type",      # human-readable page type string
    "page_type_byte", # raw first byte of page data
])


# ── Varint / serial type helpers ─────────────────────────────────────────

def _read_varint(data, offset):
    """Read a SQLite varint (1-9 bytes, MSB continuation bit).

    Returns (value, new_offset).  Raises ValueError on truncated data.
    """
    result = 0
    for i in range(9):
        if offset >= len(data):
            raise ValueError("Truncated varint")
        b = data[offset]
        offset += 1
        if i < 8:
            result = (result << 7) | (b & 0x7F)
            if b < 0x80:
                return (result, offset)
        else:
            # 9th byte: all 8 bits are payload
            result = (result << 8) | b
            return (result, offset)
    return (result, offset)


def _serial_type_size(st):
    """Return the byte-length of a value with the given serial type code."""
    if st == 0:
        return 0      # NULL
    if st == 1:
        return 1      # 8-bit int
    if st == 2:
        return 2      # 16-bit int
    if st == 3:
        return 3      # 24-bit int
    if st == 4:
        return 4      # 32-bit int
    if st == 5:
        return 6      # 48-bit int
    if st == 6:
        return 8      # 64-bit int
    if st == 7:
        return 8      # IEEE 754 float
    if st == 8:
        return 0      # integer 0
    if st == 9:
        return 0      # integer 1
    if st >= 12 and st % 2 == 0:
        return (st - 12) // 2   # BLOB
    if st >= 13 and st % 2 == 1:
        return (st - 13) // 2   # TEXT
    return 0


def _read_serial_value(data, offset, serial_type):
    """Decode a value from data at offset given its serial type.

    Returns (value, new_offset).
    """
    if serial_type == 0:
        return (None, offset)
    if serial_type == 8:
        return (0, offset)
    if serial_type == 9:
        return (1, offset)

    sz = _serial_type_size(serial_type)
    if offset + sz > len(data):
        raise ValueError(f"Truncated value: need {sz} bytes at offset {offset}")

    chunk = data[offset:offset + sz]

    if serial_type in (1, 2, 3, 4, 5, 6):
        # Signed big-endian integer
        val = int.from_bytes(chunk, "big", signed=True)
        return (val, offset + sz)

    if serial_type == 7:
        val = struct.unpack(">d", chunk)[0]
        return (val, offset + sz)

    if serial_type >= 12 and serial_type % 2 == 0:
        # BLOB
        return (bytes(chunk), offset + sz)

    if serial_type >= 13 and serial_type % 2 == 1:
        # TEXT (UTF-8)
        try:
            val = bytes(chunk).decode("utf-8", errors="replace")
        except Exception:
            val = bytes(chunk).decode("latin-1", errors="replace")
        return (val, offset + sz)

    return (None, offset + sz)


def _parse_record(data, offset):
    """Parse a full SQLite record at the given offset.

    A record is: header_length (varint), serial_type1 (varint), ...,
    followed by the values.

    Returns list of decoded values.
    """
    start = offset
    header_len, offset = _read_varint(data, offset)
    header_end = start + header_len

    # Read serial types
    serial_types = []
    while offset < header_end:
        st, offset = _read_varint(data, offset)
        serial_types.append(st)

    # Ensure we're at header_end
    offset = header_end

    # Read values
    values = []
    for st in serial_types:
        val, offset = _read_serial_value(data, offset, st)
        values.append(val)

    return values


def _identify_page_type(page_data):
    """Return (page_type_byte, human_label) for a page."""
    if not page_data or len(page_data) < 1:
        return (0, "Unknown")
    pt = page_data[0]
    label = PAGE_TYPES.get(pt, f"Unknown (0x{pt:02X})")
    return (pt, label)


# ── WALParser class ──────────────────────────────────────────────────────

class WALParser:
    """Pure binary WAL file parser using memory-mapped I/O."""

    def __init__(self):
        self._path = None
        self._f = None
        self._mm = None
        self.header = None
        self.frames = []
        self.page_size = 0
        self._valid = False
        self._file_size = 0
        # Set by DB._build_wal_page_map() after open
        self.page_map = {}   # page_num → table_name
        self.col_map = {}    # table_name → [col_name, ...]
        self.pk_col_idx = {} # table_name → column index of INTEGER PRIMARY KEY

    # ── open / close ─────────────────────────────────────────────────

    def open(self, db_path):
        """Open WAL file for the given database path (appends '-wal').

        Silently returns if the WAL file doesn't exist or is too small.
        """
        wal_path = db_path + "-wal"
        self.open_wal_file(wal_path, db_path)

    def open_wal_file(self, wal_path, db_path=None):
        """Open a specific WAL file directly.

        Used for forensic backup copies where the WAL path doesn't match
        the database path. ``db_path`` is stored for reference only.
        """
        self.close()
        if not os.path.isfile(wal_path):
            return
        fsize = os.path.getsize(wal_path)
        if fsize < WAL_HEADER_SIZE:
            return

        try:
            self._f = open(wal_path, "rb")
            self._file_size = fsize
            self._path = wal_path

            # Memory-map the entire file (read-only)
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)

            self._parse_header()
            if not self._valid:
                self.close()
                return
            self._parse_frames()
        except Exception:
            self.close()

    def close(self):
        """Release resources."""
        if self._mm:
            try:
                self._mm.close()
            except Exception:
                pass
            self._mm = None
        if self._f:
            try:
                self._f.close()
            except Exception:
                pass
            self._f = None
        self._path = None
        self.header = None
        self.frames = []
        self.page_size = 0
        self._valid = False
        self._file_size = 0

    @property
    def valid(self):
        return self._valid

    @property
    def path(self):
        return self._path

    # ── header parsing ───────────────────────────────────────────────

    def _parse_header(self):
        """Parse the 32-byte WAL header."""
        mm = self._mm
        if len(mm) < WAL_HEADER_SIZE:
            return

        magic = struct.unpack(">I", mm[0:4])[0]
        if magic not in (WAL_MAGIC_BE, WAL_MAGIC_LE):
            return

        # Determine byte order from magic number
        self._big_endian = (magic == WAL_MAGIC_BE)
        bo = ">" if self._big_endian else "<"

        version, page_size, ckpt_seq = struct.unpack(f"{bo}III", mm[4:16])
        salt1, salt2 = struct.unpack(f"{bo}II", mm[16:24])
        cksum1, cksum2 = struct.unpack(f"{bo}II", mm[24:32])

        self.header = WALHeader(
            magic=magic, version=version, page_size=page_size,
            checkpoint_seq=ckpt_seq, salt1=salt1, salt2=salt2,
            checksum1=cksum1, checksum2=cksum2,
        )
        self.page_size = page_size
        self._valid = True

    # ── frame parsing ────────────────────────────────────────────────

    def _parse_frames(self):
        """Parse all frame headers from the WAL file.

        Only reads the 24-byte frame headers (not full page data) — fast.
        Page data is read lazily via get_page_data().
        """
        mm = self._mm
        ps = self.page_size
        frame_total_size = WAL_FRAME_HEADER_SIZE + ps
        offset = WAL_HEADER_SIZE
        bo = ">" if self._big_endian else "<"
        hdr_salt1 = self.header.salt1
        hdr_salt2 = self.header.salt2
        idx = 0
        frames = []

        while offset + frame_total_size <= len(mm):
            # Parse 24-byte frame header
            page_num, commit_size, f_salt1, f_salt2, cksum1, cksum2 = \
                struct.unpack(f"{bo}IIIIII", mm[offset:offset + WAL_FRAME_HEADER_SIZE])

            # Classify
            salt_match = (f_salt1 == hdr_salt1 and f_salt2 == hdr_salt2)
            if salt_match and commit_size > 0:
                category = "committed"
            elif salt_match:
                category = "uncommitted"
            else:
                category = "old"

            # Identify page type from first byte of page data
            page_data_offset = offset + WAL_FRAME_HEADER_SIZE
            pt_byte, pt_label = _identify_page_type(mm[page_data_offset:page_data_offset + 1])

            frames.append(WALFrame(
                index=idx, offset=offset, page_num=page_num,
                commit_size=commit_size, salt1=f_salt1, salt2=f_salt2,
                checksum1=cksum1, checksum2=cksum2,
                category=category, page_type=pt_label,
                page_type_byte=pt_byte,
            ))

            offset += frame_total_size
            idx += 1

        self.frames = frames

    # ── page data access ─────────────────────────────────────────────

    def get_page_data(self, frame_index):
        """Return raw page bytes for the given frame index.

        Uses memoryview slice of the mmap — zero-copy.
        """
        if not self._valid or frame_index < 0 or frame_index >= len(self.frames):
            return b""
        frame = self.frames[frame_index]
        start = frame.offset + WAL_FRAME_HEADER_SIZE
        end = start + self.page_size
        if end > len(self._mm):
            return b""
        return bytes(self._mm[start:end])

    # ── b-tree page parsing ──────────────────────────────────────────

    def parse_btree_page(self, page_data):
        """Parse b-tree page header.

        Returns dict: {page_type, page_type_byte, cell_count, cell_offsets,
                       first_free, frag_count, right_child (interior only)}
        """
        if not page_data or len(page_data) < 8:
            return None

        pt = page_data[0]
        if pt not in PAGE_TYPES or pt == 0:
            return None

        # Header size: 8 bytes for leaf pages, 12 for interior pages
        is_interior = pt in (0x02, 0x05)
        hdr_size = 12 if is_interior else 8

        if len(page_data) < hdr_size:
            return None

        first_free = int.from_bytes(page_data[1:3], "big")
        cell_count = int.from_bytes(page_data[3:5], "big")
        cell_content_start = int.from_bytes(page_data[5:7], "big")
        frag_count = page_data[7]

        right_child = None
        if is_interior:
            right_child = int.from_bytes(page_data[8:12], "big")

        # Cell pointer array follows header
        ptr_start = hdr_size
        cell_offsets = []
        for i in range(cell_count):
            po = ptr_start + i * 2
            if po + 2 > len(page_data):
                break
            cell_offsets.append(int.from_bytes(page_data[po:po + 2], "big"))

        return {
            "page_type": PAGE_TYPES.get(pt, f"Unknown (0x{pt:02X})"),
            "page_type_byte": pt,
            "cell_count": cell_count,
            "cell_offsets": cell_offsets,
            "first_free": first_free,
            "frag_count": frag_count,
            "right_child": right_child,
            "cell_content_start": cell_content_start,
        }

    def parse_leaf_cells(self, page_data):
        """Parse all cells from a table leaf page (type 0x0D).

        Returns list of dicts: {rowid: int, values: [v1, v2, ...]}
        Returns empty list if page is not a table leaf or parsing fails.
        """
        if not page_data or len(page_data) < 8:
            return []
        if page_data[0] != 0x0D:
            return []

        info = self.parse_btree_page(page_data)
        if not info:
            return []

        cells = []
        for cell_offset in info["cell_offsets"]:
            try:
                if cell_offset >= len(page_data):
                    continue
                off = cell_offset
                # payload_len (varint)
                payload_len, off = _read_varint(page_data, off)
                # rowid (varint)
                rowid, off = _read_varint(page_data, off)

                # Check for overflow: if payload fits on this page, parse inline
                # Usable size = page_size - reserved (usually 0)
                # Max local payload for table leaf: usable - 35
                # If payload > max local, only part is inline, rest is overflow
                # For simplicity, parse what's available on this page
                payload_start = off
                payload_end = min(payload_start + payload_len, len(page_data))
                if payload_end <= payload_start:
                    continue

                payload = page_data[payload_start:payload_end]
                values = _parse_record(payload, 0)
                cells.append({"rowid": rowid, "values": values})
            except Exception:
                # Skip corrupt/unparseable cells
                continue

        return cells

    def parse_page1_cells(self, page_data):
        """Parse cells from page 1 which has a 100-byte DB header prefix.

        Page 1 is special: bytes 0-99 are the database file header,
        and the B-tree header starts at offset 100.
        Returns list of dicts like parse_leaf_cells.
        """
        if not page_data or len(page_data) < 108:
            return []
        # Check btree type at offset 100
        pt = page_data[100]
        if pt != 0x0D:  # Must be table leaf
            return []

        # Parse header at offset 100
        cell_count = int.from_bytes(page_data[103:105], "big")
        # Cell pointer array starts at offset 108 (100 + 8 byte leaf header)
        cell_offsets = []
        for i in range(cell_count):
            po = 108 + i * 2
            if po + 2 > len(page_data):
                break
            cell_offsets.append(int.from_bytes(page_data[po:po + 2], "big"))

        cells = []
        for cell_offset in cell_offsets:
            try:
                if cell_offset >= len(page_data):
                    continue
                off = cell_offset
                payload_len, off = _read_varint(page_data, off)
                rowid, off = _read_varint(page_data, off)
                payload_start = off
                payload_end = min(payload_start + payload_len, len(page_data))
                if payload_end <= payload_start:
                    continue
                payload = page_data[payload_start:payload_end]
                values = _parse_record(payload, 0)
                cells.append({"rowid": rowid, "values": values})
            except Exception:
                continue
        return cells

    # ── search ───────────────────────────────────────────────────────

    def search(self, term, mode, limit=999999, cancel=None):
        """Search all WAL table leaf pages for the given term.

        Yields dicts: {table: str, column: str, rowid, value, type, source,
                       frame_idx, page_num, category}

        Only searches table leaf pages (0x0D) — other page types don't
        contain user row data.
        """
        if not self._valid or not term:
            return

        # Pre-compile regex if needed
        rx = None
        if mode == "Regex":
            try:
                rx = re.compile(term)
            except re.error:
                return

        term_lower = term.lower()
        found = 0

        for frame in self.frames:
            if cancel and cancel():
                return
            if found >= limit:
                return

            # Only table leaf pages contain row data
            if frame.page_type_byte != 0x0D:
                continue
            # Skip sqlite_master / schema pages
            if frame.page_num == 1:
                continue

            try:
                page_data = self.get_page_data(frame.index)
                cells = self.parse_leaf_cells(page_data)
            except Exception:
                continue

            # Resolve table name and column names from page map
            table_name = self.page_map.get(frame.page_num,
                                            f"page_{frame.page_num}")
            # Skip system tables
            if table_name in ("sqlite_master", "sqlite_sequence"):
                continue
            col_names = self.col_map.get(table_name, [])
            # INTEGER PRIMARY KEY column index (value = rowid, stored as NULL)
            pk_idx = self.pk_col_idx.get(table_name, -1)

            for cell in cells:
                if cancel and cancel():
                    return
                if found >= limit:
                    return

                rowid = cell["rowid"]

                # Build complete row dict — FULL values, no truncation
                row_values = {}
                for vi, v in enumerate(cell["values"]):
                    cname = col_names[vi] if vi < len(col_names) else f"col{vi}"
                    # INTEGER PRIMARY KEY: SQLite stores rowid separately,
                    # the record payload has NULL — substitute the real rowid
                    if vi == pk_idx and v is None:
                        row_values[cname] = str(rowid)
                    elif v is None:
                        row_values[cname] = "NULL"
                    elif isinstance(v, bytes):
                        from utils import blob_type as _bt, fmtb as _fb
                        row_values[cname] = f"[BLOB: {_fb(len(v))}, {_bt(v)}]"
                    elif isinstance(v, float):
                        row_values[cname] = f"{v:.6g}"
                    else:
                        row_values[cname] = str(v)  # Full value, no truncation

                for ci, val in enumerate(cell["values"]):
                    # For PK column, treat as rowid value for matching
                    if ci == pk_idx and val is None:
                        val = rowid
                    if val is None:
                        continue
                    if isinstance(val, bytes):
                        # Skip raw BLOB data in search (like main search does)
                        continue

                    s = str(val)
                    matched = False

                    if mode == "Case-Insensitive":
                        matched = term_lower in s.lower()
                    elif mode == "Case-Sensitive":
                        matched = term in s
                    elif mode == "Exact Match":
                        matched = s == term
                    elif mode == "Starts With":
                        matched = s.lower().startswith(term_lower)
                    elif mode == "Ends With":
                        matched = s.lower().endswith(term_lower)
                    elif mode == "Regex":
                        matched = bool(rx.search(s))
                    else:
                        # Default: case-insensitive
                        matched = term_lower in s.lower()

                    if matched:
                        found += 1
                        # Truncate for search result display only
                        display_val = s if len(s) <= 500 else s[:500] + "..."
                        dt = "text" if isinstance(val, str) else \
                             "integer" if isinstance(val, int) else \
                             "real" if isinstance(val, float) else "text"
                        col_name = col_names[ci] if ci < len(col_names) \
                                   else f"col{ci}"
                        yield {
                            "table": table_name,
                            "column": col_name,
                            "rowid": rowid,
                            "value": display_val,
                            "type": dt,
                            "source": "WAL ({})".format(
                                {"committed": "Saved",
                                 "uncommitted": "Unsaved",
                                 "old": "Overwritten"}.get(
                                    frame.category, frame.category)),
                            "frame_idx": frame.index,
                            "page_num": frame.page_num,
                            "category": frame.category,
                            "row_data": row_values,
                        }
                        if found >= limit:
                            return

    # ── bulk recovery ────────────────────────────────────────────────

    def recover_all_records(self, table_filter=None, category_filter=None,
                            cancel=None, include_schema=False):
        """Recover ALL records from WAL table leaf pages.

        Unlike ``search()``, this yields every record — no term filtering.
        Used for the All-Records browser, WAL-only table browsing, full
        forensic export, and BLOB extraction.

        Parameters
        ----------
        table_filter : str or None
            Only yield records belonging to this table.
        category_filter : str or None
            Only yield records from frames of this category
            ('committed', 'uncommitted', 'old').
        cancel : callable or None
            Return True to abort early.
        include_schema : bool
            If False (default), skip sqlite_master records (CREATE TABLE
            statements etc.) which are schema metadata, not user data.

        Yields
        ------
        dict with keys:
            table, rowid, values_dict, raw_values, frame_idx,
            page_num, category
        """
        if not self._valid:
            return

        for frame in self.frames:
            if cancel and cancel():
                return
            if frame.page_type_byte != 0x0D:
                continue
            if category_filter and frame.category != category_filter:
                continue

            table_name = self.page_map.get(frame.page_num,
                                           f"page_{frame.page_num}")

            # Skip system tables / schema pages unless explicitly requested
            if not include_schema:
                if (table_name in ("sqlite_master", "sqlite_sequence")
                        or frame.page_num == 1):
                    continue

            if table_filter and table_name != table_filter:
                continue

            try:
                page_data = self.get_page_data(frame.index)
                cells = self.parse_leaf_cells(page_data)
            except Exception:
                continue

            col_names = self.col_map.get(table_name, [])
            pk_idx = self.pk_col_idx.get(table_name, -1)

            # Detect misidentified sqlite_master pages by checking cell content.
            # sqlite_master records have 5 columns where first is type string
            # ("table", "index", "view", "trigger") and 5th is CREATE SQL.
            is_schema_page = False
            if not include_schema and cells:
                first_vals = cells[0].get("values", [])
                if (len(first_vals) >= 5
                        and isinstance(first_vals[0], str)
                        and first_vals[0] in ("table", "index", "view", "trigger")
                        and isinstance(first_vals[4], str)
                        and first_vals[4].strip().upper().startswith("CREATE")):
                    is_schema_page = True
            if is_schema_page:
                continue

            for cell in cells:
                if cancel and cancel():
                    return
                rowid = cell["rowid"]
                values_dict = {}
                for vi, v in enumerate(cell["values"]):
                    cname = (col_names[vi] if vi < len(col_names)
                             else f"col{vi}")
                    if vi == pk_idx and v is None:
                        values_dict[cname] = str(rowid)
                    elif v is None:
                        values_dict[cname] = "NULL"
                    elif isinstance(v, bytes):
                        from utils import blob_type as _bt, fmtb as _fb
                        values_dict[cname] = f"[BLOB: {_fb(len(v))}, {_bt(v)}]"
                    elif isinstance(v, float):
                        values_dict[cname] = f"{v:.6g}"
                    else:
                        values_dict[cname] = str(v)

                yield {
                    "table": table_name,
                    "rowid": rowid,
                    "values_dict": values_dict,
                    "raw_values": cell["values"],
                    "frame_idx": frame.index,
                    "page_num": frame.page_num,
                    "category": frame.category,
                }

    # ── summary / analytics ──────────────────────────────────────────

    def summary(self):
        """Return summary statistics about the WAL file."""
        if not self._valid:
            return {}

        cats = {"committed": 0, "uncommitted": 0, "old": 0}
        page_types = {}
        unique_pages = set()

        for f in self.frames:
            cats[f.category] = cats.get(f.category, 0) + 1
            page_types[f.page_type] = page_types.get(f.page_type, 0) + 1
            unique_pages.add(f.page_num)

        return {
            "total_frames": len(self.frames),
            "committed": cats["committed"],
            "uncommitted": cats["uncommitted"],
            "old": cats["old"],
            "unique_pages": len(unique_pages),
            "page_types": page_types,
            "wal_size": self._file_size,
            "page_size": self.page_size,
            "checkpoint_seq": self.header.checkpoint_seq if self.header else 0,
            "header_salt1": self.header.salt1 if self.header else 0,
            "header_salt2": self.header.salt2 if self.header else 0,
        }

    def table_stats(self):
        """Return per-table statistics from WAL frames.

        Only counts records from table leaf pages (0x0D).
        Uses page header cell_count for speed (no full cell parsing).

        Returns
        -------
        dict : {table_name: {total_records, committed, uncommitted,
                old, frames, pages: set, is_wal_only: bool}}
        """
        if not self._valid:
            return {}

        stats = {}
        for frame in self.frames:
            if frame.page_type_byte != 0x0D:
                continue
            table_name = self.page_map.get(frame.page_num)
            # Skip system tables, unmapped pages, schema pages
            if (not table_name or table_name in ("sqlite_master", "sqlite_sequence")
                    or frame.page_num == 1):
                continue
            if table_name not in stats:
                stats[table_name] = {
                    "total_records": 0,
                    "committed": 0,
                    "uncommitted": 0,
                    "old": 0,
                    "frames": 0,
                    "pages": set(),
                    "is_wal_only": False,
                }
            s = stats[table_name]
            s["frames"] += 1
            s["pages"].add(frame.page_num)

            try:
                page_data = self.get_page_data(frame.index)
                if page_data and len(page_data) >= 5:
                    cell_count = int.from_bytes(page_data[3:5], "big")
                    s["total_records"] += cell_count
                    s[frame.category] += cell_count
            except Exception:
                pass

        return stats

    def transaction_groups(self):
        """Group frames into transactions.

        A transaction = consecutive frames with the same salt values,
        ending with a frame that has commit_size > 0.

        Returns list of dicts: {start_frame, end_frame, frame_count,
        pages, committed, salt1, salt2}
        """
        if not self._valid or not self.frames:
            return []

        groups = []
        current = {
            "start_frame": 0,
            "frames": [self.frames[0]],
            "salt1": self.frames[0].salt1,
            "salt2": self.frames[0].salt2,
        }

        for i in range(1, len(self.frames)):
            f = self.frames[i]
            # Same transaction if salt matches
            if f.salt1 == current["salt1"] and f.salt2 == current["salt2"]:
                current["frames"].append(f)
            else:
                # Salt changed — finalize current group, start new one
                grp = current["frames"]
                groups.append({
                    "start_frame": current["start_frame"],
                    "end_frame": grp[-1].index,
                    "frame_count": len(grp),
                    "pages": sorted(set(fr.page_num for fr in grp)),
                    "committed": any(fr.commit_size > 0 for fr in grp),
                    "salt1": current["salt1"],
                    "salt2": current["salt2"],
                })
                current = {
                    "start_frame": f.index,
                    "frames": [f],
                    "salt1": f.salt1,
                    "salt2": f.salt2,
                }

            # If this frame has commit_size > 0, it ends a transaction
            if f.commit_size > 0:
                grp = current["frames"]
                groups.append({
                    "start_frame": current["start_frame"],
                    "end_frame": f.index,
                    "frame_count": len(grp),
                    "pages": sorted(set(fr.page_num for fr in grp)),
                    "committed": True,
                    "salt1": current["salt1"],
                    "salt2": current["salt2"],
                })
                # Next frame (if any) starts a new group
                if i + 1 < len(self.frames):
                    nf = self.frames[i + 1]
                    current = {
                        "start_frame": nf.index,
                        "frames": [],
                        "salt1": nf.salt1,
                        "salt2": nf.salt2,
                    }
                else:
                    current = {"frames": []}

        # Finalize any remaining frames (uncommitted tail)
        if current.get("frames"):
            grp = current["frames"]
            groups.append({
                "start_frame": current["start_frame"],
                "end_frame": grp[-1].index,
                "frame_count": len(grp),
                "pages": sorted(set(fr.page_num for fr in grp)),
                "committed": False,
                "salt1": current.get("salt1", 0),
                "salt2": current.get("salt2", 0),
            })

        return groups
