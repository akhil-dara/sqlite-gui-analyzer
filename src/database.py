"""Compatibility facade: the DB interface the Tk UI was written against,
backed by engine.session.Session (evidence-safe, WITHOUT ROWID aware).
"""

import re
import sqlite3

from constants import PAGE_TYPES, search_mode_key
from engine.fileformat.freelist import freelist_pages
from engine.fileformat.record import InvalidText
from engine.schema import Locator, quote_ident
from engine.search import MODES, search_records, value_type
from engine.session import Session, SessionError  # noqa: F401  (re-exported for the UI)
from utils import blob_type, fmtb

RID = "_rid"


def search_mode(mode):
    """Engine mode key for a UI mode label or a mode key ('hex' has no label yet); else 'ci'."""
    key = search_mode_key(mode, None)
    return key if key is not None else (mode if mode in MODES else "ci")


class BrowseRow(list):
    """A browsed row as the UI indexes it ([locator, value, ...]) plus the engine's flags for it
    (damaged_record, pre_alter, virtual_generated, extra_values), so the grid can mark it."""
    __slots__ = ("flags",)

    def __init__(self, items, flags=()):
        list.__init__(self, items)
        self.flags = flags


class RowData(dict):
    """full_row()'s {column: value} plus the engine's flags for the row."""
    __slots__ = ("flags",)

    def __init__(self, items, flags=()):
        dict.__init__(self, items)
        self.flags = flags


def as_locator(rid):
    """Accept a Locator, an int rowid or a digit string (legacy callers)."""
    if isinstance(rid, Locator):
        return rid
    if isinstance(rid, int):
        return Locator("rowid", rid)
    if isinstance(rid, str) and re.match(r"^-?\d+$", rid):
        return Locator("rowid", int(rid))
    return None


