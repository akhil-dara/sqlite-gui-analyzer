"""WAL tab adapter.

Keeps the WALParser interface the UI was written against, but every byte is now
parsed by engine.fileformat: checksummed frame states, overflow chains, WITHOUT ROWID
rows (index-tree pages) and schema-aware column mapping.
"""

from collections import OrderedDict

from constants import PAGE_TYPES, search_mode_key
from engine.fileformat.btree import (BTreeReader, INDEX_INTERIOR, INDEX_LEAF, TABLE_INTERIOR,
                                     TABLE_LEAF, cell_pointers, parse_page_header)
from engine.fileformat.record import decode_record_lenient
from engine.fileformat.wal import STATES
from engine.schema import Locator, SchemaEntry, TableInfo, collation_names, describe_table
from engine.search import match_row

SYSTEM_TABLES = ("sqlite_master", "sqlite_sequence")


class WALFrameView(object):
    """UI-facing frame: engine WalFrame plus the legacy attribute names."""
    __slots__ = ("index", "offset", "page_num", "commit_size", "salt1", "salt2", "checksum1",
                 "checksum2", "checksum_ok", "state", "category", "commit_group",
                 "page_type", "page_type_byte")

    def __init__(self, fr):
        self.index, self.offset, self.page_num = fr.index, fr.offset, fr.page_no
        self.commit_size, self.salt1, self.salt2 = fr.db_size, fr.salt1, fr.salt2
        self.checksum1, self.checksum2, self.checksum_ok = fr.checksum1, fr.checksum2, fr.checksum_ok
        self.state = self.category = fr.state
        self.commit_group = fr.commit_group
        self.page_type_byte = fr.page_type
        self.page_type = PAGE_TYPES.get(fr.page_type, "Unknown (0x%02X)" % fr.page_type)


class _FramePager(object):
    """Pager look-alike exposing one WAL frame's copy of a page; other pages
    (overflow chains) come from the session's effective pager."""

    def __init__(self, pager, page_no, data):
        self._pager, self._page_no, self._data = pager, page_no, data
        self.usable_size, self.encoding = pager.usable_size, pager.encoding
        self.page_count = pager.page_count

    def page(self, n):
        return self._data if n == self._page_no else self._pager.page(n)


def display_value(v):
    from utils import blob_type, fmtb
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return "[BLOB: %s, %s]" % (fmtb(len(v)), blob_type(v))
    if isinstance(v, float):
        return "%.6g" % v
    return str(v)


