"""Deep forensics over an open Session: deleted-record carving, row version history, dropped
schema recovery, rollback-journal pre-state, consistency audit and reports.

Everything reads the evidence through the session (read-only maps of the main file and the
WAL; the journal through short-lived read-only handles) and keeps results in memory. Reports
are written only to a path the caller chooses outside the evidence folder.

    fx = session.forensics
    fx.carve()                      -> CarveResult (list of Record)
    fx.row_history(table, locator)  -> RowHistory
    fx.history_summary(table)       -> HistorySummary
    fx.dropped_schema()             -> [DroppedObject]
    fx.journal()                    -> Journal or None
    fx.journal_rows(table)          -> [(Locator, row, flags)] of the pre-transaction state
    fx.audit()                      -> [Finding]
    fx.write_report(path, fmt, version, ...)
"""

import itertools
import threading
import time

from ..backends import NativeTable
from ..fileformat.btree import BTreeReader
from ..issues import IssueLog
from ..schema import SchemaModel, TableInfo, describe_table
from .audit import Finding, audit as _audit
from .carve import CarveResult, Carver, MAX_RECORDS
from .history import Historian, HistorySummary, RowHistory, Version
from .index_carve import (IndexCarver, LiveIndexEntries, index_specs, index_templates,
                          learn_index_lengths, link_records, reference_rows)
from .journal import Journal, open_journal
from .live import LiveIndex
from .pages import MainFileView, PageMap, WalTimeline
from .provenance import (CARVE_SOURCES, CONFIDENCES, SOURCES, Provenance, Record, json_value)
from .report import FORMATS, ReportError, check_target, write_report as _write_report
from .schema_recovery import DroppedObject, recover as _recover_schema
from .templates import templates_for

__all__ = ["Forensics", "Record", "Provenance", "CarveResult", "RowHistory", "Version",
           "HistorySummary", "DroppedObject", "Journal", "Finding", "ReportError",
           "SOURCES", "CARVE_SOURCES", "CONFIDENCES", "FORMATS", "forensics_for", "json_value"]

PENDING_BYTE = 0x40000000
_LOCK = threading.Lock()


def forensics_for(session):
    """The session's Forensics object (one per session, created on first use)."""
    with _LOCK:
        fx = session.__dict__.get("_forensics")
        if fx is None:
            fx = session.__dict__["_forensics"] = Forensics(session)
        return fx