class DB(object):
    def __init__(self):
        self.session = None
        self._wal_adapter = None
        self._path = None
        self.last_page = None

    # -- lifecycle -----------------------------------------------------------
    def open(self, path, ram_limit=None, safe_parse=False, cancel=None, progress=None):
        """Open the evidence at path. safe_parse=True: only the built-in parser reads it
        (SQLite never opens the file). cancel() -> True stops a long open; progress(text)."""
        self.close()
        kw = {} if ram_limit is None else {"ram_limit": ram_limit}
        self.session = Session.open(path, safe_parse=safe_parse, cancel=cancel,
                                    progress=progress, **kw)
        self._path = self.session.evidence.main
        if self.session.wal is not None:
            from wal_parser import WALParser
            self._wal_adapter = WALParser(self.session)

    def close(self, report=None):
        """Close and return the evidence VerifyReport (None if nothing was open). report: a
        verification the caller already made (then the files are not verified again)."""
        if self.session is not None:
            report = self.session.close(verify=report is None) or report
        self.session = None
        self._wal_adapter = None
        self._path = None
        return report

    @property
    def ok(self):
        return self.session is not None

    @property
    def has_wal(self):
        return self._wal_adapter is not None and bool(self._wal_adapter.frames)

    @property
    def wal(self):
        return self._wal_adapter

    @property
    def evidence(self):
        return self.session.evidence if self.session else None

    @property
    def mode(self):
        """The open mode: the session's, or 'safe-parse' when only the built-in parser reads
        the file."""
        if not self.session:
            return None
        return "safe-parse" if self.session.safe_parse else self.session.mode

    @property
    def safe_parse(self):
        return bool(self.session) and self.session.safe_parse

    def banners(self):
        return self.session.banners() if self.session else []

    def sql_conn(self):
        """This thread's read-only SQL connection (None in native mode)."""
        return self.session.conn() if self.session else None

    def new_sql_conn(self):
        """A fresh read-only SQL connection (None in native mode); give it back with
        release_sql_conn(). The session interrupts it on interrupt() and close()."""
        return self.session.new_connection() if self.session else None

    def release_sql_conn(self, conn):
        if self.session is not None:
            self.session.release_connection(conn)
        else:
            conn.close()

    def interrupt(self, thread=None):
        """Cancel running SQL on the given worker thread's connection (all when None)."""
        if self.session is not None:
            self.session.interrupt(thread)

    def is_protected(self, path):
        return bool(self.session) and self.session.evidence.is_protected(path)

    # -- schema --------------------------------------------------------------
    def _entries(self, type_):
        return self.session.schema.of_type(type_) if self.session else []

    def tables(self):
        return sorted(self.session.tables()) if self.session else []

    def views(self):
        return self.session.views() if self.session else []

    def all_indexes(self):
        return sorted(e.name for e in self._entries("index") if not e.name.startswith("sqlite_"))

    def triggers(self):
        return sorted(e.name for e in self._entries("trigger"))

    def trigger_details(self):
        return sorted((e.name, e.sql) for e in self._entries("trigger"))

    def create_sql(self, name):
        for e in (self.session.schema.entries if self.session else []):
            if e.name == name:
                return e.sql or ""
        return ""

    def columns(self, tbl):
        if not self.session:
            return []
        t = self.session.info(tbl)
        return [(c.name, c.decl_type) for c in t.columns if c.hidden != 1]

    def columns_full(self, tbl):
        if not self.session:
            return []
        return [(c.name, c.decl_type, c.notnull, c.default_sql, c.pk_pos)
                for c in self.session.info(tbl).columns if c.hidden != 1]

    def _pragma(self, sql):
        """Rows of a schema PRAGMA; [] when SQL is unavailable or the PRAGMA fails (logged)."""
        conn = self.sql_conn()
        if conn is None:
            return []
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.Error as e:
            self.session.issues.add("pragma_failed", str(e), sql)
            return []

    def unique_columns(self, tbl):
        out = set()
        for idx in self._pragma("PRAGMA index_list(%s)" % quote_ident(tbl)):
            if idx[2]:
                info = self._pragma("PRAGMA index_info(%s)" % quote_ident(idx[1]))
                if len(info) == 1:
                    out.add(info[0][2])
        return out

    def indexes(self, tbl):
        res = []
        for r in self._pragma("PRAGMA index_list(%s)" % quote_ident(tbl)):
            cols = [c[2] for c in self._pragma("PRAGMA index_info(%s)" % quote_ident(r[1])) if c[2]]
            res.append((r[1], r[2], cols))
        if not res and self.session and self.sql_conn() is None:
            res = [(e.name, 0, []) for e in self._entries("index") if e.tbl_name == tbl]
        return res

    def fkeys(self, tbl):
        return [(r[2], r[3], r[4]) for r in self._pragma("PRAGMA foreign_key_list(%s)" % quote_ident(tbl))]

    def fkeys_full(self, tbl):
        return [{"id": r[0], "seq": r[1], "table": r[2], "from": r[3], "to": r[4],
                 "on_update": r[5] if r[5] != "NO ACTION" else "",
                 "on_delete": r[6] if r[6] != "NO ACTION" else ""}
                for r in self._pragma("PRAGMA foreign_key_list(%s)" % quote_ident(tbl))]

    def check_constraints(self, tbl):
        return re.findall(r"CHECK\s*\(([^)]+)\)", self.create_sql(tbl), re.IGNORECASE)

    def meta(self):
        if not self.session:
            return {}
        s = self.session
        h = s.pager.header
        return {"path": s.evidence.main, "size": s.evidence.fingerprints["main"].size,
                "page_size": s.pager.page_size, "page_count": s.pager.page_count,
                "journal_mode": "wal" if h.is_wal_mode else "rollback",
                "encoding": {"utf-8": "UTF-8", "utf-16-le": "UTF-16le",
                             "utf-16-be": "UTF-16be"}.get(h.encoding, h.encoding),
                "auto_vacuum": "yes" if h.largest_root else "none",
                "user_version": h.user_version, "freelist_count": h.freelist_count,
                "mode": s.mode, "reserved_bytes": h.reserved}

    def integrity(self):
        rows = self._pragma("PRAGMA quick_check(1)")
        return rows[0][0] if rows else "not available (SQLite cannot read this file)"

    def count(self, tbl):
        """Exact row count; errors propagate (the UI shows '?' for a table it cannot count)."""
        return self.session.count(tbl) if self.session else 0

    def approx_count(self, tbl):
        """Instant max(rowid) estimate for SQL-served rowid tables, else None."""
        s = self.session
        if not s:
            return None
        try:
            t = s.info(tbl)
            if s.source(tbl) != "sql" or t.kind != "table" or t.without_rowid or not t.rowid_name:
                return None
            r = s.conn().execute("SELECT max(%s) FROM %s" % (t.rowid_name, quote_ident(tbl))).fetchone()
            return r[0] or 0
        except Exception:
            return None

    # -- rows ----------------------------------------------------------------
    def browse(self, tbl, lim, off, ocol=None, odir="ASC", flt=None):
        """(columns, rows) with a leading '_rid' column holding the row Locator.

        Rows are BrowseRow lists carrying the row flags; self.last_page keeps the engine Page
        (its note says why a page is empty, capped, or has uncomputable columns).
        """
        if not self.session:
            return [], []
        order = None if ocol in (None, RID) else ocol   # '_rid' = natural (rowid / PK) order
        page = self.session.browse(tbl, off, lim, order, odir == "DESC", flt)
        self.last_page = page
        return [RID] + list(page.columns), [BrowseRow([r.locator] + list(r.values), r.flags)
                                             for r in page.rows]

    def browse_window(self, tbl, start, count, ocol=None, desc=False, flt=None):
        """One window of rows for the Browse grid: (columns, rows, note).

        Like browse() (rows are BrowseRow [locator, value, ...] with flags) but the page note
        is returned with the rows instead of kept in self.last_page, so a worker thread can
        read windows while the Tk thread reads others.
        """
        if not self.session:
            return [], [], ""
        order = None if ocol in (None, RID) else ocol
        page = self.session.browse(tbl, start, count, order, desc, flt)
        return ([RID] + list(page.columns),
                [BrowseRow([r.locator] + list(r.values), r.flags) for r in page.rows], page.note)

    def build_positions(self, tbl, ocol=None, desc=False, flt=None, cancel=None):
        """Index where the rows of a Browse view are, so windows anywhere read fast (slow:
        on a worker thread). Returns the view's row count, or None (nothing to index)."""
        if not self.session:
            return None
        order = None if ocol in (None, RID) else ocol
        return self.session.build_positions(tbl, order, desc, flt, cancel)

    def count_filtered(self, tbl, flt):
        """Rows of a table or view passing a Filter (what browse_window can show for it)."""
        return self.session.count(tbl, flt) if self.session else 0

    def iter_filtered(self, tbl, flt=None, ocol=None, desc=False):
        """Every row passing a Filter, in the Browse order, as BrowseRow [locator, values...]."""
        order = None if ocol in (None, RID) else ocol
        for r in self.session.iter_rows(tbl, flt, order, desc):
            yield BrowseRow([r.locator] + list(r.values), r.flags)

    @property
    def encoding(self):
        return self.session.encoding if self.session else "utf-8"

    def full_row(self, tbl, rid):
        if not self.session:
            return {}, []
        loc = as_locator(rid)
        row = self.session.row(tbl, loc) if loc is not None else None
        if row is None:
            return {}, []
        snap = row.locator.snapshot    # views / virtual tables: the columns the row was read with
        cols = [RID] + (list(snap[0]) if snap is not None else self.session.visible_columns(tbl))
        return RowData(zip(cols, [row.locator] + list(row.values)), row.flags), cols

    def has_blob_values(self, tbl, cancel=None):
        """Whether any column of tbl holds a BLOB value, whatever its declared type (a TEXT
        or INTEGER column may hold one). SQL-served tables ask SQLite (stops at the first
        one; interrupt() stops it); others read the rows until one is found. cancel() true
        stops early (False)."""
        s = self.session
        if not s:
            return False
        try:
            cols = s.visible_columns(tbl)
            if cols and s.source(tbl) == "sql":
                cond = " OR ".join("typeof(%s)='blob'" % quote_ident(c) for c in cols)
                row = s.conn().execute("SELECT 1 FROM %s WHERE %s LIMIT 1"
                                       % (quote_ident(tbl), cond)).fetchone()
                return row is not None
        except sqlite3.Error:
            if cancel is not None and cancel():
                return False            # interrupted: another table was chosen
        except Exception:               # noqa: BLE001 - read the rows below
            pass
        n = 0
        for r in s.iter_rows(tbl):
            if any(isinstance(v, (bytes, bytearray)) for v in r.values):
                return True
            n += 1
            if n % 2000 == 0 and cancel is not None and cancel():
                return False
        return False

    def iter_rows(self, tbl):
        """Every row as [Locator, values...] (exports)."""
        for r in self.session.iter_rows(tbl):
            yield [r.locator] + list(r.values)

    def search(self, tbl, cols, term, mode, limit, deep_blob, cancel, decoded=False):
        if not self.session or not term:
            return
        for hit in self.session.search(tbl, term, search_mode(mode), limit, deep_blob, cancel,
                                       decoded=decoded):
            hit["rowid"] = hit["locator"] if hit["locator"] is not None else "-"
            yield hit

    def search_tables(self, tables, term, mode, limit, deep_blob, cancel, decoded=False):
        """Search tables in parallel; yield (table, hits, error) as each one finishes.
        A term the mode cannot search raises ValueError (e.g. malformed hex) before any table."""
        if not self.session or not term:
            return
        for tbl, hits, err in self.session.search_tables(tables, term, search_mode(mode),
                                                         limit, deep_blob, cancel, decoded=decoded):
            for hit in hits:
                hit["rowid"] = hit["locator"] if hit["locator"] is not None else "-"
            yield tbl, hits, err

    def search_freelist(self, term, mode, limit=500, deep_blob=False, cancel=None, decoded=False):
        """Search the records still held by freed pages with the table-search rules.

        The records are those the Forensics carver recovers from freed pages (freed_page_
        records), so they have its one confidence and reasons. Hits are those of a table search
        plus source 'Freelist', 'page' and 'cell_offset' (where the record's cell is),
        'confidence' ('high', 'medium' or 'low') and 'reasons'. 'rowid' is the recorded rowid
        ('-' when unknown); 'locator' is an ordinal Locator carrying the record's columns and
        values. At most `limit` records (None: all).
        """
        mode = search_mode(mode)
        if not self.session or not term or mode == "col":
            return
        matcher = self.session.matcher(term, mode, deep_blob, decoded)
        for hit in search_records(self._freelist_search_records(cancel), matcher, limit, cancel):
            yield hit

    def recovered_records(self, cancel=None):
        """{source: callable() -> engine.search record dicts} of the records outside the
        tables, for engine.value_search: 'WAL' (each distinct row version held in WAL frames)
        and 'Freelist' (records still in freed pages). Each record's provenance also carries
        its 'columns' (and a WAL record its 'wal_record', for tagging)."""
        out = {}
        if self.has_wal:
            def wal_records():
                seen = set()
                for rec in self.wal.recover_all_records(cancel=cancel):
                    try:
                        key = (rec["table"], rec["locator"], tuple(rec["raw_values"]))
                    except TypeError:
                        key = id(rec)
                    if key in seen:
                        continue
                    seen.add(key)
                    cols = list(rec["values_dict"].keys())
                    yield {"table": rec["table"], "columns": cols, "values": rec["raw_values"],
                           "locator": rec["locator"], "rowid": rec["rowid"], "source": "WAL",
                           "provenance": {"frame": rec["frame_idx"], "page": rec["page_num"],
                                          "frame_state": rec["category"], "columns": cols,
                                          "flags": rec.get("flags") or (), "wal_record": rec}}
            out["WAL"] = wal_records
        if self.session is not None and self.freelist_count():
            def freelist_records():
                for rec in self._freelist_search_records(cancel):
                    rec = dict(rec)
                    rec["provenance"] = dict(rec["provenance"], columns=list(rec["columns"]))
                    yield rec
            out["Freelist"] = freelist_records
        return out

    def freed_page_records(self, cancel=None):
        """The records still held by freed pages (the freelist), as the Forensics carver
        recovers them: [engine.forensics Record], each with one confidence ('high', 'medium',
        'low') and its reasons, the same as Forensics > Recovered Records. Rows identical to a
        live row are left out. Kept once read completely (cancel() true stops early)."""
        if not self.session or not self.freelist_count():
            return []
        cached = getattr(self, "_freed_cache", None)
        if cached is not None and cached[0] is self.session:
            return cached[1]
        res = self.session.forensics.carve(sources=["freelist"], cancel=cancel,
                                           index_entries=False)
        recs = list(res)
        if getattr(res, "complete", True):
            self._freed_cache = (self.session, recs)
        return recs

    def freed_record(self, record_id):
        """The freed-page Record with this id from the records already recovered (never
        recovers them again: None when they are not at hand)."""
        cached = getattr(self, "_freed_cache", None)
        if cached is None or cached[0] is not self.session:
            return None
        return next((r for r in cached[1] if r.id == record_id), None)

    def _freelist_search_records(self, cancel=None):
        s = self.session
        for r in self.freed_page_records(cancel):
            cols = list(r.columns) or ["col%d" % i for i in range(len(r.values))]
            t = s.schema.get(r.table) if r.table else None
            decl = [c.decl_type for c in t.columns] \
                if t is not None and len(t.columns) == len(cols) else []
            yield {"table": r.table or "(unknown table)", "columns": cols,
                   "decl_types": decl, "values": list(r.values),
                   "rowid": r.rowid if r.rowid is not None else "-",
                   "source": "Freelist",
                   "provenance": {"page": r.prov.page, "cell_offset": r.prov.offset,
                                  "confidence": r.confidence, "reasons": list(r.reasons),
                                  "record_id": r.id}}

    # -- WAL-only tables -------------------------------------------------------
    def wal_tables(self):
        if not self.has_wal:
            return []
        return sorted(self._wal_adapter.wal_only_tables - set(self.tables())
                      - {"sqlite_master", "sqlite_sequence"})

    def wal_browse_columns(self, table_name):
        """The Browse columns of a WAL-only table: _rid, its columns, then where each record
        is (_wal_frame, _wal_page, _wal_status)."""
        cols = self._wal_adapter.col_map.get(table_name, []) if self.has_wal else []
        return ["_rid"] + list(cols) + ["_wal_frame", "_wal_page", "_wal_status"]

    def wal_browse(self, table_name, limit=None, offset=0, cancel=None):
        """(columns, rows, total) of a WAL-only table: every record its WAL frames hold, as
        BrowseRows of the values as stored (NULL stays None, a REAL keeps every digit, a BLOB
        its bytes; the grid formats them when drawing). A column a record does not have (added
        later by ALTER TABLE) reads NULL. limit None: every record. cancel() true stops early
        and returns what was read (total is then that count)."""
        from constants import wal_state_label
        if not self.has_wal:
            return [], [], 0
        recs = list(self._wal_adapter.recover_all_records(table_filter=table_name,
                                                          cancel=cancel))
        recs.sort(key=lambda r: (str(type(r["rowid"])), r["rowid"]))
        cols = self._wal_adapter.col_map.get(table_name, [])
        full = self.wal_browse_columns(table_name)
        end = None if limit is None else offset + limit
        rows = []
        for r in recs[offset:end]:
            stored = dict(zip(r["values_dict"].keys(), r["raw_values"]))
            rows.append(BrowseRow([r["locator"]] + [stored.get(c) for c in cols]
                                  + [r["frame_idx"], r["page_num"],
                                     wal_state_label(r["category"])], r["flags"]))
        return full, rows, len(recs)

    # -- freed pages (the freelist; Forensics > Freed Pages) ----------------------
    def freelist_count(self):
        return self.session.pager.header.freelist_count if self.session else 0

    def freelist_page_numbers(self):
        """(trunk page numbers, leaf page numbers) of the freelist, following its chain."""
        if not self.session:
            return [], []
        return freelist_pages(self.session.pager, self.session.issues)

    def page_bytes(self, n):
        """The bytes of page n of the main file as the database has it now (WAL applied)."""
        return bytes(self.session.pager.page(n)) if self.session else b""

    def read_freelist_pages(self):
        if not self.session:
            return []
        pager = self.session.pager
        trunks, leaves = freelist_pages(pager, self.session.issues)
        out = []
        for n in leaves:
            data = pager.page(n)
            pt = data[0]
            out.append({"page_num": n, "page_type_byte": pt,
                        "page_type": PAGE_TYPES.get(pt, "Unknown (0x%02X)" % pt),
                        "page_data": data, "is_trunk": False})
        for n in trunks:
            out.append({"page_num": n, "page_type_byte": 0, "page_type": "Freelist Trunk",
                        "page_data": pager.page(n), "is_trunk": True})
        return out

    @staticmethod
    def _dt(v):
        return value_type(v)

    # -- deep forensics (Forensics tab) ------------------------------------------
    # Thin wrappers over session.forensics (engine.forensics.Forensics). Long-running calls take
    # cancel() and progress(done, total) callbacks and belong on a worker thread.

    def carve_records(self, tables=None, sources=None, cancel=None, progress=None,
                      time_limit=None, index_entries=True):
        """Deleted and older records (engine.forensics Records, with .stats and .complete);
        index_entries: also deleted entries of the tables' indexes."""
        if not self.session:
            return []
        return self.session.forensics.carve(tables, sources, cancel, progress, time_limit,
                                            index_entries=index_entries)

    @staticmethod
    def record_rows(records):
        """Display dicts for a grid: one per Record (values formatted for display)."""
        out = []
        for r in records:
            p = r.prov
            out.append({"id": r.id, "table": r.table or "(unknown table)", "rowid": r.rowid,
                        "confidence": r.confidence, "source": p.source, "file": p.file,
                        "page": p.page, "offset": p.offset, "frame": p.frame,
                        "frame_state": p.frame_state, "where": p.where(),
                        "flags": sorted(r.flags), "reasons": list(r.reasons),
                        "columns": list(r.columns), "values": [_display(v) for v in r.values],
                        "values_dict": r.values_dict(), "raw_values": list(r.values),
                        "copies": [c.where() for c in r.copies], "record": r})
        return out

    def row_history(self, tbl, rid, cancel=None):
        """Every version of one row (RowHistory: .versions, .deleted, .reuse, .current)."""
        loc = as_locator(rid)
        if not self.session or loc is None:
            return None
        return self.session.forensics.row_history(tbl, loc, cancel)

    def history_summary(self, tbl, cancel=None):
        """Keys of a table with several versions, deleted keys and reused keys."""
        return self.session.forensics.history_summary(tbl, cancel) if self.session else None

    def dropped_schema(self):
        """Recovered dropped/redefined schema objects ([DroppedObject])."""
        return self.session.forensics.dropped_schema() if self.session else []

    def journal_info(self):
        """Rollback journal summary dict, or None when there is no journal."""
        if not self.session:
            return None
        j = self.session.forensics.journal()
        return j.summary() if j is not None else None

    def journal_rows(self, tbl, limit=None):
        """[(Locator, row, flags)] of a table before the journaled transaction."""
        return self.session.forensics.journal_rows(tbl, limit) if self.session else []

    def audit_findings(self):
        """Consistency / anti-forensics findings ([Finding])."""
        return self.session.forensics.audit() if self.session else []

    def write_forensic_report(self, path, fmt, records=None, findings=None, dropped=None,
                              history=None):
        """Write an HTML/CSV/JSON report (refused inside the evidence folder); returns the path."""
        from constants import VERSION
        if not self.session:
            raise ValueError("no database open")
        return self.session.forensics.write_report(path, fmt, VERSION, records=records,
                                                   findings=findings, dropped=dropped,
                                                   history=history)


def _display(v):
    if v is None:
        return "NULL"
    if isinstance(v, InvalidText):
        return "[invalid text: %d bytes]" % len(v)
    if isinstance(v, bytes):
        return "[BLOB: %s, %s]" % (fmtb(len(v)), blob_type(v))
    if isinstance(v, float):
        return "%.6g" % v
    s = str(v)
    return s if len(s) <= 200 else s[:200] + "..."
