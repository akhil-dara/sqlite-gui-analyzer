"""Scripted UI smoke run: open DBs (temp copies), drive every tab, record Tk exceptions,
report what each widget shows (no screen capture), and verify the evidence folders are untouched.

usage: python tools/ui_smoke.py [<db> ...]
With no arguments the standard test fixtures are built and used. Each database is copied to a
temp folder first; the tool never touches the originals.
"""
import os, sys, shutil, tempfile, threading, traceback, time, hashlib, csv, datetime, json
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
# Tags, saved view state and the Recent list go to a temporary app-data folder, never the
# user's profile (engine.tags reads SGA_DATA_DIR).
DATA_DIR = tempfile.mkdtemp(prefix="sga_smoke_data_")
os.environ["SGA_DATA_DIR"] = DATA_DIR
DBS = sys.argv[1:]
if not DBS:
    # No arguments: build the standard fixtures (WITHOUT ROWID, WAL states, corrupt root page,
    # freelist, quirks, and a file SQLite refuses with damaged records) in a temp folder and
    # drive the UI over them.
    sys.path.insert(0, ROOT)
    from tests.fixtures import make_fixtures as fx
    from tests.fixtures import forensic_fixtures as ffx
    _fx_dir = tempfile.mkdtemp(prefix="sga_smoke_fx_")
    DBS = [fx.without_rowid(_fx_dir), fx.wal_states(_fx_dir), fx.corrupt(_fx_dir),
           fx.freelist(_fx_dir), fx.quirks(_fx_dir), fx.unreadable_by_sqlite(_fx_dir),
           fx.wide(_fx_dir), fx.relations(_fx_dir), ffx.indexed_deleted(_fx_dir)]
    from tests.fixtures import timeline_fixtures as tlfx
    DBS.append(tlfx.build(_fx_dir))

from app import App          # noqa: E402
from constants import ROW_FLAG_BG   # noqa: E402
from dialogs import RowWin   # noqa: E402
from engine.backends import Filter  # noqa: E402
from engine.filters import value_expr   # noqa: E402
from engine.tags import TagStore, group_key, tint   # noqa: E402
from engine import timeline as tl   # noqa: E402
from utils import plain_text, row_flag_tag   # noqa: E402

errors = []


def snapshot(d):
    out = {}
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        st = os.stat(p)
        out[n] = (st.st_size, st.st_mtime_ns, hashlib.sha256(open(p, "rb").read()).hexdigest())
    return out


report = []


def note(tag, text):
    report.append("%s | %s" % (tag, text))