class Forensics(object):
    def __init__(self, session):
        self.session = session
        self.issues = IssueLog()      # forensic scan problems (listed under Issues too)
        self._lock = threading.RLock()
        self._cache = {}

    # -- shared structures (built on first use) ------------------------------
    def _get(self, name, make):
        with self._lock:
            if name not in self._cache:
                self._cache[name] = make()
            return self._cache[name]

    @property
    def main(self):
        """The main file as stored (no WAL applied)."""
        return self._get("main", lambda: MainFileView(self.session.pager))

    @property
    def timeline(self):
        return self._get("timeline", lambda: WalTimeline(self.session.wal))

    @property
    def eff_map(self):
        """Page roles in the current state (WAL applied)."""
        return self._get("eff_map", lambda: PageMap(self.session.pager, self.session.schema.entries,
                                                    self.issues))

    @property
    def main_map(self):
        """Page roles in the main file alone (the same as eff_map without committed WAL frames)."""
        def make():
            wal = self.session.wal
            if wal is None or not wal.overlay:
                return self.eff_map
            return PageMap(self.main, self.main_entries(), self.issues)
        return self._get("main_map", make)

    def main_entries(self):
        """sqlite_master rows as the main file alone has them."""
        def make():
            wal = self.session.wal
            if wal is None or not wal.overlay:
                return list(self.session.schema.entries)
            try:
                return SchemaModel.read_master(self.main, self.issues)
            except Exception as e:
                self.issues.add("forensics_main_schema", str(e), "main file", "info")
                return []
        return self._get("main_entries", make)

    @property
    def live(self):
        return self._get("live", lambda: LiveIndex(self.session))

    @property
    def lock_byte_page(self):
        return PENDING_BYTE // self.session.pager.page_size + 1

    def journal(self):
        """The rollback journal (hot or not) as a Journal, or None when there is none."""
        def make():
            p = self.session.pager
            max_page = max(self.main.file_pages, self.main.page_count, p.page_count) + 16
            return open_journal(self.session.evidence, p.page_size, max_page, self.issues)
        return self._get("journal", make)

    def journal_view(self):
        """Page view of the pre-transaction state (journal images over the main file)."""
        def make():
            j = self.journal()
            return j.pre_state_view(self.main) if j is not None else None
        return self._get("journal_view", make)

    # -- carving -----------------------------------------------------------------
    def templates(self, tables=None, dropped=None):
        """Carving templates: every table, the given dropped objects' tables, sqlite_master."""
        infos = [d.info for d in (dropped or []) if d.info is not None and d.status == "dropped"]
        tpls = templates_for(self.session.schema, infos)
        if tables is not None:
            wanted = set(tables)
            tpls = [t for t in tpls if t.name in wanted or t.kind == "master"]
        for t in tpls:
            if t.kind == "table" and not t.dropped:
                t.learn_lengths(self._sample_rows(t.info))
        return tpls

    def _sample_rows(self, info, count=48):
        """A few live rows from both ends of a table (declared column order)."""
        s = self.session
        rows = []
        try:
            names = s.visible_columns(info.name)
            pos = [names.index(c) if c in names else None for c in info.column_names]
            for desc in (False, True):
                for r in s.browse(info.name, 0, count, desc=desc).rows:
                    rows.append([r.values[p] if p is not None and p < len(r.values) else None
                                 for p in pos])
        except Exception:
            try:
                it = NativeTable(info, s.pager, IssueLog()).iter_all()
                rows = [r for _l, r, _f in itertools.islice(it, count)]
            except Exception:
                rows = []
        return rows

    def carve(self, tables=None, sources=None, cancel=None, progress=None, time_limit=None,
              include_schema=False, unattributed=True, max_records=None,
              index_entries=True):
        """Deleted and older records from free space, freed pages, WAL frames and the journal.

        tables: limit to these table names (None: all, plus dropped tables whose schema was
        recovered). sources: subset of CARVE_SOURCES. cancel() -> True stops early;
        progress(done, total) is called now and then; time_limit in seconds bounds the whole
        call (schema recovery included). index_entries: also recover deleted entries of the
        tables' indexes (Records flagged 'index_entry', see index_carve). Returns a
        CarveResult (a list of Record with .stats and .complete).
        """
        start = time.time()
        deadline = start + time_limit if time_limit else None
        if max_records is None:
            from .. import limits
            max_records = limits.get("carve_max_records")

        def stop():
            return (cancel is not None and cancel()) or \
                (deadline is not None and time.time() > deadline)
        dropped = self.dropped_schema(cancel, 0.25 * time_limit if time_limit else None)
        hints = self.dropped_pages(dropped)
        specs = self.index_specs(tables, dropped) if index_entries else []
        if progress is not None and specs:
            # the table pass reports 0..total, the index pass total..2*total
            table_progress = lambda d, t: progress(d, 2 * t)
            index_progress = lambda d, t: progress(t + d, 2 * t)
        else:
            table_progress = index_progress = progress
        self.live.cancel = stop
        try:
            carver = Carver(self, self.templates(tables, dropped), sources, cancel,
                            table_progress, deadline, max_records, include_schema,
                            unattributed and tables is None, page_hints=hints)
            result = carver.run()
        finally:
            self.live.cancel = None
        self._take_issues(carver.issues)
        if self.live.capped:
            # tables too large to hash: their recovered rows without a rowid were not compared
            # with the live rows (copies of live rows may be listed; nothing is left out)
            result.stats["live_check_skipped"] = sorted(self.live.capped)
        if specs:
            result = self._carve_indexes(result, specs, sources, cancel, index_progress,
                                         deadline, max_records, hints, stop)
        result.stats["seconds"] = round(time.time() - start, 3)
        return result

    def index_specs(self, tables=None, dropped=None):
        """IndexSpec of every index of the (given) tables, and of recovered dropped indexes."""
        if dropped is None:
            dropped = self.dropped_schema()
        return index_specs(self.session.schema, self.session.schema.entries, self.issues,
                           tables, dropped)

    def _carve_indexes(self, result, specs, sources, cancel, progress, deadline, max_records,
                       hints, stop):
        live = self._get("live_index", lambda: LiveIndexEntries(self.session))
        templates = index_templates(specs)
        live.cancel = stop
        try:
            rows = {}
            for spec in specs:
                if spec.table not in rows and not spec.dropped:
                    rows[spec.table] = self._sample_rows(spec.info)
            reference = reference_rows(result, self.session.schema, specs)
            learn_index_lengths(templates, live, rows, reference)
            carver = IndexCarver(self, templates, live, sources, cancel, progress, deadline,
                                 max(0, max_records - len(result)), hints, reference)
            found = carver.run()
        finally:
            live.cancel = None
        self._take_issues(carver.issues)
        records = list(result) + list(found)
        link_records(records, self.session)
        stats = dict(result.stats)
        for k in ("candidates", "live_skipped", "merged", "errors"):
            stats[k] = stats.get(k, 0) + found.stats.get(k, 0)
        stats["records"] = len(records)
        stats["index_pages"] = found.stats.get("index_pages", 0)
        stats["index_entries"] = found.stats.get("index_entries", 0)
        stats["indexes"] = len(specs)
        if found.stats.get("stopped") and not stats.get("stopped"):
            stats["stopped"] = found.stats["stopped"]
        if live.capped:
            # indexes too large to hash: their recovered entries were not compared with the
            # live entries (copies of live entries may be listed; nothing is left out)
            stats["live_index_check_skipped"] = sorted(live.capped)
        return CarveResult(records, stats, result.complete and found.complete)

    def _take_issues(self, log):
        """Keep a scan's issues (and the count of those it could not keep) in self.issues."""
        for i in log:
            self.issues.add(i.kind, i.detail, i.where, i.severity)
        self.issues.dropped += log.dropped

    def issue_logs(self):
        """Every log of problems the forensic scans met so far: shown with the database's
        Issues."""
        logs = [self.issues]
        with self._lock:
            for name in ("live", "live_index", "historian"):
                obj = self._cache.get(name)
                if obj is not None and getattr(obj, "issues", None) is not None:
                    logs.append(obj.issues)
        return logs

    def dropped_pages(self, dropped=None):
        """{page: table} for pages of dropped tables that no live tree uses now (the dropped
        root page and what can still be walked from it)."""
        hints = {}
        reader = BTreeReader(self.session.pager, IssueLog())
        for d in (self.dropped_schema() if dropped is None else dropped):
            if d.type != "table" or d.status != "dropped" or not d.rootpage:
                continue
            pages = set([d.rootpage])
            try:
                pages |= reader.tree_pages(d.rootpage)
            except Exception:
                pass
            for p in pages:
                if p not in self.eff_map.owner:
                    hints.setdefault(p, d.name)
        return hints

    def dropped_schema(self, cancel=None, time_limit=None):
        """Schema objects (tables, indexes, views, triggers) that were dropped or redefined."""
        with self._lock:
            if "dropped" in self._cache:
                return self._cache["dropped"]
        deadline = time.time() + time_limit if time_limit else None
        objs, result = _recover_schema(self, cancel, deadline)
        if result.complete:
            with self._lock:
                self._cache["dropped"] = objs
        return objs

    # -- history -------------------------------------------------------------------
    @property
    def historian(self):
        return self._get("historian", lambda: Historian(self))

    def row_history(self, table, locator, cancel=None):
        """Every version of one row (Locator('rowid', n) or Locator('pk', (...)))."""
        return self.historian.row_history(table, locator, cancel)

    def history_summary(self, table, cancel=None):
        """Keys of a table with several versions, deleted keys and reused keys."""
        return self.historian.summary(table, cancel)

    # -- journal ---------------------------------------------------------------------
    def journal_schema(self):
        """sqlite_master rows of the pre-transaction state, or [] without a journal."""
        view = self.journal_view()
        if view is None:
            return []
        try:
            return SchemaModel.read_master(view, IssueLog())
        except Exception:
            return []

    def journal_rows(self, table, limit=None):
        """[(Locator, row, flags)] of a table as it was before the journaled transaction."""
        view = self.journal_view()
        if view is None:
            return []
        info = None
        for e in self.journal_schema():
            if e.type == "table" and e.name == table:
                cur = self.session.schema.get(table)
                if cur is not None and cur.sql == e.sql and cur.root_page == e.rootpage:
                    info = cur
                else:
                    info = TableInfo(e.name, "table", e.rootpage, e.sql)
                    describe_table(info, self.session.schema.collations)
                break
        if info is None or not info.natively_readable:
            return []
        out = []
        for item in NativeTable(info, view, IssueLog()).iter_all():
            out.append(item)
            if limit is not None and len(out) >= limit:
                break
        return out

    # -- audit and report ------------------------------------------------------------
    def audit(self, time_limit=30.0, include_dropped=True):
        """Consistency and anti-forensics findings ([Finding])."""
        dropped = None
        if include_dropped:
            try:
                dropped = self.dropped_schema(time_limit=time_limit)
            except Exception as e:
                self.issues.add("forensics_dropped", str(e), "", "info")
        return _audit(self, time_limit, dropped)

    def write_report(self, path, fmt, version, records=None, findings=None, dropped=None,
                     history=None, carve_stats=None, title=None):
        """Write an HTML, CSV or JSON report to `path` (never inside the evidence folder).

        records: Records to include (e.g. a carve() result); findings: audit() output (run
        when None); dropped: dropped_schema() output (run when None); history: list of
        HistorySummary. Returns the path; raises ReportError when the location is refused.
        """
        check_target(self.session, path)          # refuse before any slow work
        if findings is None:
            findings = self.audit()
        if dropped is None:
            dropped = self.dropped_schema()
        if carve_stats is None and isinstance(records, CarveResult):
            carve_stats = records.stats
        return _write_report(self, path, fmt, version, records=records, findings=findings,
                             dropped=dropped, history=history, carve_stats=carve_stats,
                             title=title)
