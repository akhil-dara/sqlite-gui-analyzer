"""Database abstraction layer for SQLite GUI Analyzer."""

import sqlite3
import struct
import re
import os
import shutil
import binascii
import tempfile

from constants import SEARCH_MODES, PAGE_TYPES
from utils import _q, _le, _regex_literal_hint, blob_type, fmtb, tr


# ── DB class ─────────────────────────────────────────────────────────────
class DB:
    def __init__(self):
        self._conn = None
        self._search_conn = None  # Separate connection for search (no row_factory)
        self._path = None
        self._wal = None  # WALParser instance for forensic WAL analysis
        self._wal_backup = None  # Path to WAL backup copy (forensic preservation)
        self._wal_original_size = 0  # Size of original WAL before opening

    def open(self, path):
        self.close()
        self._path = path

        # ── Forensic WAL preservation ──
        # CRITICAL: Copy the WAL file BEFORE opening sqlite3 connections.
        # SQLite may auto-checkpoint the WAL when the last connection closes,
        # destroying forensic evidence. We preserve the original WAL by
        # copying it to a named backup, then point our WAL parser at it.
        # The backup is NOT deleted on close — it persists for forensic use.
        wal_path = path + "-wal"
        wal_backup = None
        self._wal_original_size = 0
        if os.path.isfile(wal_path) and os.path.getsize(wal_path) > 0:
            self._wal_original_size = os.path.getsize(wal_path)
            # Named backup next to original: dbname.db-wal.bak
            wal_backup_path = wal_path + ".bak"
            try:
                # Only create backup if one doesn't exist or original is newer
                need_backup = True
                if os.path.isfile(wal_backup_path):
                    bak_mtime = os.path.getmtime(wal_backup_path)
                    orig_mtime = os.path.getmtime(wal_path)
                    bak_size = os.path.getsize(wal_backup_path)
                    if bak_size > 0 and bak_mtime >= orig_mtime:
                        need_backup = False  # Existing backup is up-to-date
                if need_backup:
                    shutil.copy2(wal_path, wal_backup_path)
                wal_backup = wal_backup_path
                self._wal_backup = wal_backup
            except Exception:
                # If copy fails, try to read the original WAL directly
                wal_backup = None
                self._wal_backup = None

        uri = "file:" + path.replace("\\", "/") + "?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA cache_size = -8000")
            self._conn.execute("PRAGMA mmap_size = 268435456")
            self._conn.execute("PRAGMA temp_store = MEMORY")
            self._conn.execute("PRAGMA query_only = ON")
            self._conn.execute("PRAGMA wal_autocheckpoint = 0")
        except Exception:
            pass
        self._conn.create_function("REGEXP", 2, DB._safe_regexp)
        # Separate search connection — tuple mode, no row_factory overhead
        self._search_conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            self._search_conn.execute("PRAGMA cache_size = -8000")
            self._search_conn.execute("PRAGMA mmap_size = 268435456")
            self._search_conn.execute("PRAGMA temp_store = MEMORY")
            self._search_conn.execute("PRAGMA query_only = ON")
            self._search_conn.execute("PRAGMA wal_autocheckpoint = 0")
        except Exception:
            pass
        self._search_conn.create_function("REGEXP", 2, DB._safe_regexp)
        # Open WAL parser on the BACKUP copy (preserved from checkpoint)
        from wal_parser import WALParser
        self._wal = WALParser()
        if wal_backup:
            self._wal.open_wal_file(wal_backup, path)
        else:
            self._wal.open(path)  # Fallback: try original WAL
        # Build page→table map so WAL results show real table/column names
        if self._wal.valid:
            self._build_wal_page_map()

    @staticmethod
    def _safe_regexp(pattern, value):
        if value is None:
            return False
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            return bool(re.search(pattern, str(value)))
        except Exception:
            return False

    def close(self):
        if self._wal:
            try:
                self._wal.close()
            except Exception:
                pass
            self._wal = None
        if self._search_conn:
            try:
                self._search_conn.close()
            except Exception:
                pass
            self._search_conn = None
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._path = None
        # NOTE: WAL backup file is intentionally NOT deleted on close.
        # It persists as forensic evidence since SQLite may have
        # checkpointed the original WAL to 0 bytes during our session.
        self._wal_backup = None

    @property
    def ok(self):
        return self._conn is not None

    @property
    def has_wal(self):
        """True if a valid WAL file was found and parsed."""
        return self._wal is not None and self._wal.valid

    @property
    def wal_backup_path(self):
        """Path to the forensic WAL backup file, or None."""
        return self._wal_backup

    @property
    def wal_original_size(self):
        """Size of the original WAL file when first opened."""
        return self._wal_original_size

    @property
    def wal(self):
        """Access the WAL parser directly."""
        return self._wal

    def _build_wal_page_map(self):
        """Build page_num → table_name map so WAL results show real names.

        Reads sqlite_master root pages, then traverses B-tree interior pages
        in the main DB file to map every page to its owning table.
        Also stores column names per table for WAL record display.
        """
        if not self.ok or not self._wal or not self._wal.valid:
            return

        page_map = {}        # page_num → table_name
        col_map = {}         # table_name → [col_name, ...]
        pk_col_idx = {}      # table_name → column_index of INTEGER PRIMARY KEY
        root_pages = {}      # root_page → table_name

        try:
            # Get root pages and column info from sqlite_master.
            # Include tbl_name so index pages map to the owning TABLE,
            # not the index name (indexes don't have column info).
            rows = self._conn.execute(
                "SELECT type, name, tbl_name, rootpage FROM sqlite_master "
                "WHERE rootpage > 0"
            ).fetchall()

            for obj_type, name, tbl_name, rootpage in rows:
                if obj_type == "table":
                    # Table root pages map to themselves
                    root_pages[rootpage] = name
                    page_map[rootpage] = name
                    try:
                        cols = self._conn.execute(
                            f"PRAGMA table_info({_q(name)})"
                        ).fetchall()
                        col_map[name] = [c[1] for c in cols]
                        # Detect INTEGER PRIMARY KEY (pk=1, type=INTEGER)
                        # These columns use rowid as value (not stored in record)
                        for c in cols:
                            if c[5] == 1 and c[2].upper() == "INTEGER":
                                pk_col_idx[name] = c[0]  # cid = column index
                                break
                    except Exception:
                        pass
                else:
                    # Index/view/trigger: map root page to the OWNING TABLE
                    # so child pages get proper table name and column info
                    owner = tbl_name or name
                    root_pages[rootpage] = owner
                    page_map[rootpage] = owner

            # Page 1 is always sqlite_master
            page_map[1] = "sqlite_master"
            col_map["sqlite_master"] = ["type", "name", "tbl_name", "rootpage", "sql"]
            # Include page 1 in btree traversal so sqlite_master overflow
            # pages (when schema is large) get properly mapped
            root_pages[1] = "sqlite_master"

            # Traverse B-tree interior pages to map child pages
            # Read the main DB file directly to walk the tree
            db_path = self._path
            page_size = self._wal.page_size

            if os.path.isfile(db_path) and page_size > 0:
                self._traverse_btree_pages(db_path, page_size, root_pages,
                                            page_map)

        except Exception:
            pass  # Best-effort — partial map is still useful

        # Also scan WAL frames for sqlite_master (page 1) to discover
        # tables created inside WAL transactions (not in main DB yet)
        self._scan_wal_for_new_tables(page_map, col_map, pk_col_idx)

        # Final pass: scan WAL interior pages for child pointers to new pages
        # (WAL operations may allocate new btree pages for existing tables)
        self._map_wal_interior_children(page_map)

        # Store on the WAL parser for use during search and display
        self._wal.page_map = page_map
        self._wal.col_map = col_map
        self._wal.pk_col_idx = pk_col_idx

    def _map_wal_interior_children(self, page_map):
        """Scan WAL frames for interior pages and map their child pointers.

        When WAL operations insert/update rows, SQLite may split B-tree pages,
        creating new child pages that only exist in the WAL. This method
        finds interior pages in the WAL that belong to known tables and
        maps all their child page pointers.
        """
        if not self._wal or not self._wal.valid:
            return

        for frame in self._wal.frames:
            if frame.page_type_byte not in (0x02, 0x05):
                continue  # Only interior pages
            table_name = page_map.get(frame.page_num)
            if not table_name:
                continue  # Skip unmapped interior pages

            try:
                page_data = self._wal.get_page_data(frame.index)
                if not page_data or len(page_data) < 12:
                    continue

                cell_count = int.from_bytes(page_data[3:5], "big")
                right_child = int.from_bytes(page_data[8:12], "big")

                if right_child > 0 and right_child not in page_map:
                    page_map[right_child] = table_name

                ptr_start = 12
                for i in range(cell_count):
                    po = ptr_start + i * 2
                    if po + 2 > len(page_data):
                        break
                    cell_off = int.from_bytes(page_data[po:po + 2], "big")
                    if cell_off + 4 > len(page_data):
                        continue
                    child_pg = int.from_bytes(
                        page_data[cell_off:cell_off + 4], "big")
                    if child_pg > 0 and child_pg not in page_map:
                        page_map[child_pg] = table_name
            except Exception:
                continue

    def _traverse_btree_pages(self, db_path, page_size, root_pages, page_map):
        """Walk B-tree interior pages to map all child pages to table names.

        For each root page, reads the page from disk, checks if it's an
        interior page, and recursively maps all child page pointers.
        """
        try:
            db_size = os.path.getsize(db_path)
        except Exception:
            return

        max_pages = db_size // page_size
        visited = set()

        def _read_page(f, page_num):
            """Read a single page from the DB file."""
            if page_num < 1 or page_num > max_pages:
                return None
            # Page 1 starts at offset 0, page 2 at page_size, etc.
            offset = (page_num - 1) * page_size
            f.seek(offset)
            data = f.read(page_size)
            return data if len(data) == page_size else None

        def _walk_interior(f, page_num, table_name, depth=0):
            """Recursively walk interior B-tree pages to find all child pages."""
            if depth > 20 or page_num in visited:
                return
            visited.add(page_num)
            page_map[page_num] = table_name

            data = _read_page(f, page_num)
            if not data:
                return

            # For page 1, skip the 100-byte DB header
            hdr_offset = 100 if page_num == 1 else 0
            pt = data[hdr_offset]

            # Only interior pages (0x05 = table interior, 0x02 = index interior)
            # have child page pointers
            if pt not in (0x02, 0x05):
                return

            # Interior page header: type(1) + freeblock(2) + cells(2) +
            #                       cellstart(2) + frag(1) + rightchild(4)
            cell_count = int.from_bytes(
                data[hdr_offset + 3:hdr_offset + 5], "big")
            right_child = int.from_bytes(
                data[hdr_offset + 8:hdr_offset + 12], "big")

            # Map and recurse right child
            if right_child > 0:
                page_map[right_child] = table_name
                _walk_interior(f, right_child, table_name, depth + 1)

            # Cell pointer array starts after header (12 bytes for interior)
            ptr_start = hdr_offset + 12
            for i in range(cell_count):
                po = ptr_start + i * 2
                if po + 2 > len(data):
                    break
                cell_offset = int.from_bytes(data[po:po + 2], "big")
                if cell_offset + 4 > len(data):
                    continue

                # Interior cell: left_child(4 bytes) + key(varint)
                child_page = int.from_bytes(
                    data[cell_offset:cell_offset + 4], "big")
                if child_page > 0:
                    page_map[child_page] = table_name
                    _walk_interior(f, child_page, table_name, depth + 1)

        try:
            with open(db_path, "rb") as f:
                for root_page, table_name in root_pages.items():
                    _walk_interior(f, root_page, table_name)
        except Exception:
            pass  # Best-effort

    def _scan_wal_for_new_tables(self, page_map, col_map, pk_col_idx):
        """Scan WAL's sqlite_master pages (page 1) for newly created tables.

        Tables created inside WAL transactions don't exist in the main DB's
        sqlite_master yet. We parse the WAL copy of page 1 to discover them.
        Also scans interior pages of sqlite_master if page 1 is a btree
        interior node (large databases with many tables).
        """
        if not self._wal or not self._wal.valid:
            return

        # Collect all sqlite_master page frames from WAL
        # page 1 is always sqlite_master root, but child pages may also be here
        master_leaf_cells = []

        for frame in self._wal.frames:
            if frame.page_num != 1:
                continue
            try:
                page_data = self._wal.get_page_data(frame.index)
                if not page_data or len(page_data) < 108:
                    continue

                pt = page_data[100]  # btree type at offset 100

                if pt == 0x0D:
                    # Leaf page — parse cells directly with page1 handler
                    cells = self._wal.parse_page1_cells(page_data)
                    master_leaf_cells.extend(cells)

                elif pt == 0x05:
                    # Interior page — find child pages that might also be in WAL
                    cell_count = int.from_bytes(page_data[103:105], "big")
                    right_child = int.from_bytes(page_data[108:112], "big")
                    # Collect child page numbers
                    child_pages = set()
                    if right_child > 0:
                        child_pages.add(right_child)
                    ptr_start = 112  # 100 + 12 (interior header)
                    for i in range(cell_count):
                        po = ptr_start + i * 2
                        if po + 2 > len(page_data):
                            break
                        cell_off = int.from_bytes(page_data[po:po + 2], "big")
                        if cell_off + 4 > len(page_data):
                            continue
                        child_pg = int.from_bytes(
                            page_data[cell_off:cell_off + 4], "big")
                        if child_pg > 0:
                            child_pages.add(child_pg)

                    # Map all child pages as sqlite_master
                    for cpg in child_pages:
                        page_map[cpg] = "sqlite_master"

                    # Look for these child pages in WAL frames
                    for cf in self._wal.frames:
                        if cf.page_num in child_pages and cf.page_type_byte == 0x0D:
                            try:
                                cpd = self._wal.get_page_data(cf.index)
                                cells = self._wal.parse_leaf_cells(cpd)
                                master_leaf_cells.extend(cells)
                            except Exception:
                                continue
            except Exception:
                continue

        # Build a quick lookup: page_num → WAL frame (latest frame wins)
        wal_page_frames = {}
        for frame in self._wal.frames:
            wal_page_frames[frame.page_num] = frame

        # Now extract table info from all discovered sqlite_master cells.
        # sqlite_master columns: type, name, tbl_name, rootpage, sql
        new_root_pages = {}  # root_page → table_name (only WAL-created tables)
        for cell in master_leaf_cells:
            vals = cell.get("values", [])
            if len(vals) < 5:
                continue
            obj_type = str(vals[0]) if vals[0] else ""
            name = str(vals[1]) if vals[1] else ""
            tbl_name = str(vals[2]) if vals[2] else name
            rootpage = vals[3] if len(vals) > 3 else 0

            if name and rootpage:
                try:
                    rp = int(rootpage)
                    if rp > 0:
                        if obj_type == "table":
                            page_map[rp] = name
                            # Parse CREATE TABLE sql to get columns + PK
                            sql = str(vals[4]) if len(vals) > 4 and vals[4] else ""
                            if sql and name not in col_map:
                                col_names = self._parse_create_columns(sql)
                                if col_names:
                                    col_map[name] = col_names
                                # Detect INTEGER PRIMARY KEY from SQL
                                if name not in pk_col_idx:
                                    pk_i = self._detect_pk_from_sql(sql)
                                    if pk_i >= 0:
                                        pk_col_idx[name] = pk_i
                            # Track for btree traversal
                            if rp not in new_root_pages:
                                new_root_pages[rp] = name
                        else:
                            # Index/view/trigger: map to owning table
                            owner = tbl_name or name
                            page_map[rp] = owner
                except (ValueError, TypeError):
                    pass

        # Traverse btree pages for newly discovered tables that exist in WAL
        # (these tables' pages are only in the WAL, not in the main DB file)
        for root_pg, tname in new_root_pages.items():
            self._traverse_wal_btree(root_pg, tname, page_map, wal_page_frames)

    def _traverse_wal_btree(self, root_page, table_name, page_map,
                             wal_page_frames, depth=0):
        """Walk btree interior pages that exist in WAL to map child pages."""
        if depth > 20 or root_page in page_map and depth > 0:
            return  # Already mapped or too deep
        if root_page not in wal_page_frames:
            return  # Page not in WAL

        frame = wal_page_frames[root_page]
        if frame.page_type_byte not in (0x02, 0x05):
            return  # Only interior pages have children

        page_map[root_page] = table_name
        try:
            page_data = self._wal.get_page_data(frame.index)
            if not page_data or len(page_data) < 12:
                return

            cell_count = int.from_bytes(page_data[3:5], "big")
            right_child = int.from_bytes(page_data[8:12], "big")

            if right_child > 0:
                page_map[right_child] = table_name
                self._traverse_wal_btree(right_child, table_name, page_map,
                                          wal_page_frames, depth + 1)

            ptr_start = 12
            for i in range(cell_count):
                po = ptr_start + i * 2
                if po + 2 > len(page_data):
                    break
                cell_off = int.from_bytes(page_data[po:po + 2], "big")
                if cell_off + 4 > len(page_data):
                    continue
                child_pg = int.from_bytes(
                    page_data[cell_off:cell_off + 4], "big")
                if child_pg > 0:
                    page_map[child_pg] = table_name
                    self._traverse_wal_btree(child_pg, table_name, page_map,
                                              wal_page_frames, depth + 1)
        except Exception:
            pass

    @staticmethod
    def _detect_pk_from_sql(sql):
        """Detect INTEGER PRIMARY KEY column index from CREATE TABLE SQL.

        Returns column index (0-based) or -1 if not found.
        """
        m = re.search(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s*\((.+)\)',
                       sql, re.IGNORECASE | re.DOTALL)
        if not m:
            return -1
        body = m.group(1)
        # Split columns (respecting parens)
        depth = 0
        parts = []
        current = ""
        for ch in body:
            if ch == '(':
                depth += 1
                current += ch
            elif ch == ')':
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0:
                parts.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            parts.append(current.strip())
        # Find "col_name INTEGER PRIMARY KEY"
        col_idx = 0
        for part in parts:
            words = part.split()
            if not words:
                continue
            first_upper = words[0].upper()
            if first_upper in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                continue
            upper = part.upper()
            if "INTEGER" in upper and "PRIMARY" in upper and "KEY" in upper:
                return col_idx
            col_idx += 1
        return -1

    @staticmethod
    def _parse_create_columns(sql):
        """Extract column names from a CREATE TABLE SQL statement."""
        # Match CREATE TABLE name (...column defs...)
        m = re.search(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s*\((.+)\)',
                       sql, re.IGNORECASE | re.DOTALL)
        if not m:
            return []
        body = m.group(1)
        cols = []
        # Split by commas but respect parentheses (for DEFAULT, CHECK, etc.)
        depth = 0
        current = ""
        for ch in body:
            if ch == '(':
                depth += 1
                current += ch
            elif ch == ')':
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0:
                cols.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            cols.append(current.strip())
        # Extract just column names (first word of each def, skip constraints)
        result = []
        for col_def in cols:
            # Skip table constraints like PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK
            first_word = col_def.split()[0] if col_def.split() else ""
            upper = first_word.upper()
            if upper in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                continue
            # Column name might be quoted
            name = first_word.strip('"').strip("'").strip('`').strip('[').strip(']')
            if name:
                result.append(name)
        return result

    def tables(self):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def wal_tables(self):
        """Return table names that exist ONLY in the WAL (not visible to SQLite).

        These are tables discovered by our WAL parser (in ``col_map``) but
        NOT visible through the live SQLite connection.  In practice this
        means **uncommitted** tables — they were created inside a WAL
        transaction that was never committed, so SQLite doesn't know about
        them.  Only WAL forensic analysis can recover their data.

        Committed WAL tables ARE visible to SQLite (even if not yet
        checkpointed to the main file) and are therefore **not** listed
        here — they are normal, accessible tables.
        """
        if not self.has_wal:
            return []
        # Live connection sees main DB + committed WAL data
        main = set(self.tables())
        # WAL parser finds ALL tables (committed + uncommitted)
        wal_col = set(self._wal.col_map.keys())
        skip = {"sqlite_master", "sqlite_sequence"}
        return sorted(wal_col - main - skip)

    def wal_browse(self, table_name, limit=200, offset=0):
        """Browse records from WAL for a given table.

        Returns (col_names, rows, total_count) where each row is a list:
        [rowid, col1, col2, ..., frame_idx, page_num, status_label].
        """
        if not self.has_wal:
            return [], [], 0

        all_recs = list(
            self._wal.recover_all_records(table_filter=table_name))
        all_recs.sort(key=lambda r: r["rowid"])
        total = len(all_recs)
        page = all_recs[offset:offset + limit]

        col_names = self._wal.col_map.get(table_name, [])
        if not col_names and page:
            col_names = list(page[0]["values_dict"].keys())

        status_map = {"committed": "Saved", "uncommitted": "Unsaved",
                      "old": "Overwritten"}
        meta_cols = ["_wal_frame", "_wal_page", "_wal_status"]
        full_cols = ["_rid"] + list(col_names) + meta_cols

        rows = []
        for rec in page:
            row = [rec["rowid"]]
            for cn in col_names:
                row.append(rec["values_dict"].get(cn, ""))
            row.append(rec["frame_idx"])
            row.append(rec["page_num"])
            row.append(status_map.get(rec["category"], rec["category"]))
            rows.append(row)

        return full_cols, rows, total

    def columns(self, tbl):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA table_info({_q(tbl)})").fetchall()
            return [(r[1], r[2]) for r in rows]
        except Exception:
            return []

    def columns_full(self, tbl):
        """Return full column info: (name, type, notnull, default, pk)."""
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA table_info({_q(tbl)})").fetchall()
            # r: cid, name, type, notnull, dflt_value, pk
            return [(r[1], r[2], bool(r[3]), r[4], int(r[5])) for r in rows]
        except Exception:
            return []

    def unique_columns(self, tbl):
        """Return set of column names that have a UNIQUE constraint (from indexes)."""
        if not self.ok:
            return set()
        try:
            result = set()
            idxs = self._conn.execute(f"PRAGMA index_list({_q(tbl)})").fetchall()
            for idx in idxs:
                if idx[2]:  # unique
                    info = self._conn.execute(f"PRAGMA index_info({_q(idx[1])})").fetchall()
                    if len(info) == 1:
                        result.add(info[0][2])
            return result
        except Exception:
            return set()

    def count(self, tbl):
        if not self.ok:
            return 0
        try:
            r = self._conn.execute(f"SELECT COUNT(*) FROM {_q(tbl)}").fetchone()
            return r[0] if r else 0
        except Exception:
            return 0

    def create_sql(self, tbl):
        if not self.ok:
            return ""
        try:
            r = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (tbl,)
            ).fetchone()
            return r[0] if r and r[0] else ""
        except Exception:
            return ""

    def indexes(self, tbl):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA index_list({_q(tbl)})").fetchall()
            result = []
            for r in rows:
                name = r[1]
                unique = r[2]
                cols = self._conn.execute(f"PRAGMA index_info({_q(name)})").fetchall()
                col_names = [c[2] for c in cols if c[2]]
                result.append((name, unique, col_names))
            return result
        except Exception:
            return []

    def fkeys(self, tbl):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA foreign_key_list({_q(tbl)})").fetchall()
            return [(r[2], r[3], r[4]) for r in rows]
        except Exception:
            return []

    def fkeys_full(self, tbl):
        """Return foreign keys with full details including ON DELETE/UPDATE."""
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(f"PRAGMA foreign_key_list({_q(tbl)})").fetchall()
            result = []
            for r in rows:
                result.append({
                    "id": r[0], "seq": r[1], "table": r[2],
                    "from": r[3], "to": r[4],
                    "on_update": r[5] if len(r) > 5 and r[5] != "NO ACTION" else "",
                    "on_delete": r[6] if len(r) > 6 and r[6] != "NO ACTION" else "",
                })
            return result
        except Exception:
            return []

    def check_constraints(self, tbl):
        """Extract CHECK constraints from CREATE TABLE SQL."""
        sql = self.create_sql(tbl)
        if not sql:
            return []
        import re
        return re.findall(r'CHECK\s*\(([^)]+)\)', sql, re.IGNORECASE)

    def view_sql(self, name):
        """Get the SQL definition of a view."""
        if not self.ok:
            return None
        try:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (name,)
            ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def trigger_details(self):
        """Get trigger names and their SQL definitions."""
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).fetchall()
            return [(r[0], r[1]) for r in rows]
        except Exception:
            return []

    def views(self):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def all_indexes(self):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%%' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def triggers(self):
        if not self.ok:
            return []
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def meta(self):
        if not self.ok:
            return {}
        info = {"path": self._path or ""}
        try:
            info["size"] = os.path.getsize(self._path) if self._path else 0
        except Exception:
            info["size"] = 0
        for prag in ("page_size", "page_count", "journal_mode", "encoding",
                      "auto_vacuum", "user_version", "freelist_count"):
            try:
                r = self._conn.execute(f"PRAGMA {prag}").fetchone()
                info[prag] = r[0] if r else ""
            except Exception:
                info[prag] = ""
        return info

    def integrity(self):
        if not self.ok:
            return "No database"
        try:
            r = self._conn.execute("PRAGMA quick_check(1)").fetchone()
            return r[0] if r else "ok"
        except Exception as e:
            return str(e)

    def search(self, tbl, cols, term, mode, limit, deep_blob, cancel):
        """Generator yielding search results using separate search connection."""
        if not self.ok or not term:
            return
        sconn = self._search_conn or self._conn
        mode_key = SEARCH_MODES.get(mode, "ci")
        col_names = [c[0] for c in cols]
        col_types = [c[1].upper() if c[1] else "" for c in cols]
        # Column Name mode
        if mode_key == "col":
            for cn in col_names:
                if cancel and cancel():
                    return
                if term.lower() in cn.lower():
                    yield dict(table=tbl, column=cn, rowid="-",
                               value=cn, type="column_name")
            return
        # Regex mode: use LIKE pre-filter when possible, cursor-based fetch
        if mode_key == "rx":
            try:
                rx = re.compile(term)
            except re.error:
                return
            found = 0
            hint = _regex_literal_hint(term)
            qtbl = _q(tbl)
            # Build SQL: use LIKE pre-filter if we have a literal hint
            if hint:
                esc_hint = _le(hint)
                parts = []
                params = []
                rx_indices = []
                for ci, cn in enumerate(col_names):
                    if not deep_blob and "BLOB" in col_types[ci]:
                        continue
                    parts.append(f"{_q(cn)} LIKE ? ESCAPE '\\'")
                    params.append(f"%{esc_hint}%")
                    rx_indices.append(ci)
                if parts:
                    sql = f"SELECT rowid, * FROM {qtbl} WHERE {' OR '.join(parts)}"
                else:
                    sql = f"SELECT rowid, * FROM {qtbl}"
                    params = []
                    rx_indices = list(range(len(col_names)))
            else:
                sql = f"SELECT rowid, * FROM {qtbl}"
                params = []
                rx_indices = [ci for ci in range(len(col_names))
                              if deep_blob or "BLOB" not in col_types[ci]]
            try:
                cur = sconn.execute(sql, params)
            except Exception:
                return
            while True:
                if cancel and cancel():
                    return
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for row in rows:
                    if cancel and cancel():
                        return
                    rid = row[0]
                    for i in rx_indices:
                        v = row[i + 1]
                        if v is None:
                            continue
                        if isinstance(v, bytes):
                            if not deep_blob:
                                continue
                            try:
                                s = v.decode("utf-8", "replace")
                            except Exception:
                                continue
                        else:
                            s = str(v)
                        if rx.search(s):
                            found += 1
                            yield dict(table=tbl, column=col_names[i], rowid=rid,
                                       value=tr(s), type=DB._dt(v))
                            if found >= limit:
                                return
                        if deep_blob and isinstance(v, bytes):
                            hx = binascii.hexlify(v).decode()
                            if rx.search(hx):
                                found += 1
                                yield dict(table=tbl, column=col_names[i], rowid=rid,
                                           value=f"[hex match in {fmtb(len(v))}]",
                                           type="blob_hex")
                                if found >= limit:
                                    return
            return
        # Combined OR query — single table scan
        # Skip BLOB-typed columns in SQL WHERE (unless blob mode) to avoid
        # scanning huge binary data; Python-side already skips bytes.
        found = 0
        esc = _le(term)
        qtbl = _q(tbl)
        parts = []
        params = []
        search_col_indices = []  # indices of columns actually in WHERE
        for ci, cn in enumerate(col_names):
            # Skip BLOB-typed columns in WHERE to avoid slow scans
            if mode_key != "blob" and "BLOB" in col_types[ci]:
                continue
            qcol = _q(cn)
            if mode_key == "blob":
                parts.append(f"CAST({qcol} AS TEXT) LIKE ? ESCAPE '\\'")
                params.append(f"%{esc}%")
            elif mode_key == "cs":
                parts.append(f"instr({qcol},?)>0")
                params.append(term)
            elif mode_key == "ex":
                parts.append(f"{qcol}=?")
                params.append(term)
            elif mode_key == "sw":
                parts.append(f"{qcol} LIKE ? ESCAPE '\\'")
                params.append(f"{esc}%")
            elif mode_key == "ew":
                parts.append(f"{qcol} LIKE ? ESCAPE '\\'")
                params.append(f"%{esc}")
            else:
                parts.append(f"{qcol} LIKE ? ESCAPE '\\'")
                params.append(f"%{esc}%")
            search_col_indices.append(ci)
        if not parts:
            return
        try:
            sql = f"SELECT rowid, * FROM {qtbl} WHERE {' OR '.join(parts)} LIMIT {limit}"
            cur = sconn.execute(sql, params)
        except Exception:
            return
        # Inline column matching — only check columns included in WHERE
        check_indices = search_col_indices if mode_key != "blob" else list(range(len(col_names)))
        if mode_key == "ci" or mode_key not in ("cs", "ex", "sw", "ew", "blob"):
            tl = term.lower()
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and tl in str(v).lower():
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "cs":
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and term in str(v):
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "ex":
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and str(v) == term:
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "sw":
            tl = term.lower()
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and str(v).lower().startswith(tl):
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "ew":
            tl = term.lower()
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is not None and not isinstance(v, bytes) and str(v).lower().endswith(tl):
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        elif mode_key == "blob":
            tl = term.lower()
            for row in cur:
                if cancel and cancel():
                    return
                rid = row[0]
                for i in check_indices:
                    v = row[i + 1]
                    if v is None:
                        continue
                    if isinstance(v, bytes):
                        try:
                            sv = v.decode("utf-8", "replace")
                        except Exception:
                            continue
                        if tl in sv.lower():
                            found += 1
                            yield dict(table=tbl, column=col_names[i], rowid=rid,
                                       value=tr(sv), type=DB._dt(v))
                            if found >= limit:
                                return
                    elif tl in str(v).lower():
                        found += 1
                        yield dict(table=tbl, column=col_names[i], rowid=rid,
                                   value=tr(str(v)), type=DB._dt(v))
                        if found >= limit:
                            return
        # Deep blob hex search
        if deep_blob and mode_key == "blob":
            for cn in col_names:
                if cancel and cancel():
                    return
                if found >= limit:
                    return
                qcol = _q(cn)
                try:
                    sql2 = f"SELECT rowid, {qcol} FROM {_q(tbl)} WHERE typeof({qcol})='blob' LIMIT {limit - found}"
                    for br in sconn.execute(sql2):
                        if cancel and cancel():
                            return
                        bv = br[1]
                        if isinstance(bv, bytes):
                            hx = binascii.hexlify(bv).decode()
                            if term.lower() in hx.lower():
                                found += 1
                                yield dict(table=tbl, column=cn, rowid=br[0],
                                           value=f"[hex match in {fmtb(len(bv))}]",
                                           type="blob_hex")
                                if found >= limit:
                                    return
                except Exception:
                    pass

    def browse(self, tbl, lim, off, ocol=None, odir="ASC"):
        if not self.ok:
            return [], []
        try:
            order = ""
            if ocol:
                order = f" ORDER BY {_q(ocol)} {odir}"
            sql = f"SELECT rowid AS _rid, * FROM {_q(tbl)}{order} LIMIT {lim} OFFSET {off}"
            cur = self._conn.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return cols, [list(r) for r in rows]
        except Exception:
            return [], []

    def full_row(self, tbl, rid):
        if not self.ok:
            return {}, []
        try:
            sql = f"SELECT rowid AS _rid, * FROM {_q(tbl)} WHERE rowid = ?"
            cur = self._conn.execute(sql, (rid,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else {}, cols
        except Exception:
            return {}, []

    @staticmethod
    def _fv(v):
        """Format value for display."""
        if v is None:
            return "NULL"
        if isinstance(v, bytes):
            hx = binascii.hexlify(v[:50]).decode()
            if len(v) > 50:
                hx += "..."
            return hx
        return str(v)

    @staticmethod
    def _dt(v):
        """Detect type."""
        if v is None:
            return "NULL"
        if isinstance(v, int):
            return "INTEGER"
        if isinstance(v, float):
            return "REAL"
        if isinstance(v, bytes):
            return "BLOB"
        return "TEXT"

    @staticmethod
    def _match(v, t, m, db=None):
        if v is None:
            return False
        s = DB._fv(v)
        mk = SEARCH_MODES.get(m, "ci")
        if mk == "ci":
            return t.lower() in s.lower()
        if mk == "cs":
            return t in s
        if mk == "ex":
            return s == t
        if mk == "sw":
            return s.startswith(t)
        if mk == "ew":
            return s.endswith(t)
        if mk == "rx":
            try:
                return bool(re.search(t, s))
            except re.error:
                return False
        return t.lower() in s.lower()