class WALParser(object):
    def __init__(self, session):
        self._session = session
        self._wal = session.wal
        self.valid = self._wal is not None
        self.page_map, self.col_map, self.pk_col_idx = {}, {}, {}
        self.tables = {}
        self.index_pages = set()
        self.wal_only_tables = set()
        self.frames = []
        if not self.valid:
            self.header, self.page_size, self.path = None, 0, None
            return
        self.header = self._wal.header
        self.page_size = self._wal.page_size
        self.path = self._wal.path
        self.frames = [WALFrameView(f) for f in self._wal.frames]
        self._build_maps()

    def close(self):
        """The session owns the WAL file; nothing to release here."""

    # -- page -> table mapping -------------------------------------------
    def _frame_cells(self, frame, index_tree):
        """(rowid_or_None, payload, CellRef) for every cell in this frame's page copy."""
        data = self._wal.page_data(frame.index)
        pager = _FramePager(self._session.pager, frame.page_num, data)
        reader = BTreeReader(pager, self._session.issues)
        try:
            h = parse_page_header(data, frame.page_num)
        except Exception:
            return []
        out = list(reader.read_segment(frame.page_num, None, index_tree)) \
            if h.type in (TABLE_LEAF, INDEX_LEAF) else []
        if h.type == INDEX_INTERIOR:
            for i in range(len(reader._offsets(data, h))):
                out.extend(reader.read_segment(frame.page_num, i, True))
        return out

    def _children(self, frame):
        data = self._wal.page_data(frame.index)
        try:
            h = parse_page_header(data, frame.page_num)
        except Exception:
            return []
        if h.right_child is None:
            return []
        kids = [h.right_child]
        for off in cell_pointers(data, h, self._session.pager.usable_size):
            if off + 4 <= len(data):
                kids.append(int.from_bytes(data[off:off + 4], "big"))
        return kids

    def _wal_master_entries(self):
        """sqlite_master rows found in any WAL copy of the schema pages (incl. uncommitted)."""
        master_pages = {1}
        for f in self.frames:
            if f.page_num == 1 and f.page_type_byte == TABLE_INTERIOR:
                master_pages.update(self._children(f))
        entries = []
        enc = self._session.pager.encoding
        for f in self.frames:
            if f.page_num in master_pages and f.page_type_byte == TABLE_LEAF:
                for rowid, payload, ref in self._frame_cells(f, False):
                    v, problem = decode_record_lenient(payload, enc)
                    if problem or len(v) < 5:
                        continue
                    root = v[3] if isinstance(v[3], int) else 0
                    entries.append(SchemaEntry(str(v[0] or ""), str(v[1] or ""), str(v[2] or ""),
                                               root, v[4] if isinstance(v[4], str) else ""))
        return entries

    def _build_maps(self):
        schema = self._session.schema
        for name in schema.names("table"):
            self.tables[name] = schema.get(name)
        wal_entries = self._wal_master_entries()
        colls = schema.collations | collation_names(e.sql for e in wal_entries)
        roots = {}
        for e in list(schema.entries) + wal_entries:
            if e.rootpage <= 0:
                continue
            if e.type == "table" and not e.sql.lstrip().upper().startswith("CREATE VIRTUAL"):
                if e.name not in self.tables:
                    t = TableInfo(e.name, "table", e.rootpage, e.sql)
                    describe_table(t, colls)
                    if t.columns:
                        self.tables[e.name] = t
                        self.wal_only_tables.add(e.name)
                roots.setdefault(e.rootpage, e.name)
            elif e.type == "index":
                self.index_pages.add(e.rootpage)
        reader = BTreeReader(self._session.pager, self._session.issues)
        for root, name in roots.items():
            self.page_map[root] = name
            try:
                for p in reader.tree_pages(root):
                    self.page_map.setdefault(p, name)
            except Exception as e:     # e.g. a WAL-only root beyond the committed page count
                self._session.issues.add("wal_page_map", str(e), "%s root %d" % (name, root))
        self.page_map[1] = "sqlite_master"
        for _ in range(8):   # follow interior pages that only exist in WAL frames
            changed = False
            for f in self.frames:
                if f.page_type_byte not in (TABLE_INTERIOR, INDEX_INTERIOR):
                    continue
                owner = self.page_map.get(f.page_num)
                if owner is None:
                    continue
                for child in self._children(f):
                    if child not in self.page_map:
                        self.page_map[child] = owner
                        changed = True
            if not changed:
                break
        for name, t in self.tables.items():
            self.col_map[name] = t.column_names
            if t.rowid_alias is not None:
                self.pk_col_idx[name] = t.rowid_alias
        self.col_map["sqlite_master"] = ["type", "name", "tbl_name", "rootpage", "sql"]

    # -- raw page access (frame detail panel) ----------------------------
    def get_page_data(self, frame_index):
        if not self.valid or not 0 <= frame_index < len(self.frames):
            return b""
        return self._wal.page_data(frame_index)

    def parse_btree_page(self, page_data, page_no=0):
        try:
            h = parse_page_header(page_data, page_no)
        except Exception:
            return None
        return {"page_type": PAGE_TYPES.get(h.type, "Unknown (0x%02X)" % h.type),
                "page_type_byte": h.type, "cell_count": h.cell_count,
                "cell_offsets": cell_pointers(page_data, h, len(page_data)),
                "first_free": h.first_freeblock, "frag_count": h.fragmented,
                "right_child": h.right_child, "cell_content_start": h.content_start}

    def parse_leaf_cells(self, page_data, page_no=0):
        """Cells of a table-leaf page copy as [{rowid, values}] (overflow followed when page_no given)."""
        pager = _FramePager(self._session.pager, page_no or 0, page_data)
        issues = self._session.issues
        reader = BTreeReader(pager, issues)
        try:
            h = parse_page_header(page_data, page_no or 0)
        except Exception:
            return []      # not a b-tree page (overflow/freelist copy): no cells to show
        if h.type != TABLE_LEAF:
            return []
        out = []
        enc = self._session.pager.encoding
        for off in reader._offsets(page_data, h):
            try:
                rowid, payload, ref = reader._table_leaf_cell(page_data, page_no or 0, off)
            except Exception as e:
                issues.add("bad_cell", str(e), "WAL copy of page %d offset %d" % (page_no, off))
                continue
            values, _ = decode_record_lenient(payload, enc)
            out.append({"rowid": rowid, "values": values})
        return out

    # -- records -----------------------------------------------------------
    def _records_in_frame(self, frame, include_schema):
        name = self.page_map.get(frame.page_num)
        if frame.page_num == 1 or name == "sqlite_master":
            if not include_schema:
                return
            name = "sqlite_master"
        t = self.tables.get(name) if name else None
        pt = frame.page_type_byte
        if pt == TABLE_LEAF and (t is None or not t.without_rowid):
            index_tree = False
        elif pt in (INDEX_LEAF, INDEX_INTERIOR) and t is not None and t.without_rowid:
            index_tree = True
        else:
            return
        enc = self._session.pager.encoding
        for ordinal, (rowid, payload, ref) in enumerate(self._frame_cells(frame, index_tree)):
            values, problem = decode_record_lenient(payload, enc)
            if t is not None and name != "sqlite_master":
                row, flags = t.record_to_row(rowid, values, damaged=bool(problem))
                cols = t.column_names
                locator = t.locator_for(rowid, row, ordinal)
            else:
                row, flags = values, set()
                cols = self.col_map.get(name or "", [])
                cols = cols + ["col%d" % i for i in range(len(cols), len(row))]
                locator = Locator("rowid", rowid)
            if problem:
                flags.add("damaged_record")
            yield (name or "page_%d" % frame.page_num), cols, locator, row, flags

    def records_of_frame(self, frame_index):
        """The records of one frame's page copy (dicts like recover_all_records())."""
        if not self.valid or not 0 <= frame_index < len(self.frames):
            return []
        frame = self.frames[frame_index]
        return [self._record_dict(frame, table, cols, locator, row, flags)
                for table, cols, locator, row, flags in self._records_in_frame(frame, False)]

    @staticmethod
    def _record_dict(frame, table, cols, locator, row, flags):
        return {"table": table,
                "rowid": locator.value if locator.kind == "rowid" else locator.display(),
                "locator": locator,
                "values_dict": dict((c, display_value(v)) for c, v in zip(cols, row)),
                "raw_values": row, "flags": flags,
                "frame_idx": frame.index, "page_num": frame.page_num,
                "category": frame.category}

    def recover_all_records(self, table_filter=None, category_filter=None, cancel=None,
                            include_schema=False):
        """Every record in every WAL frame, with its frame state.

        Yields dicts: table, rowid (int, or display text for WITHOUT ROWID), locator,
        values_dict (display strings), raw_values, flags, frame_idx, page_num, category.
        """
        if not self.valid:
            return
        for frame in self.frames:
            if cancel is not None and cancel():
                return
            if category_filter and frame.category != category_filter:
                continue
            name = self.page_map.get(frame.page_num, "page_%d" % frame.page_num)
            if table_filter and name != table_filter:
                continue
            if not include_schema and name in SYSTEM_TABLES:
                continue
            for table, cols, locator, row, flags in self._records_in_frame(frame, include_schema):
                yield self._record_dict(frame, table, cols, locator, row, flags)

    def search(self, term, mode, limit=None, cancel=None, deep_blob=False, decoded=False):
        """Search every row version held in WAL frames, with the same rules as a table search
        (engine.search: BLOB bytes in UTF-8/UTF-16, hex patterns, decoded content).

        A row version is a row's key plus its values: the identical copies of it that superseded
        and stale frames repeat are one version, searched once. Each hit is one matching cell of
        a version and lists all frames holding that version in 'frames' [(frame index, page,
        state)]; frame_idx/page_num/category describe the newest of them. At most `limit`
        versions are returned (None: every one).
        """
        mode = search_mode_key(mode, mode)           # a UI label or an engine mode key
        if not self.valid or not term or mode == "col":
            return
        matcher = self._session.matcher(term, mode, deep_blob, decoded)
        versions = OrderedDict()                     # (table, locator, values) -> [record, frames]
        for rec in self.recover_all_records(cancel=cancel):
            try:
                key = (rec["table"], rec["locator"], tuple(rec["raw_values"]))
                entry = versions.get(key)
            except TypeError:                        # an unhashable value: keep this copy apart
                key, entry = (rec["table"], rec["locator"], id(rec)), None
            frame = (rec["frame_idx"], rec["page_num"], rec["category"])
            if entry is None:
                versions[key] = [rec, [frame]]
            else:
                entry[1].append(frame)
        found = 0
        hits = []
        for rec, frames in versions.values():
            if cancel is not None and cancel():
                return
            table = rec["table"]
            cols = list(rec["values_dict"].keys())
            t = self.tables.get(table)
            decl = dict((c.name, c.decl_type) for c in t.columns) if t is not None else {}
            del hits[:]
            match_row(table, cols, [decl.get(c, "") for c in cols], rec["locator"],
                      rec["raw_values"], matcher, hits.append)
            if not hits:
                continue
            newest = max(frames)
            for h in hits:
                h.update({"rowid": rec["rowid"], "source": "WAL (%s)" % newest[2].title(),
                          "frame_idx": newest[0], "page_num": newest[1], "category": newest[2],
                          "frames": frames, "row_data": rec["values_dict"],
                          "row": rec["raw_values"]})
                yield h
            found += 1
            if limit is not None and found >= limit:
                return

    # -- summaries -----------------------------------------------------------
    def summary(self):
        if not self.valid:
            return {}
        counts = self._wal.state_counts()
        page_types = {}
        for f in self.frames:
            page_types[f.page_type] = page_types.get(f.page_type, 0) + 1
        out = {"total_frames": len(self.frames), "unique_pages": len(set(f.page_num for f in self.frames)),
               "page_types": page_types, "wal_size": self._wal.size, "page_size": self.page_size,
               "checkpoint_seq": self.header.checkpoint_seq, "header_salt1": self.header.salt1,
               "header_salt2": self.header.salt2, "commits": self._wal.commit_count,
               "header_checksum_ok": self.header.checksum_ok,
               "checksum_failures": sum(1 for f in self.frames if f.checksum_ok is False)}
        out.update(counts)
        return out

    def table_stats(self):
        stats = {}
        for frame in self.frames:
            name = self.page_map.get(frame.page_num)
            t = self.tables.get(name) if name else None
            if not name or name in SYSTEM_TABLES or frame.page_num == 1:
                continue
            leaf = frame.page_type_byte == TABLE_LEAF or (
                t is not None and t.without_rowid and frame.page_type_byte in (INDEX_LEAF, INDEX_INTERIOR))
            if not leaf:
                continue
            s = stats.setdefault(name, dict([("total_records", 0), ("frames", 0), ("pages", set()),
                                             ("is_wal_only", name in self.wal_only_tables)]
                                            + [(st, 0) for st in STATES]))
            data = self._wal.page_data(frame.index)
            n = int.from_bytes(data[3:5], "big")
            s["frames"] += 1
            s["pages"].add(frame.page_num)
            s["total_records"] += n
            s[frame.category] += n
        return stats