def run_one(app, src_db, idx):
    tmp = tempfile.mkdtemp(prefix="ui_")
    dst = os.path.join(tmp, os.path.basename(src_db))
    shutil.copy2(src_db, dst)
    for sfx in ("-wal", "-shm", "-journal"):
        if os.path.exists(src_db + sfx):
            shutil.copy2(src_db + sfx, dst + sfx)
    before = snapshot(tmp)
    tag = "%02d_%s" % (idx, os.path.basename(src_db).replace(".", "_"))
    steps = []

    def step(fn, label):
        steps.append((fn, label))

    step(lambda: app._open_db(dst, wait=True), "open")
    step(lambda: note(tag, "mode=%s chips=%s" % (app.db.mode, app.status_chips())), "banners")

    grid = app._browse_grid

    def settle(timeout=30):
        """Serve Tk until the Browse grid has read the rows in view and the row count is
        known: its worker threads hand results over through after() polls."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            app.update()
            if not grid.loading() and not app._browse_counter.busy():
                app.update()
                return True
            time.sleep(0.01)
        errors.append((tag, "browse", "grid still loading after %ds" % timeout))
        return False

    def check_browse_visibility(t):
        """Spec 8: a table with rows that shows none must say why; flagged rows are marked."""
        lbl = app._browse_note_lbl
        shown = lbl.cget("text") if lbl.winfo_manager() else ""
        if shown:
            note(tag, "  note: %s" % shown[:150])
        try:
            count = app.db.count(t)
        except Exception:
            count = None
        first, end = grid.visible_row_range()
        if end <= first and count:
            errors.append((tag, "browse " + t, "the table has %d rows, none shown (note %r)"
                           % (count, shown)))
        elif end <= first and count is None and not shown:
            errors.append((tag, "browse " + t, "no rows shown, no count, no note"))
        page_note = app._browse_source.note
        if page_note and page_note not in shown:
            errors.append((tag, "browse " + t, "engine note %r not shown" % page_note))
        damaged = ROW_FLAG_BG["flag_damaged"]
        want = [r for r in range(first, end) if grid.row_data(r) is not None
                and row_flag_tag(grid.row_data(r)[1]) == "flag_damaged"]
        tagged = [r for r in range(first, end) if grid.row_background(r) == damaged]
        if want != tagged or (want and "damaged record" not in shown):
            errors.append((tag, "browse " + t, "damaged rows %s, marked %s, note %r"
                           % (want, tagged, shown)))
        if want:
            note(tag, "  %d damaged row(s) marked" % len(tagged))

    def check_filter_alignment(t):
        """In the optional filter row under the headers, every entry sits exactly over its
        column, after scrolling and resizing."""
        grid.set_inline_filters(True)
        app.update_idletasks()
        try:
            _check_filter_alignment(t)
        finally:
            grid.set_inline_filters(False)

    def _check_filter_alignment(t):
        for label, action in (("scroll", lambda: grid.scroll_x(137)),
                              ("resize", lambda: grid.set_column_width(
                                  grid.visible_columns()[0],
                                  grid.column_width(grid.visible_columns()[0]) + 41))):
            if not grid.visible_columns():
                return
            action()
            grid.redraw_now()
            app.update_idletasks()
            for c in grid.visible_columns():
                e = grid.filter_entry(c)
                got = (e.winfo_x(), e.winfo_width()) if e is not None else None
                if got != (grid.column_x(c), grid.column_width(c)):
                    errors.append((tag, "filter entry " + t, "after %s column %d at %r, entry %r"
                                   % (label, c, (grid.column_x(c), grid.column_width(c)), got)))
                    return
        grid.xview_moveto(0)

    def check_column_filters(t):
        """'>5' on an INTEGER column counts what the engine counts; a bad regex is marked on
        its entry and never raises."""
        if t.startswith("WAL: "):
            return
        ints = [i for i, (name, typ) in enumerate(app.db.columns(t), 1) if "INT" in typ.upper()]
        if not ints or not app.db.count(t):
            return
        c = ints[0]
        name = grid.columns()[c]
        total = app._browse_source.total
        grid.set_filter_text(c, ">5")
        settle()
        got = app._browse_source.row_count()
        want = app.db.count_filtered(t, Filter(col_exprs={name: ">5"}))
        small = app.db.count_filtered(t, Filter(col_exprs={name: "<=5"}))
        note(tag, "  filter %s >5: %r of %r rows (engine %r); status %r"
             % (name, got, total, want, app._browse_status.cget("text")[:70]))
        if got != want or (small and total is not None and not got < total):
            errors.append((tag, "filter " + t, ">5 on %s shows %r rows, engine %r, total %r"
                           % (name, got, want, total)))
        grid.set_filter_text(c, "/[/")
        settle()
        e = grid.filter_entry(c)
        bad = grid.filter_errors()
        if name not in bad or e is None or str(e.cget("style")) != "GridFilterBad.TEntry" \
                or not grid.filter_tip(c).startswith("Cannot use"):
            errors.append((tag, "filter " + t, "invalid regex not marked: %r style %r"
                           % (bad, e.cget("style") if e is not None else None)))
        else:
            note(tag, "  bad regex marked: %s" % bad[name][:70])
        if app._browse_source.row_count() != total:
            errors.append((tag, "filter " + t, "an invalid filter changed the rows"))
        grid.set_filter_text(c, "")
        settle()

    def check_wide(t):
        """A 250-column table draws only the columns in view, before and after scrolling."""
        ncols = len(grid.columns())
        if ncols < 100:
            return
        for where in (0.0, 0.5, 1.0):
            grid.xview_moveto(where)
            grid.redraw_now()
            app.update_idletasks()
            first, end = grid.visible_row_range()
            vis = grid.visible_columns()
            cells = len(grid.text_items("cells"))
            heads = len(grid.text_items("header"))
            note(tag, "  wide at %.1f: %d of %d columns drawn (%d..%d), %d cell + %d header texts"
                 % (where, len(vis), ncols, vis[0] if vis else -1, vis[-1] if vis else -1,
                    cells, heads))
            if not vis or len(vis) > 40 or cells > (end - first) * len(vis) or heads > len(vis):
                errors.append((tag, "wide " + t, "drew %d columns, %d cell texts" % (len(vis), cells)))
        # 50 scroll steps, each a turn of the event loop as when the user scrolls (in one
        # callback they made one long stall the watchdog took for the app's)
        spent = 0.0
        for _ in range(50):
            t0 = time.perf_counter()
            grid.scroll_rows(3)
            app.update_idletasks()
            spent += time.perf_counter() - t0
            app.update()
        note(tag, "  wide scroll: %.1f ms per 3-row step" % (spent * 20))
        grid.xview_moveto(0)

    def check_grid_tools(t):
        """Row detail from the context menu, inspector, column chooser and copy."""
        if not grid.visible_rows_data():
            return
        col = grid.displayed_columns()[-1]
        grid.set_current_cell(0, col)
        grid.redraw_now()
        menu = grid.build_context_menu(0, col)
        labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                  if menu.type(i) == "command"]
        opened = len(RowWin._pool)
        for i in range(menu.index("end") + 1):
            if menu.type(i) == "command" and menu.entrycget(i, "label") == "Open row detail":
                menu.invoke(i)
        if len(RowWin._pool) <= opened and not t.startswith("WAL: "):
            errors.append((tag, "row detail " + t, "no window opened; menu %s" % labels))
        tsv = grid.rows_copy_text("tsv").split("\n")
        if len(tsv) < 2 or not tsv[0].startswith("_rid\t"):
            errors.append((tag, "copy " + t, "TSV %r" % tsv[:2]))
        app._browse_inspector_var.set(True)
        grid.set_inspector(True)
        grid.redraw_now()
        app.update_idletasks()
        if len(grid.inspector_rows()) != len(grid.columns()):
            errors.append((tag, "inspector " + t, "%d lines for %d columns"
                           % (len(grid.inspector_rows()), len(grid.columns()))))
        app._browse_inspector_var.set(False)
        grid.set_inspector(False)
        chooser = grid.column_chooser()
        chooser.set_all(False)
        hidden_all = grid.displayed_columns() == [0]
        chooser.set_all(True)
        chooser.destroy()
        if not hidden_all or grid.hidden_columns():
            errors.append((tag, "column chooser " + t, "hide/show all failed"))

    def browse_one(t):
        app._browse_table_var.set(t)
        app._load_browse_table()
        settle()
        first, end = grid.visible_row_range()
        d = grid.row_data(first) if end > first else None
        note(tag, "browse %-34s rows=%-3d first=%s | %s" % (
            t[:34], end - first, [plain_text(v)[:12] for v in d[0][:3]] if d else "-",
            app._browse_status.cget("text")[:80]))
        check_browse_visibility(t)
        check_filter_alignment(t)
        check_column_filters(t)
        check_wide(t)
        check_grid_tools(t)
        app._browse_filter_var.set("a")
        grid.set_global_filter("a", apply=True)
        settle()
        app._browse_filter_var.set("")
        grid.set_global_filter("", apply=True)
        settle()

    def browse_names():
        return app.db.tables()[:12] + ["WAL: " + w for w in app.db.wal_tables()[:2]]

    def browse_nth(i):
        # one table per step, as the examiner would: the watchdog then measures each one
        app._nb.select(app._browse_frame)       # the grid draws (and reads rows) when shown
        app.update()
        names = browse_names()
        if i < len(names):
            browse_one(names[i])
    for _i in range(14):
        step(lambda i=_i: browse_nth(i), "browse+rowwin")

    def browse_tables():
        for w in list(RowWin._pool.values()):
            warned = [c.cget("text") for c in w._body.winfo_children()
                      if c.winfo_class() == "Label" and c.cget("text").startswith("⚠")]
            note(tag, "RowWin %s fields=%d%s" % (w.title(), len(w._col_widgets),
                                                 (" warning=%r" % warned[0][:60]) if warned else ""))
            if "damaged_record" in (getattr(getattr(w, "_row_data", None), "flags", None) or ()) \
                    and not warned:
                errors.append((tag, "RowWin " + w.title(), "damaged row shown without a warning"))
            w._on_close()
    step(browse_tables, "browse+rowwin")

    def check_grouped_results(term, kids):
        """One line per row: no row twice, every match under its row, and the flat view back."""
        tree = app._search_tree
        groups = app._sr_grouper.groups
        # Every match is under its row, except a WAL copy identical to a matching database row:
        # that one is folded into the row's line as its frames
        listed = set(id(h) for g in groups for h in g.hits)
        db_rows = dict(((g.table, g.locator), g) for g in groups if g.source == "DB")
        for h in app._search_results:
            if id(h) in listed:
                continue
            g = db_rows.get((h["table"], h.get("locator")))
            if not h.get("frames") or g is None or not set(h["frames"]) <= set(g.frames):
                errors.append((tag, "search groups", "match %s.%s %s is on no line"
                               % (h["table"], h["column"], h["rowid"])))
                break
        db_keys = [(g.table, g.locator) for g in groups if g.source == "DB" and g.locator is not None]
        if len(db_keys) != len(set(db_keys)):
            errors.append((tag, "search groups", "a database row is listed more than once"))
        shown = min(len(groups), app._sr_page_size)
        if len(kids) != shown:
            errors.append((tag, "search groups", "page shows %d lines for %d rows" % (len(kids), shown)))
        for iid in kids:
            _hit, g = app._sr_iid_map[iid]
            sub = tree.get_children(iid)
            want = (len(g.hits) if len(g.hits) > 1 else 0) + \
                (1 if g.frames and (g.source == "DB" or len(g.frames) > 1) else 0)
            if len(sub) != want:
                errors.append((tag, "search groups", "row %s has %d sub-lines, expected %d"
                               % (iid, len(sub), want)))
        multi = [g for g in groups if len(g.hits) > 1 or g.frames]
        note(tag, "grouped: %d rows (%d with several matches or WAL copies)%s" % (
            len(groups), len(multi),
            "; e.g. %s | %s | %s" % (multi[0].source_label(), multi[0].columns_label(),
                                     multi[0].frames_label()) if multi else ""))
        app._sr_expand_all(True)
        app._sr_expand_all(False)
        app._sr_group_var.set(False)
        app._filter_search_results()
        flat = len(tree.get_children())
        if flat != min(len(app._search_results), app._sr_page_size):
            errors.append((tag, "search flat", "flat view shows %d lines" % flat))
        app._sr_group_var.set(True)
        app._filter_search_results()
        if kids:                          # the right-click menu builds for a row
            bbox = tree.bbox(kids[0])
            if bbox:
                ev = type("E", (), {"y": bbox[1] + 2, "x_root": -5000, "y_root": -5000})()
                orig = tk.Menu.tk_popup
                tk.Menu.tk_popup = lambda self, x, y: None
                try:
                    app._on_search_rightclick(ev)
                finally:
                    tk.Menu.tk_popup = orig

    def search_cancel():
        # Stop during a parallel search: every worker thread must end promptly.
        app._search_var.set("e")
        app._do_search()
        app._stop_search()
        deadline = time.time() + 10
        while app._search_thread.is_alive() and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        left = [t.name for t in threading.enumerate() if t.name.startswith("search-")]
        if app._search_thread.is_alive() or left:
            errors.append((tag, "search cancel", "still running 10 s after Stop: %s" % left))
    step(search_cancel, "search cancel")

    def search():
        first = next((t for t in app.db.tables() if app.db.count(t)), None)
        term = "a"
        if first:
            _c, rows = app.db.browse(first, 1, 0)
            term = next((str(v)[:6] for v in (rows[0][1:] if rows else [])
                         if isinstance(v, str) and len(v) >= 3), "a")
        app._search_var.set(term)
        if app.db.has_wal:
            app._search_wal_var.set(True)
        app._do_search()
        # Keep the Tk loop running while waiting: the search worker posts results with after().
        deadline = time.time() + 60
        while app._search_thread.is_alive() and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        tree = app._search_tree
        kids = tree.get_children()
        groups = app._sr_grouper.groups
        note(tag, "search %r -> %d matches in %d rows; first=%s" % (
            term, len(app._search_results), len(groups),
            tree.item(kids[0], "values")[1:5] if kids else "-"))
        check_grouped_results(term, kids)
        if kids:
            app._search_tree.selection_set(kids[0])
            app._on_search_dblclick(None)
        for w in list(RowWin._pool.values()):
            w._on_close()
        for w in app.winfo_children():
            if w.winfo_class() == "Toplevel" and not getattr(w, "_closing", False):
                w.destroy()
    step(search, "search")

    def run_search(term, mode, **options):
        app._search_var.set(term)
        app._search_mode_var.set(mode)
        app._deep_blob_var.set(options.get("deep", False))
        app._search_decoded_var.set(options.get("decoded", False))
        app._search_free_var.set(options.get("freelist", False))
        app._search_views_var.set(options.get("views", False))
        app._search_wal_var.set(False)
        before = app._search_thread
        app._do_search()
        if app._search_thread is before:
            return None                   # refused (a malformed term)
        deadline = time.time() + 60
        while app._search_thread.is_alive() and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        return app._search_results

    def search_options():
        # A malformed hex pattern is refused with a message, before any search starts
        if run_search("zz 1", "Byte pattern (hex)") is not None or "Cannot search" not in app._hint_label.cget("text"):
            errors.append((tag, "search hex", "malformed hex pattern was not refused"))
        # Hex bytes of the first BLOB found: its hits say how and where they matched
        blob = None
        for t in app.db.tables():
            for row in app.db.iter_rows(t):
                blob = next((v for v in row[1:] if isinstance(v, bytes) and len(v) >= 3), None)
                if blob is not None:
                    break
            if blob is not None:
                break
        if blob is not None:
            pattern = " ".join("%02x" % b for b in blob[:3])
            hits = run_search(pattern, "Byte pattern (hex)") or []
            labels = set(app._search_tree.item(i, "values")[6] for i in app._search_tree.get_children())
            note(tag, "hex %r -> %d matches, labels %s" % (pattern, len(hits), sorted(labels)[:3]))
            if not hits or not any(h.get("encoding") == "hex" and h.get("offset") is not None
                                   for h in hits):
                errors.append((tag, "search hex", "BLOB bytes %r not found as hex" % pattern))
        # Deleted records in freed pages, and their detail window
        if app.db.freelist_count() > 0 and str(app._search_free_cb.winfo_manager()):
            hits = run_search("e", "Case-Insensitive", freelist=True) or []
            free = [g for g in app._sr_grouper.groups if g.source == "Freelist"]
            note(tag, "freelist search -> %d matches, %d freed records" % (len(hits), len(free)))
            if free:
                win = app._show_record_detail(free[0].first, free[0].first["column"])
                app.update()
                win.destroy()
        run_search("a", "Case-Insensitive", decoded=True)
        if app._search_errors:
            errors.append((tag, "search decoded", "; ".join(app._search_errors[:3])))
        # views: only searched with 'Include views'; their lines are marked and open the row
        views = app.db.views()
        if app._search_views_var.get():
            errors.append((tag, "search views", "'Include views' was left on"))
        run_search("e", "Case-Insensitive")
        if any(h["table"] in views for h in app._search_results):
            errors.append((tag, "search views", "views searched with 'Include views' off"))
        if views:
            hits = run_search("e", "Case-Insensitive", views=True) or []
            in_views = [h for h in hits if h["table"] in views]
            label = next((l for l in app._sr_table_filter.cget("values")
                          if l.rsplit(" (", 1)[0] in views), None)
            if label is not None:               # the view's lines (after the tables')
                app._sr_table_filter.set(label)
                app._filter_search_results()
            shown = [i for i in app._search_tree.get_children()
                     if str(app._search_tree.item(i, "values")[2]).endswith(" (view)")]
            note(tag, "search with views -> %d matches, %d in views, %d view lines shown"
                 % (len(hits), len(in_views), len(shown)))
            if in_views and not shown:
                errors.append((tag, "search views", "view results are not marked"))
            if shown:
                app._search_tree.selection_set(shown[0])
                app._on_search_dblclick(None)
                app.update()
                wins = list(RowWin._pool.values())
                if not wins:
                    errors.append((tag, "search views", "a view result opened no row window"))
                for w in wins:
                    w._on_close()
            app._search_views_var.set(False)
    step(search_options, "search options")

    def forensics():
        # Every Forensics job runs on its worker; the evidence folder must stay untouched
        tab = app._forensics

        def wait(timeout=180):
            deadline = time.time() + timeout
            app.update()
            while tab._busy and time.time() < deadline:
                app.update()
                time.sleep(0.02)
            app.update()
            if tab._busy:
                errors.append((tag, "forensics", "a job was still running after %ds" % timeout))
        app._nb.select(tab)
        tab.carve_time.set("1 minute")
        if not tab.index_entries_var.get():
            errors.append((tag, "forensics", "'Index entries' is not on by default"))
        tab.start_carve()
        wait()
        note(tag, "forensics carve: %s" % tab.carve_status.cget("text"))
        # deleted index entries: listed as 'table [index name]' with indexed values + rowid
        entries = [r for r in tab._records if getattr(r, "index", None) is not None]
        src = tab.carve_grid.source
        labels = set(v[2] for v in src.iter_rows()) if src is not None else set()
        note(tag, "  index entries: %d (%s)" % (len(entries), ", ".join(sorted(
            set(r.index for r in entries)))[:120]))
        if "indexed" in os.path.basename(src_db) and not entries:
            errors.append((tag, "forensics", "no index entries recovered from %s" % src_db))
        if entries and not any("[index " in str(l) for l in labels):
            errors.append((tag, "forensics", "index entries not labelled in the grid"))
        for r in entries:
            if r.columns[-1:] != ["rowid"] and r.rowid is not None or "index_entry" not in r.flags:
                errors.append((tag, "forensics", "odd index entry %r" % r))
                break
        if tab._records:
            tab._open_record(0, [1])
            app.update()
            shown = tab.filtered_records()
            if len(shown) != len(tab._record_view):
                errors.append((tag, "forensics", "grid lists %d of %d records"
                               % (len(shown), len(tab._record_view))))
            # recovered records can be tagged, one by one or all shown
            menu = tk.Menu(app, tearoff=0)
            tab._carve_menu(menu, 0, 0)
            labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                      if menu.type(i) != "separator"]
            if not any(l.startswith("Tag") for l in labels):
                errors.append((tag, "forensics", "no Tag items for a recovered record: %s" % labels))
            before = len(app.tags.store.entries()) if app.tags.store is not None else 0
            tab._tag_all_shown("Review")
            after = app.tags.store.entries() if app.tags.store is not None else []
            carved = [e for e in after if e.source == "Carved"]
            note(tag, "forensics tagged %d recovered records (%d tags before)" % (len(carved), before))
            if len(carved) != len(set(r.id for r in shown)):
                errors.append((tag, "forensics", "%d of %d shown records tagged"
                               % (len(carved), len(shown))))
            app.tags.remove_all(carved)          # leave the store as the tags step expects it
            menu.destroy()
        first = next((t for t in app.db.tables() if app.db.count(t)), None)
        if first:
            # Browse right-click offers the row's history for table rows
            app._browse_table_var.set(first)
            app._load_browse_table()
            deadline = time.time() + 20
            while app._browse_grid.row_data(0) is None and time.time() < deadline:
                app.update()
                time.sleep(0.02)
            menu = tk.Menu(app, tearoff=0)
            app._on_browse_menu(menu, 0, 1)
            labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                      if menu.type(i) != "separator"] if menu.index("end") is not None else []
            loc = (app._browse_grid.row_data(0) or [[None]])[0][0]
            if getattr(loc, "kind", None) in ("rowid", "pk") and \
                    not any(l.startswith("Row history") for l in labels):
                errors.append((tag, "forensics", "no Row History in the Browse menu: %s" % labels))
            menu.destroy()
            tab.history_table.set(first)
            tab.start_history_summary()
            wait()
            _c, rows = app.db.browse(first, 1, 0)
            if rows:
                tab.show_history(first, rows[0][0])
                wait()
                if not tab.versions_tree.get_children():
                    errors.append((tag, "forensics", "no version listed for a live row of %s" % first))
                note(tag, "forensics history: %s" % tab.history_status.cget("text"))
        tab.start_dropped()
        wait()
        tab.start_audit()
        wait()
        note(tag, "forensics: %s | audit %s" % (tab.drop_status.cget("text"),
                                                 tab.audit_status.cget("text")))
        if tab._findings is None:
            errors.append((tag, "forensics", "audit gave no findings list"))
        if str(tab.journal_show.cget("state")) == "normal":
            tab.show_journal_rows()
            wait()
        out = tempfile.mkdtemp(prefix="sga_report_")
        try:
            for fmt in ("html", "csv", "json"):
                p = os.path.join(out, "report." + fmt)
                app.db.write_forensic_report(p, fmt, records=tab.filtered_records()[:300],
                                             findings=tab._findings, dropped=tab._dropped)
                if not os.path.getsize(p):
                    errors.append((tag, "forensics", "empty %s report" % fmt))
            refused = False
            try:
                app.db.write_forensic_report(os.path.join(tmp, "report.html"), "html")
            except Exception:
                refused = True
            if not refused or os.path.exists(os.path.join(tmp, "report.html")):
                errors.append((tag, "forensics", "a report was written into the evidence folder"))
        finally:
            shutil.rmtree(out, ignore_errors=True)
        for w in app.winfo_children():
            if w.winfo_class() == "Toplevel" and not getattr(w, "_closing", False):
                w.destroy()
        for w in tab.winfo_children():
            if w.winfo_class() == "Toplevel" and not getattr(w, "_closing", False):
                w.destroy()
    step(forensics, "forensics tab")
    def wait_jobs(timeout=60):
        """Serve Tk until a tagging or export job ended (its result arrives through after())."""
        deadline = time.time() + timeout
        while app.tags.busy() and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        if app.tags.busy():
            errors.append((tag, "tags", "a tag job still runs after %ds" % timeout))

    def submenu(menu, label):
        for i in range((menu.index("end") or 0) + 1):
            if menu.type(i) == "cascade" and menu.entrycget(i, "label").startswith(label):
                return app.nametowidget(menu.entrycget(i, "menu"))
        return None

    def invoke(menu, label):
        for i in range((menu.index("end") or 0) + 1):
            if menu.type(i) in ("command", "checkbutton") and menu.entrycget(i, "label") == label:
                menu.invoke(i)
                return True
        return False

    def close_windows():
        for w in list(RowWin._pool.values()):
            w._on_close()
        for w in app.winfo_children():
            # a closed row window is already going away, a few lines per turn (RowWin._on_close)
            if w.winfo_class() == "Toplevel" and not getattr(w, "_closing", False):
                w.destroy()

    def tags_check():
        """Tag rows from the Browse menu, with Ctrl+T, all filtered rows, a search result and
        all results; the Tagged tab lists them; export HTML / CSV / JSON (to a temp folder, not
        the evidence folder); close and reopen: tags, table, widths and hidden columns return."""
        tg, store = app.tags, app.tags.store
        table = next((t for t in app.db.tables() if app.db.count(t)), None)
        if table is None or store is None:
            return
        app._nb.select(app._browse_frame)
        app._browse_table_var.set(table)
        app._load_browse_table()
        settle()
        grid.redraw_now()
        col = grid.displayed_columns()[-1]
        grid.set_current_cell(0, col)
        grid.redraw_now()
        # the grid's right-click menu: Tag > Relevant
        sub = submenu(grid.build_context_menu(0, col), "Tag")
        if sub is None or not invoke(sub, "Relevant"):
            errors.append((tag, "tags", "no Tag > Relevant in the Browse menu"))
            return
        key0 = tg.browse_key(table, grid.row_data(0)[0])
        grid.redraw_now()
        red = store.color_of("Relevant")
        if store.tags_of(key0) != ["Relevant"] or grid.row_marker(0) != red:
            errors.append((tag, "tags", "menu: tags %r, marker %r" % (store.tags_of(key0),
                                                                      grid.row_marker(0))))
        # Ctrl+T (the handler its binding calls) on the second row
        if not (grid._cv.bind("<Control-t>") and grid._cv.bind("<Control-Key-2>")):
            errors.append((tag, "tags", "Ctrl+T / Ctrl+2 not bound on the grid"))
        if grid.row_count() > 1:
            grid.set_current_cell(1, col)
            tg.toggle_browse(0)
            grid.set_current_cell(0, col)
            grid.redraw_now()
            key1 = tg.browse_key(table, grid.row_data(1)[0])
            flagged = row_flag_tag(grid.row_data(1)[1]) in ROW_FLAG_BG
            want_bg = grid.row_background(1) if flagged else tint(red)
            if store.tags_of(key1) != ["Relevant"] or grid.row_marker(1) != red \
                    or grid.row_background(1) != want_bg:
                errors.append((tag, "tags", "Ctrl+T: tags %r, marker %r, background %r"
                               % (store.tags_of(key1), grid.row_marker(1),
                                  grid.row_background(1))))
        # every row the filter keeps (a filter by the first row's value)
        c = grid.displayed_columns()[1] if len(grid.displayed_columns()) > 1 else None
        expr = value_expr(grid.row_data(0)[0][c]) if c is not None else None
        if expr:
            grid.set_filter_text(c, expr)
            settle()
            n = app._browse_source.row_count()
            if n is not None and n <= 2000:
                tg.tag_all_filtered("Review", confirm=False)
                wait_jobs()
                got = store.counts()["Review"]
                note(tag, "  tags: 'Review' on all %d filtered rows -> %d tagged" % (n, got))
                if got != n:
                    errors.append((tag, "tags", "tag all filtered: %d rows, %d tagged" % (n, got)))
            grid.set_filter_text(c, "")
            settle()
        # a search result and all results
        _c, rows = app.db.browse(table, 1, 0)
        term = next((str(v)[:6] for v in (rows[0][1:] if rows else [])
                     if isinstance(v, str) and len(v) >= 3), "a")
        run_search(term, "Case-Insensitive", freelist=app.db.freelist_count() > 0)
        groups = [g for g in app._sr_groups_filtered if g.locator is not None
                  or g.source == "Freelist"]
        if groups:
            g = groups[0]
            m = tk.Menu(app, tearoff=0)
            tg.search_menu(m, g, g.first)
            s2 = submenu(m, "Tag")
            if s2 is None or not invoke(s2, "Suspicious") \
                    or "Suspicious" not in store.tags_of(group_key(g)):
                errors.append((tag, "tags", "search result not tagged from its menu"))
            first = app._search_tree.get_children()[0]
            if not app._search_tree.set(first, "Source").startswith("●"):
                errors.append((tag, "tags", "tagged search line not marked: %r"
                               % app._search_tree.set(first, "Source")))
            tg.tag_search_results("Not relevant", confirm=False)
            wait_jobs()
            want = len(set(group_key(g) for g in groups))
            got = store.counts()["Not relevant"]
            note(tag, "  tags: search %r -> %d result rows tagged of %d" % (term, got, want))
            if got != want:
                errors.append((tag, "tags", "tag all results: %d rows, %d tagged" % (want, got)))
        # the Tagged tab
        tab = app._tags_tab
        tab.refresh()
        shown, label = tab.grid.row_count(), app._nb.tab(tab, "text")
        note(tag, "  Tagged tab: %s, %d lines, counts %s" % (label.strip(), shown,
                                                              dict(store.counts())))
        if shown != len(store) or "Tagged (%s)" % format(len(store), ",") not in label:
            errors.append((tag, "tags", "Tagged tab shows %d of %d rows, label %r"
                           % (shown, len(store), label)))
        if shown:
            # tag colours, a column filter, selection and the row actions of the grid
            app._nb.select(tab)
            app.update()
            tab.grid.redraw_now()
            first = tab._entry_of(tab.grid.row_data(0)[0]) if tab.grid.row_data(0) else None
            want = store.color_of(first.tags[0]) if first is not None and first.tags else None
            if tab.grid.row_marker(0) != want:
                errors.append((tag, "tags", "Tagged row marker %r, want %r"
                               % (tab.grid.row_marker(0), want)))
            tab.grid.set_filter_text(2, "=" + first.table if first is not None else "")
            app.update()
            kept = tab.filtered_entries()
            if not kept or any(e.table != first.table for e in kept):
                errors.append((tag, "tags", "Tagged column filter kept %d rows" % len(kept)))
            tab.grid.clear_filters()
            app.update()
            tab.select_rows(0, min(1, shown - 1))
            picked = tab.selected_entries()
            menu = tab.grid.build_context_menu(0, 1)
            items = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                     if menu.type(i) != "separator"]
            note(tag, "  Tagged grid: marker ok, %d selected, menu %s" % (len(picked), items[-3:]))
            if not picked or "Edit note…" not in items:
                errors.append((tag, "tags", "Tagged selection/menu: %d, %s" % (len(picked), items)))
        opened = len(app.winfo_children())
        for e in store.entries()[:3]:
            tg.open_entry(e)
        app.update()
        if len(app.winfo_children()) <= opened:
            errors.append((tag, "tags", "opening tagged rows showed no window"))
        close_windows()
        # exports, to a folder outside the evidence folder
        out = tempfile.mkdtemp(prefix="sga_smoke_export_")
        results = {}
        for fmt, target in (("html", os.path.join(out, "tags.html")),
                            ("json", os.path.join(out, "tags.json")),
                            ("csv", os.path.join(out, "csv"))):
            got = []
            tab.export(fmt, target, on_done=got.append)
            wait_jobs()
            results[fmt] = got[0] if got else None
        sha = app.db.evidence.fingerprints["main"].sha256
        with open(os.path.join(out, "tags.html"), encoding="utf-8") as f:
            html_ok = sha is not None and sha in f.read()
        if results["csv"] is None:
            errors.append((tag, "tags", "the CSV export gave no result"))
            return
        with open(results["csv"]["index"], encoding="utf-8-sig", newline="") as f:
            index_rows = sum(1 for _ in csv.reader(f)) - 1
        again = TagStore(dst, path=os.path.join(out, "reload.json"))
        added = again.merge_file(os.path.join(out, "tags.json"))[0]
        note(tag, "  exports: html hash=%s, csv %d rows + %d BLOB files, json reloads %d"
             % (html_ok, index_rows, results["csv"]["blobs"], added))
        if not html_ok or index_rows != len(store) or added != len(store):
            errors.append((tag, "tags", "exports: html hash %s, csv %d, json %d, tags %d"
                           % (html_ok, index_rows, added, len(store))))
        shutil.rmtree(out, ignore_errors=True)
        # saved view state: a width and a hidden column, then close and reopen
        cols = grid.displayed_columns()
        sized = hidden = None
        if len(cols) >= 3:
            sized, hidden = cols[1], cols[2]
            grid.set_column_width(sized, 177)
            grid.hide_column(hidden)
        keys = set(e.key for e in store.entries())
        app._close_db(confirm=False)
        app._open_db(dst, wait=True)
        settle()
        grid.redraw_now()
        store2 = app.tags.store
        back = (app._browse_table_var.get() == table and store2 is not None
                and set(e.key for e in store2.entries()) == keys
                and grid.row_marker(0) == red)
        if sized is not None:
            back = back and grid.column_width(sized) == 177 and grid.hidden_columns() == {hidden}
        note(tag, "  reopened: table %r, %d tags, width %s, hidden %s, marker %s" % (
            app._browse_table_var.get(), len(store2) if store2 else -1,
            grid.column_width(sized) if sized is not None else "-",
            sorted(grid.hidden_columns()), grid.row_marker(0)))
        if not back:
            errors.append((tag, "tags", "the tags or the view state did not come back"))
        if hidden is not None:
            grid.show_column(hidden)
    step(tags_check, "tags")

    def wait_window(w, timeout=60):
        """Serve Tk until a relationship window's job ended."""
        deadline = time.time() + timeout
        while w.busy() and time.time() < deadline:
            app.update()
            time.sleep(0.01)
        app.update()
        if w.busy():
            errors.append((tag, "relations", "%s still working after %ds" % (w.title(), timeout)))

    def relations():
        """The background map and the Relationships tab (list, diagram, SVG export to a temp
        folder); Related rows and Find this value everywhere from the Browse cell menu (lines,
        rows, Row Detail, Tag all); Column relationships from the header menu; Row Detail's
        Related button."""
        rw = app.relations
        deadline = time.time() + 120
        while rw.mapper.busy() and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        tab = app._relations_tab
        note(tag, "map: %s; tab %r; %d list rows, diagram %d tables"
             % (rw.state[0], tab.status.cget("text")[:90], len(tab.list_rows()),
                len(tab.node_items())))
        if rw.state[0] != "done":
            errors.append((tag, "relations", "mapping did not finish: %r" % (rw.state,)))
        if len(tab.node_items()) != len(tab.shown_tables()):
            errors.append((tag, "relations", "diagram draws %d boxes for %d tables"
                           % (len(tab.node_items()), len(tab.shown_tables()))))
        out = tempfile.mkdtemp(prefix="sga_smoke_map_")
        if not tab.export_svg(os.path.join(out, "map.svg")) or \
                not tab.export_csv(os.path.join(out, "links.csv")):
            errors.append((tag, "relations", "map exports refused"))
        shutil.rmtree(out, ignore_errors=True)
        cands = [t for t in app.db.tables() if rw.supported(t) and app.db.count(t)]
        if "message_poll" in cands:
            cands.remove("message_poll")
            cands.insert(0, "message_poll")
        if not cands:
            return
        table = cands[0]
        app._nb.select(app._browse_frame)
        app._browse_table_var.set(table)
        app._load_browse_table()
        settle()
        grid.redraw_now()
        col = 1 if len(grid.columns()) > 1 else 0
        grid.set_current_cell(0, col)
        grid.redraw_now()
        menu = grid.build_context_menu(0, col)
        labels = [menu.entrycget(i, "label") for i in range((menu.index("end") or 0) + 1)
                  if menu.type(i) != "separator"]
        value = grid.row_data(0)[0][col]
        note(tag, "relations menu for %s.%s: %s; header marks %s"
             % (table, grid.columns()[col], [l for l in labels if "elated" in l or "Find" in l],
                sorted(grid.header_marks())))
        if value not in (None, "", b"") and "Find this value everywhere" not in labels:
            errors.append((tag, "relations", "no Find this value everywhere: %s" % labels))
        if table == "message_poll" and "Related rows" not in labels:
            errors.append((tag, "relations", "message_poll: no Related rows in %s" % labels))
        sub = submenu(menu, "Related rows")
        if sub is not None:
            before = len(rw.windows)
            invoke(sub, "All related rows…")
            if len(rw.windows) != before + 1:
                errors.append((tag, "relations", "All related rows opened no window"))
                return
            w = rw.windows[-1]
            wait_window(w)
            note(tag, "related: %d lines, %d rows; status %r"
                 % (len(w.tree.get_children()), sum(g.count for g in w.groups),
                    w.status.cget("text")[:90]))
            if table == "message_poll":
                got = set((g.table, g.column) for g in w.groups)
                if not set([("message", "_id"),
                            ("message_poll_option", "message_row_id")]) <= got:
                    errors.append((tag, "relations", "message_poll: related %s" % sorted(got)))
            shown = w._current
            if not w.groups or shown is None or any(g.count <= 0 for g in w.groups):
                errors.append((tag, "relations", "empty lines or none selected"))
            else:
                w.grid.redraw_now()
                app.update_idletasks()
                first, end = w.grid.visible_row_range()
                if w.grid.row_count() != len(shown.rows) or end <= first:
                    errors.append((tag, "relations", "grid shows %d rows (%d drawn) for %d"
                                   % (w.grid.row_count(), end - first, len(shown.rows))))
                opened = len(RowWin._pool)
                w._open_row(0, w.grid.row_data(0)[0])
                if len(RowWin._pool) <= opened:
                    errors.append((tag, "relations", "double-click opened no Row Detail"))
                tsub = submenu(w.tag_all_menu(), "Tag all rows found")
                if tsub is None or not invoke(tsub, "Review"):
                    errors.append((tag, "relations", "Tag all rows found: no Review item"))
                elif app.tags.store is not None and not any(
                        e.table == shown.table for e in app.tags.store.entries()):
                    errors.append((tag, "relations", "tagging the related rows tagged none"))
        if invoke(menu, "Find this value everywhere"):
            fw = rw.windows[-1]
            wait_window(fw)
            note(tag, "find %r: %d lines, %d rows; %s; status %r"
                 % (plain_text(value)[:20], len(fw.tree.get_children()),
                    sum(g.count for g in fw.groups), fw.warning.cget("text")[:30],
                    fw.status.cget("text")[:80]))
            if len(fw.tree.get_children()) != len(fw.groups):
                errors.append((tag, "relations", "find: %d lines for %d groups"
                               % (len(fw.tree.get_children()), len(fw.groups))))
        # the column header menu: Column relationships (checked against the values)
        hm = grid.build_header_menu(col)
        if not invoke(hm, "Column relationships…"):
            errors.append((tag, "relations", "no Column relationships in the header menu"))
            return
        cm = rw.windows[-1]
        wait_window(cm)
        unchecked = [r for r in cm.relations if not r.verified]
        note(tag, "column map %s: %d related (%d confident); %r"
             % (cm.title(), len(cm.relations), len(cm.strong()), cm.status.cget("text")[:90]))
        if unchecked:
            errors.append((tag, "relations", "column map: %d unchecked" % len(unchecked)))
        # Row Detail: a Related (n) button only where related rows exist
        for rwin in list(RowWin._pool.values()):
            runner = getattr(rwin, "_related_runner", None)     # the counts run on a worker
            deadline = time.time() + 30
            while runner is not None and runner.busy() and time.time() < deadline:
                app.update()
                time.sleep(0.02)
            app.update()
            buttons = [b for f in rwin._body.winfo_children() for b in f.winfo_children()
                       if b.winfo_class() == "Button" and b.cget("text").startswith("Related (")]
            note(tag, "Row Detail %s: %d Related buttons" % (rwin.title(), len(buttons)))
            if buttons:
                n = len(rw.windows)
                buttons[0].invoke()
                if len(rw.windows) != n + 1:
                    errors.append((tag, "relations", "Row Detail Related opened no window"))
                else:
                    wait_window(rw.windows[-1])
            elif table == "message_poll":
                errors.append((tag, "relations", "Row Detail %s has no Related button"
                               % rwin.title()))
            rwin._on_close()
        for w in list(rw.windows):
            w.close()
        if rw.windows:
            errors.append((tag, "relations", "windows left open: %d" % len(rw.windows)))
    step(relations, "relations")

    def datamap_check():
        """Copy with related from the Browse menu (formats, 2 links, Copy, Export into a temp
        folder, refused in the evidence folder) and from Row Detail; Export Database Map… from
        the Relationships tab as HTML and JSON (temp folder only; refused in the evidence
        folder)."""
        dmu = app.datamap
        dmu.warn_with_dialogs = False
        out = tempfile.mkdtemp(prefix="sga_smoke_dbmap_")

        def filtered_check(table, col, out):
            """All filtered rows: the menu item with the count, the estimate, a copy made in
            the background (preview smaller than the rows), the clipboard limit, a streamed
            export of every filtered row."""
            value = grid.row_data(0)[0][col]
            expr = value_expr(value)
            if not expr:
                return
            grid.set_filter_text(col, expr)
            settle()
            n = app._browse_source.row_count()
            grid.set_current_cell(0, col)
            menu = grid.build_context_menu(0, col)
            label = "Copy with related: all %s filtered rows" % format(n, ",")
            if not invoke(menu, label):
                errors.append((tag, "datamap", "no %r in the menu" % label))
                grid.set_filter_text(col, "")
                settle()
                return
            w = dmu.windows[-1]
            wait_w(w)
            est = w.estimate.cget("text")
            w.limits["related_preview_rows"] = 1        # the copy is made in a second pass
            w.rebuild()
            wait_w(w)
            first = w.copy()
            wait_w(w)
            # more rows than the preview: made in the background (copy() returned None)
            copied = (first is None) == (n > 1) and w.status.cget("text").startswith("Copied")
            w.limits["clipboard_chars"] = 1024
            w.copy()
            wait_w(w)
            refused = "Too large for the clipboard" in w.status.cget("text") and \
                "clipboard_chars" in w.status.cget("text")
            target = os.path.join(out, "filtered.json")
            w.fmt_var.set("json")
            w.export_to(target)
            wait_w(w)
            try:
                with open(target, encoding="utf-8") as f:
                    written = json.load(f)["rows_written"]
            except (OSError, ValueError, KeyError):
                written = -1
            note(tag, "all %s filtered rows of %s: estimate %r; copy %s, clipboard limit said %s, "
                 "export %d rows" % (n, table, est[:70], copied, refused, written))
            if format(n, ",") not in est or not copied or not refused or written != n:
                errors.append((tag, "datamap", "filtered rows: estimate %r, copy %s, limit %s, "
                               "export %d of %s" % (est[:60], copied, refused, written, n)))
            w.close()
            # a selection too large to read at once: its rows are read in windows while
            # exporting (as sorted and filtered when it was chosen)
            if n > 2:
                w = dmu.copy_range(table, app._browse_source, 1, n - 1)
                wait_w(w)
                target = os.path.join(out, "range.json")
                w.fmt_var.set("json")
                w.export_to(target)
                wait_w(w)
                try:
                    with open(target, encoding="utf-8") as f:
                        got = json.load(f)["rows_written"]
                except (OSError, ValueError, KeyError):
                    got = -1
                note(tag, "selected rows 2..%d: %d exported" % (n, got))
                if got != n - 1:
                    errors.append((tag, "datamap", "selected range: %d of %d" % (got, n - 1)))
                w.close()
            grid.set_filter_text(col, "")
            settle()

        def limits_check():
            """The Limits window refuses a bad value, saves a good one (settings in the temp
            app-data folder), and new windows use it."""
            lw = dmu.limits_window()
            lw.vars["related_rows_per_link"].set("0")
            bad = lw.save()
            lw.vars["related_rows_per_link"].set("7")
            good = lw.save()
            seen = dmu.limits()["related_rows_per_link"]
            lw.defaults()
            lw.save()
            back = dmu.limits()["related_rows_per_link"]
            lw.close()
            if bad or not good or seen != 7 or back != 20:
                errors.append((tag, "datamap", "limits: bad saved %s, good %s, read %r, reset %r"
                               % (bad, good, seen, back)))

        def wait_w(w, timeout=180):
            deadline = time.time() + timeout
            app.update()
            while w.busy() and time.time() < deadline:
                app.update()
                time.sleep(0.01)
            app.update()
            if w.busy():
                errors.append((tag, "datamap", "%s still working after %ds" % (w.title(), timeout)))
        try:
            cands = [t for t in app.db.tables() if app.relations.supported(t) and app.db.count(t)]
            if "message_poll" in cands:
                cands.remove("message_poll")
                cands.insert(0, "message_poll")
            cands.sort(key=lambda t: not dmu.has_links(t))     # a table with links first
            if cands:
                table = cands[0]
                app._nb.select(app._browse_frame)
                app._browse_table_var.set(table)
                app._load_browse_table()
                settle()
                grid.redraw_now()
                col = 1 if len(grid.columns()) > 1 else 0
                grid.set_current_cell(0, col)
                loc = grid.row_data(0)[0][0]
                menu = grid.build_context_menu(0, col)
                labels = [menu.entrycget(i, "label") for i in range((menu.index("end") or 0) + 1)
                          if menu.type(i) != "separator"]
                keyed = getattr(loc, "kind", None) in ("rowid", "pk") and dmu.has_links(table)
                if keyed != ("Copy with related" in labels):
                    errors.append((tag, "datamap", "row %r: Copy with related offered=%s"
                                   % (loc, "Copy with related" in labels)))
                note(tag, "Copy with related offered for %s: %s" % (table, keyed))
                if keyed and invoke(menu, "Copy with related"):
                    w = dmu.windows[-1]
                    wait_w(w)
                    md = w.text()
                    formats = {"markdown": md.startswith("# Copy with related")}
                    w.fmt_var.set("json")
                    w.render()
                    try:
                        formats["json"] = bool(json.loads(w.text())["rows"])
                    except ValueError:
                        formats["json"] = False
                    w.fmt_var.set("sql")
                    w.render()
                    formats["sql"] = "SELECT " in w.text()
                    w.hops_var.set(2)
                    w.rebuild()
                    wait_w(w)
                    two = w.bundle.rows if w.bundle is not None else -1
                    copied = w.copy() and app.clipboard_get() == w.text()
                    target = os.path.join(out, "related.sql")
                    started = w.export_to(target)
                    wait_w(w)
                    exported = started and os.path.getsize(target) > 0 and \
                        w.result is not None and w.result.complete
                    inside = os.path.join(tmp, "related.sql")
                    refused = not w.export_to(inside) and not os.path.exists(inside) \
                        and "evidence folder" in dmu.last_message
                    note(tag, "copy with related %s %r: %s; 2 links %d rows; copy %s, export %s, "
                         "refused %s; %r" % (table, loc, formats, two, copied, exported, refused,
                                             w.status.cget("text")[:80]))
                    if not all(formats.values()) or two < 1 or not (copied and exported
                                                                    and refused):
                        errors.append((tag, "datamap", "copy with related: %s, rows %d, copy %s, "
                                       "export %s, refused %s" % (formats, two, copied,
                                                                  exported, refused)))
                    w.close()
                    # Row Detail offers it too
                    rwin = RowWin.show(app, app.db, table, loc)
                    def descendants(w):
                        found = []
                        for c in w.winfo_children():
                            found.append(c)
                            found.extend(descendants(c))
                        return found
                    btns = [b for b in descendants(rwin) if b.winfo_class() == "Button"
                            and b.cget("text") == "Copy with related"]
                    if len(btns) != 1:
                        errors.append((tag, "datamap", "Row Detail has %d Copy with related "
                                                       "buttons" % len(btns)))
                    else:
                        n = len(dmu.windows)
                        btns[0].invoke()
                        if len(dmu.windows) != n + 1:
                            errors.append((tag, "datamap", "Row Detail button opened no window"))
                        else:
                            wait_w(dmu.windows[-1])
                            dmu.windows[-1].close()
                    rwin._on_close()
                    filtered_check(table, len(grid.columns()) - 1, out)
            limits_check()
            # Export ▾ › Database Map… from the Relationships tab
            em = app._relations_tab.export_menu
            em.invoke(next(i for i in range(em.index("end") + 1) if em.type(i) == "command"
                           and em.entrycget(i, "label") == "Database Map…"))
            mw = dmu.map_window()
            if mw is None:
                errors.append((tag, "datamap", "Export Database Map… opened no window"))
                return
            results = {}
            for fmt, samples in (("html", False), ("json", True)):
                mw.fmt_var.set(fmt)
                mw.samples_var.set(samples)
                target = os.path.join(out, "map." + fmt)
                t0 = time.time()
                if not mw.export_to(target):
                    errors.append((tag, "datamap", "the %s map was not started" % fmt))
                    continue
                wait_w(mw)
                ok = os.path.isfile(target) and mw.result_path == target
                size = os.path.getsize(target) if ok else 0
                results[fmt] = (ok, size, round(time.time() - t0, 1))
                if ok and fmt == "html":
                    with open(target, encoding="utf-8") as f:
                        text = f.read()
                    sha = app.db.evidence.fingerprints["main"].sha256
                    if "<svg" not in text or not sha or sha not in text:
                        errors.append((tag, "datamap", "HTML map lacks the diagram or the hash"))
                elif ok:
                    with open(target, encoding="utf-8") as f:
                        d = json.load(f)
                    if d["format"] != "sqlite-gui-analyzer-database-map" or \
                            (d["summary"]["tables"] and not d["samples"] and
                             any(t["rows"] for t in d["tables"])):
                        errors.append((tag, "datamap", "JSON map: tables %d, samples %d"
                                       % (d["summary"]["tables"], len(d["samples"]))))
                if not ok:
                    errors.append((tag, "datamap", "%s map not written: %r"
                                   % (fmt, mw.status.cget("text"))))
            inside = os.path.join(tmp, "map.html")
            refused = not mw.export_to(inside) and not os.path.exists(inside)
            note(tag, "database map: %s; refused in the evidence folder %s; %r"
                 % (results, refused, mw.status.cget("text")[:100]))
            if not refused:
                errors.append((tag, "datamap", "a map was written into the evidence folder"))
            mw.close()
            if dmu.windows:
                errors.append((tag, "datamap", "windows left open: %d" % len(dmu.windows)))
        finally:
            shutil.rmtree(out, ignore_errors=True)
    step(datamap_check, "datamap")

    def timeline_check():
        """Timeline tab: detection when shown, the column list, a build, a date range, opening
        and tagging an event, a local offset and the exports (never into the evidence folder)."""
        tab = app._timeline

        def wait(timeout=180):
            deadline = time.time() + timeout
            app.update()
            while tab.busy() and time.time() < deadline:
                app.update()
                time.sleep(0.02)
            app.update()
            if tab.busy():
                errors.append((tag, "timeline", "a job still runs after %ds" % timeout))
        app._nb.select(tab)
        wait()
        det = tab.detection
        if det is None:
            errors.append((tag, "timeline", "no detection after the tab was shown"))
            return
        found = det.detected()
        note(tag, "timeline detect: %s | %s" % (tab.status.cget("text"), ", ".join(
            "%s.%s=%s" % (c.table, c.column, c.kind) for c in found[:6])))
        if len(tab.col_tree.get_children()) != len(det.columns):
            errors.append((tag, "timeline", "%d columns listed of %d"
                           % (len(tab.col_tree.get_children()), len(det.columns))))
        tlfx = globals().get("tlfx")
        if tlfx is not None and os.path.basename(src_db) == "timeline.db":
            for key, kind in tlfx.EXPECTED.items():
                c = det.get(*key)
                if c is None or c.kind != kind:
                    errors.append((tag, "timeline", "%s.%s: %r, expected %s" % (
                        key[0], key[1], c.kind if c else None, kind)))
            for key in tlfx.TRAPS:
                c = det.get(*key)
                if c is not None and c.kind:
                    errors.append((tag, "timeline", "%s.%s taken for %s" % (key + (c.kind,))))
        if not found:
            return
        # leave a column out from its menu, and take it back
        iid = next(i for i in tab.col_tree.get_children() if tab._column(i) is found[0])
        m = tab.column_menu(iid)
        m.invoke(0)
        off = not found[0].enabled and tab.col_tree.set(iid, "on") == "☐"
        tab.toggle(iid)
        if not off or not found[0].enabled:
            errors.append((tag, "timeline", "leave out / include did not work"))
        m.destroy()
        fz = app._forensics                 # recovered records join the timeline
        if not fz._records:
            fz.carve_time.set("1 minute")
            fz.start_carve()
            deadline = time.time() + 120
            while fz._busy and time.time() < deadline:
                app.update()
                time.sleep(0.02)
            app.update()
        tab.start_build()
        wait()
        res = tab.result
        if res is None:
            errors.append((tag, "timeline", "no events built"))
            return
        evs = res.events
        g = tab.grid
        g.redraw_now()
        note(tag, "timeline build: %s" % tab.status.cget("text")[:160])
        if [e.when for e in evs] != sorted(e.when for e in evs) or g.row_count() != len(evs):
            errors.append((tag, "timeline", "%d events, grid %d rows, sorted %s" % (
                len(evs), g.row_count(), [e.when for e in evs] == sorted(e.when for e in evs))))
        if not evs:
            return
        # open the first database event and a WAL / recovered one, tag from the grid menu
        opened = len(app.winfo_children()) + len(RowWin._pool)
        for src in ("DB", "WAL", "Recovered"):
            ev = next((e for e in evs if e.source.startswith(src)), None)
            if ev is not None:
                tab.open_event(ev)
                app.update()
                note(tag, "  timeline opened a %s event (%s.%s row %s)" % (
                    src, ev.table, ev.column, ev.row))
        if len(app.winfo_children()) + len(RowWin._pool) <= opened:
            errors.append((tag, "timeline", "opening an event showed no window"))
        close_windows()
        g.set_current_cell(0, 1)
        menu = g.build_context_menu(0, 1)
        sub = submenu(menu, "Tag")
        if sub is None or not invoke(sub, "Review"):
            errors.append((tag, "timeline", "no Tag > Review in the event menu"))
        else:
            ev0 = tab.event_at_row(0)
            key = tab.entry_for(ev0).key
            if "Review" not in app.tags.store.tags_of(key):
                errors.append((tag, "timeline", "the event's row was not tagged"))
            app.tags.store.remove([key])
        # a date range: only events inside it, read again
        mid = evs[len(evs) // 2].when
        tab.from_var.set(mid.strftime("%Y-%m-%d"))
        tab.to_var.set(mid.strftime("%Y-%m-%d"))
        tab.start_build()
        wait()
        lo, hi = datetime.datetime(mid.year, mid.month, mid.day), \
            datetime.datetime(mid.year, mid.month, mid.day, 23, 59, 59, 999999)
        ranged = tab.result.events
        want = [e for e in evs if lo <= e.when <= hi]
        note(tag, "  timeline %s: %d events (%d of the full build)" % (
            lo.date(), len(ranged), len(want)))
        if not ranged or any(not lo <= e.when <= hi for e in ranged) or \
                len(ranged) < len(want):
            errors.append((tag, "timeline", "range %s: %d events, %d expected" % (
                lo.date(), len(ranged), len(want))))
        tab.from_var.set("")
        tab.to_var.set("")
        # a local offset adds a column; exports; the evidence folder is refused
        tab.offset_var.set("UTC+05:30")
        tab._offset_changed()
        if "Time (UTC+05:30)" not in g.columns():
            errors.append((tag, "timeline", "no local time column: %s" % g.columns()))
        out = tempfile.mkdtemp(prefix="sga_smoke_timeline_")
        try:
            for fmt in ("csv", "json", "html"):
                n = tab.export(fmt, os.path.join(out, "timeline." + fmt))
                if n != len(tab.shown_events()) or not os.path.getsize(
                        os.path.join(out, "timeline." + fmt)):
                    errors.append((tag, "timeline", "%s export wrote %d of %d" % (
                        fmt, n, len(tab.shown_events()))))
            refused = False
            try:
                tab.export("csv", os.path.join(tmp, "timeline.csv"))
            except ValueError:
                refused = True
            if not refused or os.path.exists(os.path.join(tmp, "timeline.csv")):
                errors.append((tag, "timeline", "an export was written into the evidence folder"))
        finally:
            shutil.rmtree(out, ignore_errors=True)
        tab.offset_var.set("UTC")
        tab._offset_changed()
    step(timeline_check, "timeline")

    def date_format_check():
        """Browse 'Show as date' on a detected date column: the grid shows dates, the raw value
        stays in the tooltip and 'Copy raw value', and the choice comes back after reopening."""
        det = app._timeline.detection
        col = next((c for c in (det.detected() if det else []) if c.kind != tl.ISO
                    and app.db.count(c.table)), None)
        if col is None:
            return
        app._nb.select(app._browse_frame)
        app._browse_table_var.set(col.table)
        app._load_browse_table()
        settle()
        c = grid.columns().index(col.column)
        menu = grid.build_header_menu(c)
        sub = submenu(menu, "Show as date")
        if sub is None:
            errors.append((tag, "date format", "no 'Show as date' in the header menu"))
            return
        auto = sub.entrycget(0, "label")
        sub.invoke(0)                       # Auto (a sample is read on a worker, then shown)
        deadline = time.time() + 30
        while grid.column_formatter(c) is None and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        grid.redraw_now()
        row = next((r for r in range(grid.row_count()) if grid.row_data(r) is not None
                    and grid.row_data(r)[0][c] not in (None, 0)), None)
        fmt = grid.column_formatter(c)
        if fmt is None or row is None:
            errors.append((tag, "date format", "Auto set no formatter (%r)" % auto))
            return
        raw = grid.row_data(row)[0][c]
        shown = grid.display_text(raw, c)
        grid.set_current_cell(row, c)
        tip = grid.show_cell_tip(row, c, -5000, -5000)
        grid._tip_hide()
        note(tag, "date format %s.%s: %r -> %r (%s)" % (col.table, col.column, raw, shown,
                                                       auto))
        if shown != tl.formatter(col.kind)(raw) or grid.cell_copy_text(raw=True) != \
                plain_text(raw) or not tip or "raw: " not in tip:
            errors.append((tag, "date format", "shown %r, copy raw %r, tip %r" % (
                shown, grid.cell_copy_text(raw=True), tip)))
        app._close_db(confirm=False)
        app._open_db(dst, wait=True)
        settle()
        again = grid.column_formatter(grid.columns().index(col.column)) \
            if app._browse_table_var.get() == col.table else None
        if again is None:
            errors.append((tag, "date format", "the date format did not come back"))
        app._browse_dates.set(col.table, col.column, tl.OFF)
        if grid.column_formatter(grid.columns().index(col.column)) is not None:
            errors.append((tag, "date format", "Off left the formatter"))
    step(date_format_check, "date format")

    def sql_grid():
        # Query results show in the same grid; its column filters work on the returned rows.
        if app.db.sql_conn() is None:
            return
        first = next((t for t in app.db.tables() if app.db.count(t)), None)
        if first is None:
            return
        app._nb.select(app._sql_frame)
        app._sql_editor.delete("1.0", "end")
        app._sql_editor.insert("1.0", 'SELECT * FROM "%s" LIMIT 100' % first.replace('"', '""'))
        app._sql_run()
        deadline = time.time() + 30
        while app._sql_query_thread.is_alive() and time.time() < deadline:
            app.update()
            time.sleep(0.01)
        app.update()
        g = app._sql_grid
        g.redraw_now()
        first_row, end = g.visible_row_range()
        rows = g.row_count()
        note(tag, "SQL grid: %d rows, %d drawn, %d columns; status %r" % (
            rows, end - first_row, len(g.columns()), app._sql_status_label.cget("text")[:60]))
        if not rows or end <= first_row:
            errors.append((tag, "SQL grid", "%d rows, %d drawn" % (rows, end - first_row)))
            return
        g.set_filter_text(0, "NOT NULL")
        app.update()
        kept = g.row_count()
        status = app._sql_status_label.cget("text")
        g.set_filter_text(0, "")
        app.update()
        if kept > rows or g.row_count() != rows or "kept by the column filters" not in status:
            errors.append((tag, "SQL grid", "filter NOT NULL kept %d of %d; status %r"
                           % (kept, rows, status)))
    step(sql_grid, "sql grid")

    def sql_cancel():
        # An endless query only stops if Cancel interrupts the running statement.
        if app.db.sql_conn() is None:
            return
        app._sql_editor.delete("1.0", "end")
        app._sql_editor.insert("1.0", "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL "
                                      "SELECT x + 1 FROM c) SELECT count(*) FROM c")
        app._sql_run()
        time.sleep(0.3)
        t0 = time.time()
        app._sql_cancel()
        app._sql_query_thread.join(10)
        stopped = not app._sql_query_thread.is_alive()
        app.update()
        note(tag, "SQL cancel: stopped=%s in %.1fs status=%r" % (
            stopped, time.time() - t0, app._sql_status_label.cget("text")))
        if not stopped:
            errors.append((tag, "SQL cancel", "the query was still running 10 s after Cancel"))
    step(sql_cancel, "sql cancel")

    def issues():
        # Every engine Issue must be listed in the Issues dialog, and counted on its button.
        app.update()
        # (the database's log and, once they ran, the Forensics scans' logs)
        def logged():
            return sum(len(log) for _l, log in app._issue_logs())
        before = logged()                        # the logs only grow (worker threads add too)
        app._show_issues()
        rows = sum(int(app._issues_tree.item(k, "values")[4])        # 'Times' column
                   for k in app._issues_tree.get_children())
        title = app._issues_win.title()
        app._refresh_issue_btn()
        n = logged()
        distinct = len(app._issues_tree.get_children())     # one line per distinct issue
        btn = app._issues_btn.cget("text")
        kinds = sorted(set(app._issues_tree.item(k, "values")[1]
                           for k in app._issues_tree.get_children()))
        note(tag, "Issues button=%r dialog lines=%d (met %d times) kinds=%s"
             % (btn, distinct, rows, kinds[:8]))
        if (not before <= rows <= n or title != "Issues (%d)" % distinct
                or (distinct and btn != "Issues (%d)" % app._issue_count())
                or distinct != app._issue_count()):
            errors.append((tag, "issues", "%d-%d issues, dialog %r with %d rows, button %r"
                           % (before, n, title, rows, btn)))
        app._issues_win.destroy()
    step(issues, "issues dialog")

    def chips():
        # The header's evidence chip says how the database was opened; the warnings chip
        # counts the engine's warnings (SQL failures found mid-session included) and the
        # status window lists every banner.
        from constants import mode_label
        app._refresh_status()
        ev, warn = app.status_chips()
        warns = [b for b in app.db.banners() if b.level in ("warning", "error")]
        detail = app.status_detail_text()
        if mode_label(app.db.mode) not in ev or (warns and str(len(warns)) not in warn) or \
                (not warns and warn) or any(b.short not in detail for b in app.db.banners()):
            errors.append((tag, "chips", "evidence %r, warnings %r, engine says %r" % (
                ev, warn, [b.short for b in app.db.banners()])))
        note(tag, "chips after browsing=%r %r" % (ev, warn))
    step(chips, "status chips")

    def wal():
        if not app._wal_tab_added:
            return
        app._nb.select(app._wal_frame)
        wt = app._wal_frame
        if wt.problem.winfo_manager():
            note(tag, "WAL tab: %s" % wt.problem.cget("text")[:150])
            return
        wt._toggle_stats()
        kids = wt.tree.get_children()
        for k in kids[:5]:
            wt.tree.selection_set(k)
            wt._on_frame()
        note(tag, "WAL frames shown=%d first=%s summary=%s" % (len(kids),
             wt.tree.item(kids[0], "values") if kids else "-", wt.summary.cget("text")[:120]))
        # records of every table: one 'Values' column, never another table's headers
        wt.view_var.set("records")
        wt._switch_view()
        wt.load_records()
        deadline = time.time() + 60
        while wt._runner.busy() and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        cols = wt.rec_grid.columns()
        tables = set(r[0]["table"] for r in wt._records)
        note(tag, "WAL records compared: %d of %d table(s); columns %s; %s" % (
            len(wt._records), len(tables), cols[:9], wt.rec_status.cget("text")[:140]))
        if len(tables) > 1 and "Values" not in cols:
            errors.append((tag, "wal records", "several tables under one table's headers: %s"
                           % cols))
        if any(st == "error" for _r, st, _d, _w in wt._records):
            note(tag, "  WAL records that could not be compared: %d" % sum(
                1 for _r, st, _d, _w in wt._records if st == "error"))
        wt.view_var.set("frames")
        wt._switch_view()
    step(wal, "wal tab")

    def freed():
        tab = app._forensics
        n = app.db.freelist_count()
        shown = str(tab._freed_page) in tab.nb.tabs() and tab.nb.tab(tab._freed_page,
                                                                       "state") != "hidden"
        if not n:
            if shown:
                errors.append((tag, "freed pages", "the Freed Pages page shows without freed "
                                                   "pages"))
            return
        if not shown:
            errors.append((tag, "freed pages", "%d freed pages but no Freed Pages page" % n))
            return
        tab.start_freed()
        deadline = time.time() + 60
        while tab._busy and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        kids = tab.freed_tree.get_children()
        confs = set(r.confidence for r in tab._freed)
        note(tag, "Freed pages: %d listed, %d records, confidences %s; %s" % (
            len(kids), len(tab._freed), sorted(confs), tab.freed_status.cget("text")[:120]))
        if not confs <= set(("high", "medium", "low")):
            errors.append((tag, "freed pages", "confidence outside the engine's scale: %s"
                           % confs))
        with_recs = [k for k in kids if int(tab.freed_tree.item(k, "values")[2] or 0)]
        if with_recs:
            tab.freed_tree.selection_set(with_recs[0])
            tab._on_freed_page()
            app.update()
            if tab.freed_grid.row_count() != int(tab.freed_tree.item(with_recs[0],
                                                                       "values")[2]):
                errors.append((tag, "freed pages", "the page's records are not all listed"))
            if not tab.freed_hex.data:
                errors.append((tag, "freed pages", "no page bytes shown"))
    step(freed, "freed pages (forensics)")

    def evidence():
        app._show_evidence()
        app.update()
        for w in app.winfo_children():
            if w.winfo_class() == "Toplevel" and not getattr(w, "_closing", False):
                w.destroy()
    step(evidence, "evidence panel")

    def close():
        # Close while an endless SQL-tab query runs: _close_db must stop the worker before the
        # session closes, and no connection may be closed under it (a crash on Python <= 3.10).
        busy = app.db.sql_conn() is not None
        if busy:
            app._sql_editor.delete("1.0", "end")
            app._sql_editor.insert("1.0", "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL "
                                          "SELECT x + 1 FROM c) SELECT count(*) FROM c")
            app._sql_run()
            time.sleep(0.3)
        t0 = time.time()
        app._close_db(confirm=False)
        took = time.time() - t0
        th = app._sql_query_thread
        running = th is not None and th.is_alive()
        note(tag, "close%s: %.2fs, SQL worker still running=%s"
             % (" during a query" if busy else "", took, running))
        if running or took > 5:
            errors.append((tag, "close", "took %.1fs, SQL worker still running=%s"
                           % (took, running)))
    step(close, "close")

    def verify():
        after = snapshot(tmp)
        if after != before:
            errors.append((tag, "EVIDENCE CHANGED", "%s -> %s" % (sorted(before), sorted(after))))
        shutil.rmtree(tmp, ignore_errors=True)
    step(verify, "verify evidence")
    return steps


def run_case(app):
    """A case of three databases (messages, contacts, settings; temp copies): the case bar,
    the schema per database, a search across them with its per-database summary (and
    'searched, nothing found'), the Databases: choice and the grouped scope, rows opened in
    their own database, the links between databases (matched by value) in the Relationships
    tab and the Related menu, 'Show value from linked table', the merged timeline, tags of
    two databases in the Tagged tab, the saved case reopened, a database removed, and the
    evidence folders untouched."""
    import tkinter.messagebox as mb
    sys.path.insert(0, ROOT)
    from tests.fixtures import case_fixtures as cf
    from case_ui import OpenFolderDialog
    from dialogs import ScopeDlg
    tag = "case"
    steps = []
    tmp = tempfile.mkdtemp(prefix="ui_case_")
    folders = [os.path.join(tmp, n) for n in ("phone_a", "phone_b")]
    for f in folders:
        os.makedirs(f)
    paths = [cf.messages(folders[0]), cf.contacts(folders[1]), cf.settings(folders[1])]
    with open(os.path.join(folders[1], "notes.txt"), "w") as f:
        f.write("not a database")
    before = [snapshot(f) for f in folders]
    st = {}

    def step(fn, label):
        steps.append((fn, "case " + label))

    def pump(cond, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            app.update()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def labels(menu):
        end = menu.index("end")
        return [] if end is None else [menu.entrycget(i, "label") for i in range(end + 1)
                                       if menu.type(i) != "separator"]

    def open_case():
        # Open Folder lists only SQLite files (the text file is left out), all ticked
        dlg = OpenFolderDialog(app, folders[1])
        dlg.wait()
        listed = [c.name for c in dlg.candidates]
        if sorted(listed) != ["contacts.db", "settings.db"] or len(dlg.ticked) != 2:
            errors.append((tag, "open folder", "listed %s, ticked %d" % (listed, len(dlg.ticked))))
        dlg.filter_var.set("contacts")
        dlg.set_all(False)
        if len(dlg.ticked) != 1:
            errors.append((tag, "open folder", "select none of the filtered left %d ticked"
                           % len(dlg.ticked)))
        dlg.close()
        app._open_db(paths[0], wait=True)
        if app._overview_added:
            errors.append((tag, "overview", "shown with one database"))
        # they open on a worker thread and join the case as each is open
        app._add_databases(paths[1:] + [paths[1]])       # a duplicate is skipped
        if not pump(lambda: not app.opening()):
            errors.append((tag, "case", "still opening after 60 s"))
        app.update()
        names = [m.name for m in app.case]
        lines = app._navigator.db_lines()
        groups = app._navigator.group_lines()
        note(tag, "case: %s; navigator %s, groups %s" % (names, lines, groups))
        if names != ["messages.db", "contacts.db", "settings.db"] or len(lines) != 3 or \
                len(groups) != 2 or not app._overview_added:
            errors.append((tag, "case", "databases %s, navigator %s, groups %s"
                           % (names, lines, groups)))
        # plain tab names; the breadcrumb says which database a tab shows
        crumb = app._browse_crumb.text()
        if app._nb.tab(app._browse_frame, "text") != "Browse" or \
                not crumb.startswith("messages.db"):
            errors.append((tag, "tab titles", "%r, breadcrumb %r" % (
                app._nb.tab(app._browse_frame, "text"), crumb)))
        rows = app._overview.rows()
        if len(rows) != 3:
            errors.append((tag, "overview", "rows %s" % rows))
        note(tag, "overview: %s" % [r[:6] for r in rows])
    step(open_case, "open")

    def links():
        if not pump(lambda: app.relations.state[0] in ("done", "stopped"), 120):
            errors.append((tag, "links", "mapping still running"))
            return
        cross = app.relations.cross_links()
        app._overview.update_cards()
        note(tag, "links between databases: %s; overview card %r" % (
            [(l.src_table, l.src_col, l.dst_table, l.dst_col, round(l.fraction, 2))
             for l in cross], app._overview.cards["links"][1].cget("text")))
        pairs = set((l.src_table, l.dst_table) for l in cross)
        if ("message", "wa_contacts") not in pairs or ("jid", "wa_contacts") not in pairs:
            errors.append((tag, "links", "expected message/jid -> wa_contacts, got %s" % pairs))
        tab = app._relations_tab
        tab.reload()
        tab.cross_only_var.set(True)
        tab.refresh()
        rows = tab.list_rows()
        if not rows or any(r[3] != "matched by value" for r in rows) or \
                len(tab.canvas.find_withtag("cross")) != len(tab.graph.shown()):
            errors.append((tag, "relations tab", "only-across rows %s" % rows[:3]))
        note(tag, "relationships: %s | %d rows across" % (tab.status.cget("text")[:150], len(rows)))
        tab.cross_only_var.set(False)
        tab.refresh()
        m = tk.Menu(app, tearoff=0)
        app.relations.value_menu(m, "jid", "raw_string", cf.JIDS[0], member=app.case.members[0])
        sub = None
        for i in range((m.index("end") or 0) + 1):
            if m.type(i) == "cascade":
                sub = labels(m.nametowidget(m.entrycget(i, "menu")))
        if not sub or "In other databases (matched by value)" not in sub or \
                not any(s.startswith("contacts.db › wa_contacts") for s in sub):
            errors.append((tag, "related menu", "Related rows %s" % sub))
        note(tag, "related menu: %s" % sub)
        m.destroy()
    step(links, "links")

    def datamap_case():
        """Copy with related follows the links between databases (matched by value, each row
        naming its database); Export Database Map… offers the active database or the whole
        case (every database, the links between them, one diagram); nothing is written into a
        case folder."""
        from engine.schema import Locator
        dmu = app.datamap
        dmu.warn_with_dialogs = False
        out = tempfile.mkdtemp(prefix="sga_smoke_case_map_")
        try:
            member = app.case.members[0]
            app.activate_member(member)
            loc = [Locator("rowid", 3)]
            if not dmu.offers("message", loc, member):
                errors.append((tag, "copy with related", "not offered for messages.db message"))
                return
            w = dmu.copy_with_related("message", loc, member)
            pump(lambda: not w.busy())
            md = w.text()
            w.fmt_var.set("sql")
            w.render()
            sql = w.text()
            across = "## messages.db › message row 3" in md and \
                "contacts.db › wa_contacts" in md and "matched by value" in md
            note(tag, "copy with related across databases: %s; sql per database %s; %r"
                 % (across, "-- in contacts.db" in sql, w.status.cget("text")[:90]))
            if not across or "-- in contacts.db" not in sql:
                errors.append((tag, "copy with related", "across %s, sql %s"
                               % (across, "-- in contacts.db" in sql)))
            w.close()
            # a row opened from another database: offered for that database
            other = app.case.members[1]
            rwin = RowWin.show(app, other.db, "wa_contacts", Locator("rowid", 4))

            def descendants(w):
                found = []
                for c in w.winfo_children():
                    found.append(c)
                    found.extend(descendants(c))
                return found
            btns = [b for b in descendants(rwin) if b.winfo_class() == "Button"
                    and b.cget("text") == "Copy with related"]
            opened = None
            if btns:
                n = len(dmu.windows)
                btns[0].invoke()
                if len(dmu.windows) == n + 1:
                    opened = dmu.windows[-1]
                    pump(lambda: not opened.busy())
            rwin._on_close()
            if opened is None or opened.member is not other or \
                    "## contacts.db › wa_contacts row 4" not in opened.text():
                errors.append((tag, "copy with related", "Row Detail of contacts.db: %s"
                               % (opened.text()[:120] if opened is not None else btns)))
            if opened is not None:
                opened.close()
            mw = dmu.export_map()
            if mw is None or not mw.multi:
                errors.append((tag, "database map", "no case choice"))
                return
            results = {}
            for scope, fmt in (("case", "html"), ("case", "json"), ("active", "markdown")):
                mw.scope_var.set(scope)
                mw.fmt_var.set(fmt)
                target = os.path.join(out, "%s.%s" % (scope, fmt))
                if not mw.export_to(target):
                    errors.append((tag, "database map", "%s %s not started" % (scope, fmt)))
                    continue
                pump(lambda: not mw.busy(), 180)
                with open(target, encoding="utf-8") as f:
                    text = f.read()
                results[(scope, fmt)] = len(text)
                if scope == "case" and fmt == "html":
                    # the shared report design: a section per database, the links between
                    # them, and each table under its database
                    ok = all(n in text for n in ("messages.db", "contacts.db",
                                                 "settings.db")) and \
                        "Links between databases" in text and "messages.db › message" in text
                elif scope == "case":
                    d = json.loads(text)
                    ok = d.get("case") and len(d["database_maps"]) == 3 and \
                        d["summary"]["cross_links"] >= 2
                else:
                    ok = text.startswith("# messages.db - Database Map")
                if not ok:
                    errors.append((tag, "database map", "%s %s content" % (scope, fmt)))
            inside = os.path.join(folders[1], "map.html")
            refused = not mw.export_to(inside) and not os.path.exists(inside)
            note(tag, "database map: %s; refused in a case folder %s" % (results, refused))
            if not refused:
                errors.append((tag, "database map", "written into a case folder"))
            mw.close()
        finally:
            shutil.rmtree(out, ignore_errors=True)
    step(datamap_case, "copy with related and database map")

    def search():
        app._search_var.set("zebracorn")
        app._do_search()
        pump(lambda: not app._search_thread.is_alive())
        app.update()
        status = app._search_status.cget("text")
        srcs = [app._search_tree.item(i, "values")[1] for i in app._search_tree.get_children()]
        note(tag, "search: %s | sources %s" % (status, srcs))
        if "searched, nothing found (settings.db)" not in status or \
                sorted(srcs) != ["contacts.db · DB", "messages.db · DB"]:
            errors.append((tag, "search", "%s | %s" % (status, srcs)))
        # open the contacts row: it opens in contacts.db, whatever database is active
        iid = next(i for i in app._search_tree.get_children()
                   if app._search_tree.item(i, "values")[1].startswith("contacts.db"))
        app._search_tree.selection_set(iid)
        app._on_search_dblclick(None)
        app.update()
        win = [w for w in RowWin._pool.values() if w.winfo_exists()]
        titles = [w.title() for w in win]
        if not any(t.startswith("Row detail — wa_contacts") and "(contacts.db)" in t
                   for t in titles):
            errors.append((tag, "row window", "titles %s" % titles))
        for w in win:
            w._on_close()
        # search only two databases: said before and after
        pick = app._search_db_picker
        pick.set_selection([app.case.members[0].uid, app.case.members[2].uid])
        app._search_dbs_changed()
        before_text = app._search_status.cget("text")
        if "2 of 3 databases" not in before_text or \
                "messages.db, settings.db" not in before_text:
            errors.append((tag, "databases", "scope text %r" % before_text))
        app._do_search()
        pump(lambda: not app._search_thread.is_alive())
        app.update()
        status = app._search_status.cget("text")
        if "contacts.db" in status or "messages.db: 1 row" not in status:
            errors.append((tag, "databases", "two-database search said %r" % status))
        pick.set_selection(None)
        app._search_dbs_changed()
        # the scope dialog groups tables under their database
        dlg = ScopeDlg.for_case(app, app._search_members())
        tops = dlg.group_titles()
        dlg.apply()
        if len(tops) != 3 or not tops[1].startswith("contacts.db"):
            errors.append((tag, "scope", "database nodes %s" % tops))
        note(tag, "scope dialog: %s" % tops)
    step(search, "search")

    def lookup():
        app.activate_member(app.case.members[0])
        app._nb.select(app._browse_frame)
        app._browse_table_var.set("jid")
        app._load_browse_table()
        grid = app._browse_grid
        c = grid.columns().index("raw_string")
        # offered in the header menu of the linked column only
        menu = grid.build_header_menu(c)
        offered = "Show value from linked table…" in labels(menu)
        other = grid.build_header_menu(grid.columns().index("_id"))
        if not offered or "Show value from linked table…" in labels(other):
            errors.append((tag, "lookup menu", "raw_string %s, _id %s" % (labels(menu),
                                                                          labels(other))))
        other.destroy()
        lk = app._browse_lookups
        targets = lk.targets("jid", "raw_string")
        cols, default = lk.columns_of(targets[0]) if targets else ([], None)
        if not targets or default != "display_name":
            errors.append((tag, "lookup", "targets %s default %s" % (
                [t.label(True) for t in targets], default)))
            return
        lk.show("jid", "raw_string", targets[0], default)
        pump(lambda: lk.active["raw_string"].map is not None)
        pump(lambda: (grid.redraw_now(), not grid.loading() and grid.row_data(0) is not None)[1])
        raw = grid.row_data(0)[0][c]
        shown = grid.display_text(raw, c)
        note(tag, "lookup: %r -> %r" % (raw, shown))
        if not shown.startswith(raw) or "→ Person 0" not in shown or \
                "contacts.db › wa_contacts.display_name" not in shown:
            errors.append((tag, "lookup", "shown %r" % shown))
        names = [n for _i, n, _l in lk.export_columns(grid.columns())]
        if names != ["raw_string → contacts.db › wa_contacts.display_name"]:
            errors.append((tag, "lookup export", names))
        menu.destroy()
    step(lookup, "lookup")

    def timeline():
        tab = app._timeline
        app._nb.select(tab)
        pump(lambda: tab.detection is not None and not tab.busy())
        tab.start_build()
        pump(lambda: not tab.busy())
        cols = tab.grid.columns()
        dbs = sorted(set(e.database for e in tab.result.events)) if tab.result else []
        note(tag, "timeline: %s | columns %s" % (tab.status.cget("text")[:200], cols[:4]))
        if "Database" not in cols or dbs != ["contacts.db", "messages.db"] or \
                "no dated rows in the ticked columns (settings.db)" not in \
                tab.status.cget("text"):
            errors.append((tag, "timeline", "columns %s, databases %s" % (cols, dbs)))
    step(timeline, "timeline")

    def tags():
        app._search_var.set("zebracorn")
        app._do_search()
        pump(lambda: not app._search_thread.is_alive())
        app.update()
        app.tags.tag_search_results("Review", confirm=False)
        app.update()
        tt = app._tags_tab
        tt.refresh()
        rows = [tt.grid.row_data(r)[0] for r in range(tt.grid.row_count())
                if tt.grid.row_data(r) is not None] if tt.grid.row_count() else []
        per = [len(s) for s in app.tags.stores.values()]
        note(tag, "tags: per database %s, Tagged columns %s" % (per, tt.columns()[:2]))
        if per[:2] != [1, 1] or tt.columns()[0][1] != "Database":
            errors.append((tag, "tags", "per database %s, columns %s" % (per, tt.columns())))
        app.tags.save_now()
        st["tags"] = per
    step(tags, "tags")

    def reopen():
        cp = app.tags.settings["recent_cases"][0]["path"]
        app._close_db(confirm=False)
        asked = []
        real = mb.askyesno
        mb.askyesno = lambda *a, **k: asked.append(a) or True
        try:
            app.reopen_case(cp)
            if not pump(lambda: not app.opening()):
                errors.append((tag, "reopen", "still opening after 60 s"))
        finally:
            mb.askyesno = real
        app.update()
        names = [m.name for m in app.case]
        per = [len(s) for s in app.tags.stores.values()]
        note(tag, "reopened %s, tags %s, asked %d" % (names, per, len(asked)))
        if len(names) != 3 or per[:2] != st.get("tags", [])[:2] or asked:
            errors.append((tag, "reopen", "%s, tags %s, asked %s" % (names, per, asked)))
    step(reopen, "reopen")

    def remove():
        app.remove_member(app.case.members[2])
        app.update()
        app.remove_member(app.case.members[1])
        app.update()
        if len(app.case) != 1 or app._overview_added or \
                app._nb.tab(app._browse_frame, "text") != "Browse":
            errors.append((tag, "remove", "left %s" % [m.name for m in app.case]))
        app._close_db(confirm=False)
        after = [snapshot(f) for f in folders]
        if after != before:
            errors.append((tag, "EVIDENCE CHANGED", "case folders"))
        shutil.rmtree(tmp, ignore_errors=True)
    step(remove, "remove and close")
    return steps


OFF_SCREEN = "+-4000+0"


def keep_off_screen():
    """Every window of the run stays off the screen: the main window, each Toplevel (placed
    at creation, and again whatever position the app asks for later), and message boxes are
    recorded instead of shown (askyesno answers yes, as a user going ahead would)."""
    import re
    import tkinter.messagebox as mb
    real_geometry = tk.Wm.wm_geometry

    def geometry(self, newGeometry=None):
        if newGeometry is None:
            return real_geometry(self)
        size = re.match(r"^\d+x\d+", str(newGeometry))
        return real_geometry(self, (size.group(0) if size else "") + OFF_SCREEN)
    tk.Wm.wm_geometry = tk.Wm.geometry = geometry
    real_init = tk.Toplevel.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        real_geometry(self, OFF_SCREEN)
    tk.Toplevel.__init__ = init
    # the main window too: placed off the screen as it is created, and never maximised
    real_tk_init, real_state = tk.Tk.__init__, tk.Wm.wm_state

    def tk_init(self, *a, **k):
        real_tk_init(self, *a, **k)
        real_geometry(self, OFF_SCREEN)

    def state(self, newstate=None):
        return None if newstate == "zoomed" else real_state(self, newstate)
    tk.Tk.__init__ = tk_init
    tk.Wm.wm_state = tk.Wm.state = state

    def shown(kind):
        def box(title=None, message=None, **k):
            note("dialog", "%s: %s | %s" % (kind, title, str(message)[:160].replace("\n", " ")))
            if kind == "askquestion":
                return "yes"
            return True if kind.startswith("ask") else "ok"
        return box
    for kind in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel",
                 "askyesnocancel", "askquestion", "askretrycancel"):
        setattr(mb, kind, shown(kind))


def main():
    keep_off_screen()
    app = App()
    app.geometry("1500x900" + OFF_SCREEN)
    app.tags.warn_with_dialogs = False      # tag warnings are collected, not shown as dialogs

    def on_tk_error(exc, val, tb):
        errors.append(("tk-callback", current[0], "".join(traceback.format_exception(exc, val, tb))[-900:]))
    app.report_callback_exception = on_tk_error
    current = ["start"]
    all_steps = []
    for i, db in enumerate(DBS):
        all_steps.extend(run_one(app, db, i))
    if not sys.argv[1:]:
        all_steps.extend(run_case(app))     # several databases as one case

    # the watchdog: a tick every 50 ms measures how long the event loop went without
    # answering (the longest gaps, with the step running then)
    gaps = []
    last = [time.perf_counter()]

    def tick():
        now = time.perf_counter()
        gaps.append(((now - last[0]) * 1000.0, current[0]))
        last[0] = now
        try:
            app.after(50, tick)
        except tk.TclError:
            pass
    app.after(50, tick)
    # while the loop is stalled, a watcher thread notes where the Tk thread is (so a stall
    # names the code that held it, not only the step)
    main_id = threading.get_ident()
    stalls = []
    watching = [True]

    def watcher():
        seen = None
        while watching[0]:
            time.sleep(0.25)
            if time.perf_counter() - last[0] > 1.0 and seen != last[0]:
                seen = last[0]
                frame = sys._current_frames().get(main_id)
                if frame is not None:
                    stack = traceback.format_stack(frame)[-6:]
                    stalls.append((current[0], "".join(stack)))
    threading.Thread(target=watcher, name="stall-watcher", daemon=True).start()

    def pump(k=0):
        if k >= len(all_steps):
            app.after(200, app.destroy)
            return
        fn, label = all_steps[k]
        current[0] = label
        try:
            fn()
        except Exception:
            errors.append(("step", label, traceback.format_exc()[-900:]))
        app.after(150, lambda: pump(k + 1))

    app.after(800, pump)
    app.mainloop()
    watching[0] = False
    for lbl, stack in stalls[:6]:
        note("watchdog", "stalled in %s at:\n%s" % (lbl, stack))
    from engine import limits as _limits
    worst = sorted(gaps, reverse=True)[:5]
    most = _limits.get("ui_stall_ms")
    note("watchdog", "longest event-loop gaps: %s (limit ui_stall_ms %d ms)" % (
        ", ".join("%.0f ms in %s" % (g, lbl) for g, lbl in worst), most))
    for g, lbl in worst:
        if g > most:
            errors.append(("watchdog", lbl, "the event loop stalled %.0f ms (limit "
                                             "ui_stall_ms %d ms)" % (g, most)))
    # tag files and settings were written to the temp app-data folder only
    cases = os.path.join(DATA_DIR, "cases")
    written = sorted(os.listdir(cases)) if os.path.isdir(cases) else []
    note("app-data", "%d tag file(s), settings: %s, warnings: %d" % (
        len(written), os.path.isfile(os.path.join(DATA_DIR, "settings.json")),
        len(app.tags.messages)))
    for text in app.tags.messages:
        note("app-data", "  " + text[:160])
    if not written:
        errors.append(("app-data", "tags", "no tag file was saved in %s" % DATA_DIR))
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    print("\n".join(report))
    print("steps run:", len(all_steps), "errors:", len(errors))
    for e in errors:
        print("---", e[0], "|", e[1])
        print(e[2])
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
