"""Main application window for SQLite GUI Analyzer."""

import sqlite3
import threading
import os
import csv
import json
import re
import sys
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont

from constants import C, VERSION, SEARCH_MODES, _EXT_MAP
from utils import (_q, _le, fmtb, vb, _snippet, fmt_count, _int_count,
                   blob_type, try_decode_timestamp, _build_schema_text,
                   _build_schema_html)
from database import DB
from widgets import ToolTip, TreeviewTooltip, setup_theme
from dialogs import HelpDialog, ScopeDlg, BlobViewer, RowWin


# ── Combobox type-ahead helper ────────────────────────────────────────────
# ── Main Application ─────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self, initial_path=None):
        super().__init__()
        self.title("SQLite GUI Analyzer v" + VERSION)
        self.geometry("1200x750")
        self.configure(bg=C["bg"])
        self.minsize(900, 500)
        # Start maximized
        try:
            self.state('zoomed')  # Windows/macOS
        except tk.TclError:
            try:
                self.attributes('-zoomed', True)  # Linux
            except tk.TclError:
                pass

        setup_theme(self)
        self._set_app_icon()
        self.db = DB()
        self._count_cache = {}
        self._search_cancel = False
        self._search_thread = None
        self._count_cancel = False
        self._scope_tables = []
        self._browse_cache_data = []
        self._browse_cache_cols = []
        self._browse_sort_col = None
        self._browse_sort_dir = "ASC"
        self._browse_offset = 0
        self._browse_filter_after = None
        self._schema_filter_after = None
        self._load_time = 0
        self._measure_font = None
        self._measure_bold_font = None

        self._build_header()
        self._build_body()
        self._bind_keys()
        self._setup_tooltips()
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)

        if initial_path and os.path.isfile(initial_path):
            self.after(200, lambda: self._open_db(initial_path))

    def _set_app_icon(self):
        """Generate and set a programmatic app icon (no external file needed)."""
        try:
            # Create a 32x32 icon image using a PhotoImage
            icon_size = 32
            img = tk.PhotoImage(width=icon_size, height=icon_size)
            # Draw a simple database cylinder icon with blue/white colors
            # Background: transparent
            # Blue database body
            for y in range(8, 26):
                for x in range(6, 26):
                    img.put("#3b82f6", (x, y))
            # Top ellipse (lighter blue)
            for x in range(6, 26):
                dx = x - 16
                if abs(dx) <= 10:
                    for dy in range(-3, 4):
                        ry = 8 + dy
                        if 0 <= ry < icon_size and (dx * dx / 100 + dy * dy / 9) <= 1:
                            img.put("#60a5fa", (x, ry))
            # Bottom ellipse
            for x in range(6, 26):
                dx = x - 16
                if abs(dx) <= 10:
                    for dy in range(-3, 4):
                        ry = 24 + dy
                        if 0 <= ry < icon_size and (dx * dx / 100 + dy * dy / 9) <= 1:
                            img.put("#2563eb", (x, ry))
            # Magnifying glass (white circle + handle)
            for x in range(18, 30):
                for y in range(16, 28):
                    dx = x - 23
                    dy = y - 21
                    r2 = dx * dx + dy * dy
                    if 16 <= r2 <= 36:
                        if 0 <= x < icon_size and 0 <= y < icon_size:
                            img.put("white", (x, y))
            # Handle
            for i in range(5):
                px = 27 + i
                py = 25 + i
                if 0 <= px < icon_size and 0 <= py < icon_size:
                    img.put("white", (px, py))
                    if px + 1 < icon_size:
                        img.put("white", (px + 1, py))
            self._app_icon = img  # prevent GC
            self.wm_iconphoto(True, img)
        except Exception:
            pass  # Icon is cosmetic; don't crash if it fails

    # ── Header ───────────────────────────────────────────────────────
    def _build_header(self):
        hf = ttk.Frame(self, style="H.TFrame")
        hf.pack(fill="x")
        inner = ttk.Frame(hf, style="H.TFrame")
        inner.pack(fill="x", padx=10, pady=6)

        # Canvas logo - larger and more prominent
        logo = tk.Canvas(inner, width=48, height=44, bg=C["hbg"], highlightthickness=0)
        logo.pack(side="left", padx=(0, 10))
        # Database cylinder - stacked disks
        logo.create_rectangle(8, 10, 36, 34, fill="#3b82f6", outline="")
        logo.create_oval(8, 4, 36, 16, fill="#60a5fa", outline="#2563eb", width=1.5)
        logo.create_oval(8, 14, 36, 24, outline="#93c5fd", width=1)
        logo.create_oval(8, 28, 36, 38, fill="#2563eb", outline="#1d4ed8", width=1.5)
        logo.create_line(8, 10, 8, 33, fill="#1d4ed8", width=1.5)
        logo.create_line(36, 10, 36, 33, fill="#1d4ed8", width=1.5)
        # Magnifying glass - prominent white
        logo.create_oval(24, 18, 40, 34, outline="white", width=2.5)
        logo.create_line(38, 32, 46, 42, fill="white", width=3, capstyle=tk.ROUND)

        ttk.Label(inner, text="  SQLite GUI Analyzer", style="H.TLabel").pack(side="left")

        self._db_info = ttk.Label(inner, text="  No database loaded", style="HI.TLabel")
        self._db_info.pack(side="left", padx=(16, 0))

        # Buttons right side
        ttk.Button(inner, text="Close DB", style="HB.TButton",
                   command=self._close_db).pack(side="right", padx=3)
        ttk.Button(inner, text="Help", style="HB.TButton",
                   command=lambda: HelpDialog(self)).pack(side="right", padx=3)
        ttk.Button(inner, text="Info", style="HB.TButton",
                   command=self._show_info).pack(side="right", padx=3)
        ttk.Button(inner, text="Open", style="HB.TButton",
                   command=self._open_file).pack(side="right", padx=3)

    # ── Body ─────────────────────────────────────────────────────────
    def _build_body(self):
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        # Sidebar container with collapse toggle
        self._sidebar_visible = True
        sb_outer = ttk.Frame(body)
        sb_outer.pack(side="left", fill="y")
        self._sb_outer = sb_outer

        # Toggle button
        self._sb_toggle = tk.Button(
            sb_outer, text="\u25C0", font=("Segoe UI", 8), width=2, bd=0,
            bg=C["sbg"], fg=C["text2"], activebackground=C["bg4"],
            command=self._toggle_sidebar, relief="flat", cursor="hand2")
        self._sb_toggle.pack(side="right", fill="y")

        self._sidebar = ttk.Frame(sb_outer, style="S.TFrame", width=300)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)
        self._build_sidebar()

        # Main area with notebook
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)

        self._nb = ttk.Notebook(main)
        self._nb.pack(fill="both", expand=True)

        self._search_frame = ttk.Frame(self._nb)
        self._browse_frame = ttk.Frame(self._nb)

        self._nb.add(self._search_frame, text="  Search  ")
        self._nb.add(self._browse_frame, text="  Browse  ")

        # WAL tab — added dynamically when a WAL-mode DB is opened
        self._wal_frame = ttk.Frame(self._nb)
        self._wal_tab_added = False

        self._build_search_tab()
        self._build_browse_tab()

    # ── Sidebar ──────────────────────────────────────────────────────
    def _build_sidebar(self):
        sf = self._sidebar

        ttk.Label(sf, text="Schema", style="B.TLabel").pack(padx=8, pady=(8, 2), anchor="w")

        filt_f = ttk.Frame(sf, style="S.TFrame")
        filt_f.pack(fill="x", padx=8, pady=2)
        self._schema_filter_var = tk.StringVar()
        self._schema_filter_var.trace_add("write", self._on_schema_filter)
        ttk.Entry(filt_f, textvariable=self._schema_filter_var, width=18).pack(side="left", fill="x", expand=True)
        self._schema_cols_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(filt_f, text="Cols", variable=self._schema_cols_var,
                         command=lambda: self._on_schema_filter()).pack(side="left", padx=4)

        tree_f = ttk.Frame(sf, style="S.TFrame")
        tree_f.pack(fill="both", expand=True, padx=4, pady=4)

        self._schema_tree = ttk.Treeview(tree_f, style="Sc.Treeview", show="tree", selectmode="browse")
        ssb_x = ttk.Scrollbar(tree_f, orient="horizontal", command=self._schema_tree.xview)
        ssb = ttk.Scrollbar(tree_f, orient="vertical", command=self._schema_tree.yview)
        self._schema_tree.configure(yscrollcommand=ssb.set, xscrollcommand=ssb_x.set)
        self._schema_tree.column("#0", minwidth=280, width=500, stretch=True)
        ssb.pack(side="right", fill="y")
        ssb_x.pack(side="bottom", fill="x")
        self._schema_tree.pack(fill="both", expand=True)
        self._schema_tree.bind("<<TreeviewOpen>>", self._on_schema_expand)
        self._schema_tree.bind("<Double-1>", self._on_schema_dblclick)
        self._schema_tree.bind("<Button-3>", self._on_schema_rightclick)

        # SQL preview
        self._sql_preview = tk.Text(sf, height=5, font=("Consolas", 8), bg=C["bg2"],
                                     fg=C["text"], wrap="word", state="disabled")
        self._sql_preview.pack(fill="x", padx=8, pady=(0, 4))

        btn_f = ttk.Frame(sf, style="S.TFrame")
        btn_f.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_f, text="Copy Schema", style="Sm.TButton",
                   command=self._copy_schema).pack(side="left", padx=2)
        ttk.Button(btn_f, text="Copy SQL", style="Sm.TButton",
                   command=self._copy_sql).pack(side="left", padx=2)

    def _populate_schema(self):
        tree = self._schema_tree
        tree.delete(*tree.get_children())
        if not self.db.ok:
            return
        tables = self.db.tables()
        self._scope_tables = list(tables)

        # Tables node
        tbl_node = tree.insert("", "end", text="Tables", open=True, tags=("header",))
        for t in tables:
            cnt = self._count_cache.get(t, "?")
            iid = tree.insert(tbl_node, "end", text=f"{t}  ({fmt_count(cnt)})", values=(t, "table"))
            tree.insert(iid, "end", text="loading...")  # placeholder

        # Views
        views = self.db.views()
        if views:
            v_node = tree.insert("", "end", text="Views", open=False, tags=("header",))
            for v in views:
                tree.insert(v_node, "end", text=v, values=(v, "view"))

        # Indexes
        indexes = self.db.all_indexes()
        if indexes:
            i_node = tree.insert("", "end", text="Indexes", open=False, tags=("header",))
            for idx in indexes:
                tree.insert(i_node, "end", text=idx, values=(idx, "index"))

        # Triggers
        triggers = self.db.triggers()
        if triggers:
            tr_node = tree.insert("", "end", text="Triggers", open=False, tags=("header",))
            for trg in triggers:
                tree.insert(tr_node, "end", text=trg, values=(trg, "trigger"))

        # WAL-Only Tables
        if self.db.has_wal:
            wal_only = self.db.wal_tables()
            if wal_only:
                wal_node = tree.insert("", "end", text=f"WAL-Only Tables ({len(wal_only)})",
                                       open=False, tags=("header",))
                col_map = getattr(self.db.wal, 'col_map', {})
                for wt in wal_only:
                    wt_iid = tree.insert(wal_node, "end",
                                         text=f"{wt}  (WAL-only)",
                                         values=(f"WAL: {wt}", "wal_table"))
                    # Show columns if known
                    wcols = col_map.get(wt, [])
                    for wc in wcols:
                        tree.insert(wt_iid, "end", text=f"  {wc}",
                                    values=(f"WAL: {wt}", "wal_column"))

    def _on_schema_expand(self, event):
        iid = self._schema_tree.focus()
        vals = self._schema_tree.item(iid, "values")
        if not vals or len(vals) < 2:
            return
        tbl, typ = vals[0], vals[1]
        if typ != "table":
            return
        children = self._schema_tree.get_children(iid)
        if len(children) == 1 and self._schema_tree.item(children[0], "text") == "loading...":
            self._schema_tree.delete(children[0])
            # Columns
            cols = self.db.columns(tbl)
            for cn, ct in cols:
                self._schema_tree.insert(iid, "end", text=f"  {cn}  ({ct})", values=(tbl, "column"))
            # Indexes
            idxs = self.db.indexes(tbl)
            for name, unique, icols in idxs:
                ustr = " UNIQUE" if unique else ""
                self._schema_tree.insert(iid, "end",
                    text=f"  IDX{ustr}: {name} ({', '.join(icols)})", values=(tbl, "index_detail"))
            # Foreign keys (with ON DELETE/UPDATE details)
            try:
                fks = self.db.fkeys_full(tbl)
                for fk in fks:
                    detail = f"  FK: {fk['from']} \u2192 {fk['table']}({fk['to']})"
                    actions = []
                    if fk.get('on_delete'):
                        actions.append(f"ON DELETE {fk['on_delete']}")
                    if fk.get('on_update'):
                        actions.append(f"ON UPDATE {fk['on_update']}")
                    if actions:
                        detail += f"  [{', '.join(actions)}]"
                    self._schema_tree.insert(iid, "end", text=detail, values=(tbl, "fk"))
            except Exception:
                fks = self.db.fkeys(tbl)
                for ref_tbl, from_col, to_col in fks:
                    self._schema_tree.insert(iid, "end",
                        text=f"  FK: {from_col} \u2192 {ref_tbl}({to_col})", values=(tbl, "fk"))
            # CHECK constraints
            try:
                checks = self.db.check_constraints(tbl)
                for chk in checks:
                    self._schema_tree.insert(iid, "end",
                        text=f"  CHECK: {chk}", values=(tbl, "check"))
            except Exception:
                pass

        # Show CREATE SQL
        sql = self.db.create_sql(tbl)
        self._sql_preview.configure(state="normal")
        self._sql_preview.delete("1.0", "end")
        self._sql_preview.insert("1.0", sql)
        self._sql_preview.configure(state="disabled")

    def _on_schema_dblclick(self, event):
        iid = self._schema_tree.focus()
        vals = self._schema_tree.item(iid, "values")
        if not vals or len(vals) < 2:
            return
        tbl, typ = vals[0], vals[1]
        if typ in ("table", "column", "index_detail", "fk"):
            self._nb.select(self._browse_frame)
            self._browse_table_var.set(tbl)
            self._load_browse_table()
        elif typ in ("wal_table", "wal_column"):
            self._nb.select(self._browse_frame)
            self._browse_table_var.set(tbl)  # tbl is already "WAL: name"
            self._load_browse_table()

    def _on_schema_rightclick(self, event):
        iid = self._schema_tree.identify_row(event.y)
        if not iid:
            return
        self._schema_tree.selection_set(iid)
        vals = self._schema_tree.item(iid, "values")
        if not vals or len(vals) < 2:
            return
        tbl, typ = vals[0], vals[1]
        if typ not in ("table", "column", "index_detail", "fk"):
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"Browse '{tbl}'",
                         command=lambda: self._schema_ctx_browse(tbl))
        menu.add_command(label="Show 10 Rows",
                         command=lambda: self._schema_ctx_sample(tbl))
        menu.add_separator()
        menu.add_command(label="Copy CREATE SQL",
                         command=lambda: self._schema_ctx_copy_sql(tbl))
        menu.add_command(label="Copy Schema Text",
                         command=lambda: self._schema_ctx_copy_schema(tbl))
        menu.add_command(label="Copy Table Name",
                         command=lambda: self._schema_ctx_copy_name(tbl))
        menu.add_separator()
        menu.add_command(label="Export All Schema (HTML)",
                         command=self._schema_export_html)
        menu.add_separator()
        menu.add_command(label=f"Search in '{tbl}'",
                         command=lambda: self._schema_ctx_search(tbl))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _schema_ctx_browse(self, tbl):
        self._nb.select(self._browse_frame)
        self._browse_table_var.set(tbl)
        self._load_browse_table()

    def _schema_ctx_copy_sql(self, tbl):
        sql = self.db.create_sql(tbl)
        if sql:
            self.clipboard_clear()
            self.clipboard_append(sql)

    def _schema_ctx_copy_schema(self, tbl):
        self.clipboard_clear()
        self.clipboard_append(_build_schema_text(self.db, tbl, self._count_cache.get(tbl, "?")))

    def _schema_ctx_copy_name(self, tbl):
        self.clipboard_clear()
        self.clipboard_append(tbl)

    def _schema_ctx_sample(self, tbl):
        """Show 10 sample rows in a popup window."""
        cols, rows = self.db.browse(tbl, 10, 0)
        w = tk.Toplevel(self)
        w.title(f"Sample: {tbl} (first 10 rows)")
        w.geometry("900x400")
        w.configure(bg=C["bg"])
        # Tree
        tree = ttk.Treeview(w, columns=cols, show="headings", height=10)
        sb = ttk.Scrollbar(w, orient="vertical", command=tree.yview)
        sbh = ttk.Scrollbar(w, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=sb.set, xscrollcommand=sbh.set)
        sb.pack(side="right", fill="y")
        sbh.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=120, minwidth=60, anchor="w")
        for ri, row in enumerate(rows):
            tag = "odd" if ri % 2 else "even"
            tree.insert("", "end", values=[vb(v) for v in row], tags=(tag,))
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])
        # Bottom bar
        bot = tk.Frame(w, bg=C["bg3"])
        bot.pack(fill="x")
        tk.Label(bot, text=f"{len(rows)} rows  |  {len(cols)} columns",
                 font=("Segoe UI", 8), bg=C["bg3"], fg=C["text2"]).pack(side="left", padx=6, pady=3)
        tk.Button(bot, text="Open in Browse", font=("Segoe UI", 8),
                  command=lambda: (w.destroy(), self._schema_ctx_browse(tbl)),
                  relief="flat", bg=C["bg"], fg=C["accent"], padx=8).pack(side="right", padx=4, pady=3)
        tk.Button(bot, text="Close", font=("Segoe UI", 8),
                  command=w.destroy, relief="flat", bg=C["bg"], padx=8).pack(side="right", padx=4, pady=3)

    def _schema_export_html(self):
        """Export full schema report as HTML."""
        if not self.db.ok:
            return
        tables = self.db.tables()
        path = filedialog.asksaveasfilename(
            defaultextension=".html",
            initialfile="schema_report.html",
            filetypes=[("HTML", "*.html"), ("All", "*.*")])
        if not path:
            return
        try:
            html = _build_schema_html(self.db, self.db._path or "", VERSION,
                                      tables=tables, row_counts=self._count_cache)
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            messagebox.showinfo("Exported", f"Schema report exported:\n{os.path.basename(path)}\n"
                                f"{len(tables)} tables documented.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _schema_ctx_search(self, tbl):
        self._scope_tables = [tbl]
        self._nb.select(self._search_frame)
        self._search_entry.focus_set()

    def _on_schema_filter(self, *args):
        if self._schema_filter_after:
            self.after_cancel(self._schema_filter_after)
        self._schema_filter_after = self.after(300, self._apply_schema_filter)

    def _apply_schema_filter(self):
        filt = self._schema_filter_var.get().lower().strip()
        search_cols = self._schema_cols_var.get()
        if not filt:
            self._populate_schema()
            return
        tree = self._schema_tree
        tree.delete(*tree.get_children())
        if not self.db.ok:
            return
        tables = self.db.tables()
        tbl_node = tree.insert("", "end", text=f"Matches", open=True)
        for t in tables:
            tbl_match = filt in t.lower()
            col_matches = []
            if search_cols:
                cols = self.db.columns(t)
                col_matches = [(cn, ct) for cn, ct in cols if filt in cn.lower()]
            if tbl_match or col_matches:
                cnt = self._count_cache.get(t, "?")
                iid = tree.insert(tbl_node, "end", text=f"{t}  ({fmt_count(cnt)})",
                                  values=(t, "table"), open=bool(col_matches))
                if col_matches:
                    # Show matching columns directly — no need to expand
                    for cn, ct in col_matches:
                        tree.insert(iid, "end",
                                    text=f"  >> {cn}  ({ct})",
                                    values=(t, "column"),
                                    tags=("col_match",))
                else:
                    tree.insert(iid, "end", text="loading...")
        tree.tag_configure("col_match", foreground=C["orange"])

    def _copy_schema(self):
        if not self.db.ok:
            return
        lines = []
        for t in self.db.tables():
            cols_full = self.db.columns_full(t)
            uniq = self.db.unique_columns(t)
            cnt = self._count_cache.get(t, "?")
            lines.append(f"TABLE: {t} ({fmt_count(cnt)} rows)")
            for cn, ct, notnull, default, pk in cols_full:
                flags = []
                if pk:
                    flags.append("PK")
                if notnull:
                    flags.append("NOT NULL")
                if default is not None:
                    flags.append(f"DEFAULT {default}")
                if cn in uniq:
                    flags.append("UNIQUE")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                lines.append(f"  {cn} ({ct or ''}){flag_str}")
            lines.append("")
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))

    def _copy_sql(self):
        self.clipboard_clear()
        self._sql_preview.configure(state="normal")
        txt = self._sql_preview.get("1.0", "end").strip()
        self._sql_preview.configure(state="disabled")
        self.clipboard_append(txt)

    # ── Search Tab ───────────────────────────────────────────────────
    def _build_search_tab(self):
        sf = self._search_frame

        # Top controls
        top = ttk.Frame(sf)
        top.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(top, text="Search:", style="B.TLabel").pack(side="left")
        self._search_var = tk.StringVar()
        self._search_entry = ttk.Entry(top, textvariable=self._search_var, width=40, style="SE.TEntry")
        self._search_entry.pack(side="left", padx=6, fill="x", expand=True)

        self._search_mode_var = tk.StringVar(value="Case-Insensitive")
        self._mode_combo = ttk.Combobox(top, textvariable=self._search_mode_var,
                                         values=list(SEARCH_MODES.keys()), state="readonly", width=16)
        self._mode_combo.pack(side="left", padx=4)
        self._mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)


        self._deep_blob_var = tk.BooleanVar(value=False)
        self._deep_blob_cb = ttk.Checkbutton(top, text="Deep BLOB", variable=self._deep_blob_var)
        self._deep_blob_cb.pack(side="left", padx=4)

        self._search_wal_var = tk.BooleanVar(value=False)
        self._search_wal_cb = ttk.Checkbutton(top, text="Include Hidden Data (WAL)",
                                                variable=self._search_wal_var)
        # Don't pack yet — only shown when a DB with WAL is opened

        self._search_max_lbl = ttk.Label(top, text="Max/table:")
        self._search_max_lbl.pack(side="left", padx=(8, 2))
        self._limit_var = tk.StringVar(value="500")
        limit_combo = ttk.Combobox(top, textvariable=self._limit_var,
                                    values=["100", "500", "1000", "5000", "All"],
                                    state="readonly", width=6)
        limit_combo.pack(side="left")

        # Hint label
        self._hint_label = ttk.Label(sf, text="", style="M.TLabel", foreground=C["green"])
        self._hint_label.pack(fill="x", padx=16, pady=(0, 2))

        # Buttons row
        btn_row = ttk.Frame(sf)
        btn_row.pack(fill="x", padx=10, pady=2)

        self._search_btn = ttk.Button(btn_row, text="Search", style="P.TButton",
                   command=self._do_search)
        self._search_btn.pack(side="left", padx=3)
        self._stop_btn = ttk.Button(btn_row, text="Stop", style="D.TButton",
                   command=self._stop_search)
        self._stop_btn.pack(side="left", padx=3)
        self._scope_btn = ttk.Button(btn_row, text="Scope", command=self._show_scope)
        self._scope_btn.pack(side="left", padx=3)
        self._reset_scope_btn = ttk.Button(btn_row, text="Reset Scope", command=self._reset_scope)
        self._reset_scope_btn.pack(side="left", padx=3)

        ttk.Button(btn_row, text="Export Details", command=self._search_export_details).pack(side="right", padx=3)
        ttk.Button(btn_row, text="Export CSV", command=self._search_export_csv).pack(side="right", padx=3)
        ttk.Button(btn_row, text="Export JSON", command=self._search_export_json).pack(side="right", padx=3)
        ttk.Button(btn_row, text="Copy Results", command=self._search_copy).pack(side="right", padx=3)

        self._search_err_btn = ttk.Button(btn_row, text="0 errors", style="Sm.TButton",
                                           command=self._show_search_errors)
        self._search_err_btn.pack(side="right", padx=3)

        # Progress
        prog_f = ttk.Frame(sf)
        prog_f.pack(fill="x", padx=10, pady=2)
        self._search_progress = ttk.Progressbar(prog_f, mode="determinate")
        self._search_progress.pack(fill="x", side="left", expand=True, padx=(0, 8))
        self._search_status = ttk.Label(prog_f, text="Ready", style="M.TLabel")
        self._search_status.pack(side="right")

        # Compact filter + pagination bar
        self._sr_page = 0
        self._sr_page_size = 200
        self._sr_filtered = []
        self._search_start_time = time.time()

        fp_bar = ttk.Frame(sf)
        fp_bar.pack(fill="x", padx=10, pady=(1, 2))

        ttk.Label(fp_bar, text="Source:", font=("Segoe UI", 8)).pack(side="left")
        self._sr_source_filter = ttk.Combobox(fp_bar, values=["All", "DB", "WAL"], state="readonly", width=6)
        self._sr_source_filter.set("All")
        self._sr_source_filter.pack(side="left", padx=(1, 4))
        self._sr_source_filter.bind("<<ComboboxSelected>>", lambda e: self._filter_search_results())

        ttk.Label(fp_bar, text="Table:", font=("Segoe UI", 8)).pack(side="left")
        self._sr_table_filter = ttk.Combobox(fp_bar, values=["All"], state="readonly", width=32)
        self._sr_table_filter.set("All")
        self._sr_table_filter.pack(side="left", padx=(1, 4))
        self._sr_table_filter.bind("<<ComboboxSelected>>", lambda e: self._filter_search_results())
        self._sr_table_filter.configure(postcommand=lambda: self._auto_dropdown_width(self._sr_table_filter))

        ttk.Label(fp_bar, text="Col:", font=("Segoe UI", 8)).pack(side="left")
        self._sr_col_filter = ttk.Combobox(fp_bar, values=["All"], state="readonly", width=24)
        self._sr_col_filter.set("All")
        self._sr_col_filter.pack(side="left", padx=(1, 4))
        self._sr_col_filter.bind("<<ComboboxSelected>>", lambda e: self._filter_search_results())
        self._sr_col_filter.configure(postcommand=lambda: self._auto_dropdown_width(self._sr_col_filter))

        ttk.Label(fp_bar, text="Type:", font=("Segoe UI", 8)).pack(side="left")
        self._sr_type_filter = ttk.Combobox(fp_bar, values=["All"], state="readonly", width=8)
        self._sr_type_filter.set("All")
        self._sr_type_filter.pack(side="left", padx=(1, 6))
        self._sr_type_filter.bind("<<ComboboxSelected>>", lambda e: self._filter_search_results())

        ttk.Button(fp_bar, text="\u25C0", width=3, command=self._sr_prev_page).pack(side="left", padx=1)
        ttk.Button(fp_bar, text="\u25B6", width=3, command=self._sr_next_page).pack(side="left", padx=1)
        self._sr_page_label = ttk.Label(fp_bar, text="", font=("Segoe UI", 8))
        self._sr_page_label.pack(side="left", padx=4)

        # Results treeview in bordered frame
        border_frame = tk.Frame(sf, relief="solid", bd=1, bg=C["border"])
        border_frame.pack(fill="both", expand=True, padx=10, pady=(2, 10))

        cols = ("#", "Source", "Table", "Column", "RowID", "Matched Value", "Type")
        self._search_tree = ttk.Treeview(border_frame, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self._search_tree.heading(c, text=c)
        self._search_tree.column("#", width=50, minwidth=40, stretch=False)
        self._search_tree.column("Source", width=120, minwidth=70, stretch=False)
        self._search_tree.column("Table", width=200, minwidth=120, stretch=True)
        self._search_tree.column("Column", width=160, minwidth=100, stretch=True)
        self._search_tree.column("RowID", width=70, minwidth=50, stretch=False)
        self._search_tree.column("Matched Value", width=450, minwidth=200, stretch=True)
        self._search_tree.column("Type", width=70, minwidth=50, stretch=False)

        xsb = ttk.Scrollbar(border_frame, orient="horizontal", command=self._search_tree.xview)
        ysb = ttk.Scrollbar(border_frame, orient="vertical", command=self._search_tree.yview)
        self._search_tree.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self._search_tree.pack(fill="both", expand=True)
        self._search_tree.bind("<Double-1>", self._on_search_dblclick)
        self._search_tree.bind("<Return>", self._on_search_dblclick)
        self._search_tree.bind("<Button-3>", self._on_search_rightclick)
        TreeviewTooltip(self._search_tree)

        self._search_results = []
        self._search_errors = []

    def _on_mode_change(self, event=None):
        mode = self._search_mode_var.get()
        mk = SEARCH_MODES.get(mode, "ci")
        if mk == "rx":
            self._hint_label.configure(
                text="Tip: Type \\. for literal dot (not \\\\.). Omit ^ and $ to find patterns within values.",
                foreground=C["green"])
        elif mk == "blob":
            self._hint_label.configure(
                text="Searches text in binary data. Enable 'Deep BLOB' for hex-level matching.",
                foreground=C["orange"])
        elif mk == "col":
            self._hint_label.configure(
                text="Finds columns whose name contains your search text.",
                foreground=C["purple"])
        else:
            self._hint_label.configure(text="")

    def _do_search(self):
        term = self._search_var.get()
        if not term or not self.db.ok:
            return
        self._stop_search()
        # Wait for previous search thread to fully stop (avoids connection contention)
        if self._search_thread and self._search_thread.is_alive():
            self._search_thread.join(timeout=3)
        self._search_cancel = False
        self._search_results = []
        self._search_errors = []
        self._search_tree.delete(*self._search_tree.get_children())
        self._search_err_btn.configure(text="0 errors")
        self._search_start_time = time.time()

        self._count_cancel = True  # Cancel bg counting to free connection

        mode = self._search_mode_var.get()
        self._search_term = term
        self._search_mode_key = SEARCH_MODES.get(mode, "ci")
        lv = self._limit_var.get()
        limit = 999999 if lv == "All" else int(lv)
        deep = self._deep_blob_var.get()
        tables = list(self._scope_tables) if self._scope_tables else self.db.tables()

        self._search_progress.configure(maximum=len(tables), value=0)
        self._search_status.configure(text=f"Searching {len(tables)} tables...")

        self._search_thread = threading.Thread(target=self._search_worker,
                                                args=(tables, term, mode, limit, deep), daemon=True)
        self._search_thread.start()

    def _search_worker(self, tables, term, mode, limit, deep):
        last_update = 0
        last_progress = 0
        count = 0
        self._search_table_counts = {}  # per-table result count
        for ti, tbl in enumerate(tables):
            if self._search_cancel:
                break
            cols = self.db.columns(tbl)
            tbl_count = 0
            try:
                for result in self.db.search(tbl, cols, term, mode, limit, deep,
                                              cancel=lambda: self._search_cancel):
                    if self._search_cancel:
                        break
                    result["source"] = "DB"
                    count += 1
                    tbl_count += 1
                    self._search_results.append(result)
                    now = time.time()
                    if now - last_update >= 0.3:
                        last_update = now
                        self.after(0, self._update_search_ui, count, ti, len(tables))
            except Exception as e:
                self._search_errors.append(f"{tbl}: {e}")
            if tbl_count > 0:
                self._search_table_counts[tbl] = tbl_count
            now = time.time()
            if now - last_progress >= 0.3:
                last_progress = now
                self.after(0, lambda i=ti: self._search_progress.configure(value=i + 1))

        # Phase 2: Search WAL if checkbox is checked and WAL exists
        if self._search_wal_var.get() and self.db.has_wal:
            self.after(0, lambda: self._search_status.configure(
                text=f"Searching WAL frames..."))
            wal_table_counts = {}
            try:
                for result in self.db.wal.search(term, mode, limit=limit,
                                                  cancel=lambda: self._search_cancel):
                    if self._search_cancel:
                        break
                    count += 1
                    self._search_results.append(result)
                    # Count per real table name
                    key = f"WAL: {result['table']}"
                    wal_table_counts[key] = wal_table_counts.get(key, 0) + 1
                    now = time.time()
                    if now - last_update >= 0.3:
                        last_update = now
                        self.after(0, self._update_search_ui, count,
                                   len(tables), len(tables))
            except Exception as e:
                self._search_errors.append(f"WAL: {e}")
            self._search_table_counts.update(wal_table_counts)

        self.after(0, self._finalize_search, count, len(tables))

    def _update_search_ui(self, count, ti, total):
        elapsed = time.time() - self._search_start_time
        self._search_status.configure(text=f"Found {count}... ({ti + 1}/{total} tables, {elapsed:.1f}s)")
        self._search_err_btn.configure(text=f"{len(self._search_errors)} errors")
        # Progressive display: append only new results (no flicker)
        snap = list(self._search_results)
        self._sr_filtered = snap
        tree = self._search_tree
        existing = len(tree.get_children())
        page_size = self._sr_page_size
        term = getattr(self, '_search_term', '')
        mkey = getattr(self, '_search_mode_key', 'ci')
        # Only add items that are new and within page 0
        for i in range(existing, min(len(snap), page_size)):
            r = snap[i]
            source = r.get("source", "DB")
            tag = "odd" if i % 2 else "even"
            # Color-code WAL results by category
            if source.startswith("WAL"):
                cat = r.get("category", "")
                if cat == "committed":
                    tag = "wal_committed"
                elif cat == "uncommitted":
                    tag = "wal_uncommitted"
                elif cat == "old":
                    tag = "wal_old"
            val = _snippet(r["value"], term, mkey) if term else r["value"]
            tree.insert("", "end", values=(i + 1, source, r["table"], r["column"],
                        r["rowid"], val, r["type"]), tags=(tag,))
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])
        tree.tag_configure("wal_committed", background=C["gl"])
        tree.tag_configure("wal_uncommitted", background="#fff8e6")
        tree.tag_configure("wal_old", background=C["rl"])
        total_results = len(snap)
        total_pages = max(1, (total_results + page_size - 1) // page_size)
        showing = min(total_results, page_size)
        self._sr_page_label.configure(
            text=f"Page 1 of {total_pages}  |  Showing {showing} of {total_results} results (searching...)")

    def _finalize_search(self, count, total):
        elapsed = time.time() - self._search_start_time
        status = "Cancelled" if self._search_cancel else "Complete"
        tc = getattr(self, '_search_table_counts', {})
        tables_hit = len(tc)
        self._search_status.configure(
            text=f"{status}: {count} results across {tables_hit} tables ({total} scanned, {elapsed:.1f}s)")
        self._search_err_btn.configure(text=f"{len(self._search_errors)} errors")
        self._search_progress.configure(value=total)
        # Update filter combos — table filter shows per-table counts
        tbl_labels = [f"{t} ({tc[t]})" for t in sorted(tc.keys())]
        columns = sorted(set(r["column"] for r in self._search_results))
        types = sorted(set(r["type"] for r in self._search_results))
        self._sr_table_filter.configure(values=["All"] + tbl_labels)
        self._sr_col_filter.configure(values=["All"] + columns)
        self._sr_type_filter.configure(values=["All"] + types)
        # Dynamically size combobox width to fit longest entry
        max_tbl = max((len(l) for l in tbl_labels), default=5)
        self._sr_table_filter.configure(width=max(32, min(max_tbl + 2, 55)))
        max_col = max((len(c) for c in columns), default=5)
        self._sr_col_filter.configure(width=max(24, min(max_col + 2, 45)))
        self._sr_table_filter.set("All")
        self._sr_col_filter.set("All")
        self._sr_type_filter.set("All")
        # Auto-resize treeview Table/Column columns to fit longest name
        # Use tkfont to measure actual pixel width for accuracy
        if not self._measure_font:
            self._measure_font = tkfont.nametofont("TkDefaultFont")
        mf = self._measure_font
        tbl_names = sorted(tc.keys()) if tc else []
        if tbl_names:
            longest_tbl_px = max(mf.measure(t) for t in tbl_names)
            tbl_px = max(220, min(longest_tbl_px + 30, 500))
            self._search_tree.column("Table", width=tbl_px)
        if columns:
            longest_col_px = max(mf.measure(c) for c in columns)
            col_px = max(170, min(longest_col_px + 30, 400))
            self._search_tree.column("Column", width=col_px)
        # Display first page
        self._sr_filtered = list(self._search_results)
        self._sr_page = 0
        self._display_search_page()

    @staticmethod
    def _auto_dropdown_width(combo):
        """Widen combobox dropdown popup to fit the longest entry."""
        try:
            pd = combo.tk.call('ttk::combobox::PopdownWindow', str(combo))
            combo.tk.call(pd + '.f.l', 'configure', '-width', 0)
        except Exception:
            pass

    def _filter_search_results(self):
        src_f = self._sr_source_filter.get()
        tbl_f = self._sr_table_filter.get()
        col_f = self._sr_col_filter.get()
        type_f = self._sr_type_filter.get()
        filtered = self._search_results
        if src_f == "DB":
            filtered = [r for r in filtered if r.get("source", "DB").startswith("DB")]
        elif src_f == "WAL":
            filtered = [r for r in filtered if r.get("source", "DB").startswith("WAL")]
        if tbl_f != "All":
            # Strip count suffix: "table_name (42)" -> "table_name"
            tbl_name = tbl_f.rsplit(" (", 1)[0] if " (" in tbl_f else tbl_f
            if tbl_name.startswith("WAL: "):
                # WAL table filter: "WAL: notes" -> filter by table + WAL source
                wal_tbl = tbl_name[5:]
                filtered = [r for r in filtered
                            if r["table"] == wal_tbl
                            and r.get("source", "DB").startswith("WAL")]
            else:
                filtered = [r for r in filtered if r["table"] == tbl_name]
        if col_f != "All":
            filtered = [r for r in filtered if r["column"] == col_f]
        if type_f != "All":
            filtered = [r for r in filtered if r["type"] == type_f]
        self._sr_filtered = filtered
        self._sr_page = 0
        self._display_search_page()

    def _display_search_page(self):
        tree = self._search_tree
        tree.delete(*tree.get_children())
        start = self._sr_page * self._sr_page_size
        end = start + self._sr_page_size
        page_data = self._sr_filtered[start:end]
        term = getattr(self, '_search_term', '')
        mkey = getattr(self, '_search_mode_key', 'ci')
        for i, r in enumerate(page_data):
            idx = start + i + 1
            source = r.get("source", "DB")
            tag = "odd" if i % 2 else "even"
            # Color-code WAL results by category
            if source.startswith("WAL"):
                cat = r.get("category", "")
                if cat == "committed":
                    tag = "wal_committed"
                elif cat == "uncommitted":
                    tag = "wal_uncommitted"
                elif cat == "old":
                    tag = "wal_old"
            val = _snippet(r["value"], term, mkey) if term else r["value"]
            tree.insert("", "end", values=(idx, source, r["table"], r["column"],
                        r["rowid"], val, r["type"]), tags=(tag,))
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])
        tree.tag_configure("wal_committed", background=C["gl"])
        tree.tag_configure("wal_uncommitted", background="#fff8e6")
        tree.tag_configure("wal_old", background=C["rl"])
        total = len(self._sr_filtered)
        total_pages = max(1, (total + self._sr_page_size - 1) // self._sr_page_size)
        self._sr_page_label.configure(
            text=f"Page {self._sr_page + 1} of {total_pages}  |  Showing {len(page_data)} of {total} results")

    def _sr_prev_page(self):
        if self._sr_page > 0:
            self._sr_page -= 1
            self._display_search_page()

    def _sr_next_page(self):
        total_pages = max(1, (len(self._sr_filtered) + self._sr_page_size - 1) // self._sr_page_size)
        if self._sr_page < total_pages - 1:
            self._sr_page += 1
            self._display_search_page()

    def _stop_search(self):
        self._search_cancel = True

    def _show_scope(self):
        if not self.db.ok:
            return
        tables = self.db.tables()
        dlg = ScopeDlg(self, tables, self._count_cache, self._scope_tables)
        self.wait_window(dlg)
        if dlg.result is not None:
            self._scope_tables = dlg.result

    def _reset_scope(self):
        if self.db.ok:
            self._scope_tables = list(self.db.tables())
            self._search_status.configure(text=f"Scope reset: {len(self._scope_tables)} tables selected")

    def _on_search_dblclick(self, event):
        sel = self._search_tree.selection()
        if not sel:
            return
        vals = self._search_tree.item(sel[0], "values")
        if not vals:
            return
        # Columns: #(0), Source(1), Table(2), Column(3), RowID(4),
        #          Matched Value(5), Type(6)
        source = vals[1]
        tbl = vals[2]
        match_col = vals[3]  # The column where the match was found
        rid = vals[4]
        search_term = getattr(self, '_search_term', '')
        if source.startswith("WAL"):
            # WAL results — show complete row in a detail window
            # Find the matching result to get row_data
            idx = int(vals[0]) - 1  # 1-based display index
            row_data = None
            frame_idx = None
            page_num = None
            if 0 <= idx < len(self._sr_filtered):
                result = self._sr_filtered[idx]
                row_data = result.get("row_data", {})
                frame_idx = result.get("frame_idx")
                page_num = result.get("page_num")
            self._show_wal_row_detail(source, tbl, match_col, rid, vals[5],
                                      row_data, frame_idx=frame_idx,
                                      page_num=page_num)
            return
        if rid and rid != "-":
            try:
                rid = int(rid)
                RowWin.show(self, self.db, tbl, rid,
                            search_term=search_term, match_col=match_col)
            except (ValueError, TypeError):
                pass

    def _on_search_rightclick(self, event):
        """Right-click context menu on search results — Go to WAL Frame."""
        iid = self._search_tree.identify_row(event.y)
        if not iid:
            return
        self._search_tree.selection_set(iid)
        vals = self._search_tree.item(iid, "values")
        if not vals:
            return
        source = vals[1]
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Open Row Detail",
                         command=lambda: self._on_search_dblclick(None))
        if source.startswith("WAL"):
            idx = int(vals[0]) - 1
            frame_idx = None
            page_num = None
            if 0 <= idx < len(self._sr_filtered):
                result = self._sr_filtered[idx]
                frame_idx = result.get("frame_idx")
                page_num = result.get("page_num")
            if frame_idx is not None:
                menu.add_command(
                    label=f"Go to WAL Frame #{frame_idx}",
                    command=lambda fi=frame_idx, pn=page_num:
                        self._navigate_to_wal_frame(fi, pn))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _show_wal_row_detail(self, source, table, match_col, rowid, match_val,
                             row_data, frame_idx=None, page_num=None):
        """Show WAL search result with complete row — multi-line text,
        DB comparison, and WAL-only indicator."""
        win = tk.Toplevel(self)
        win.title(f"WAL Record — {table} (Row {rowid})")
        win.geometry("920x700")
        win.configure(bg=C["bg"])
        win.transient(self)

        # ── Status banner ──
        status_color = "#00875a" if "In DB" in source else \
                       "#c25100" if "WAL Only" in source else "#665500"
        status_bg = "#e3fcef" if "In DB" in source else \
                    "#fff8e6" if "WAL Only" in source else "#faf5e6"
        banner = tk.Frame(win, bg=status_bg, padx=10, pady=6)
        banner.pack(fill="x")
        tk.Label(banner, text=f"Source: {source}   |   Table: {table}   |   RowID: {rowid}",
                 font=("Segoe UI", 10, "bold"), bg=status_bg, fg=status_color,
                 anchor="w").pack(fill="x")

        # ── Check if record exists in main DB — WAL-only detection ──
        db_row = None
        wal_only = False
        try:
            rid_int = int(rowid)
            db_row_dict, db_cols = self.db.full_row(table, rid_int)
            if db_row_dict and len(db_row_dict) > 1:
                db_row = db_row_dict
            else:
                wal_only = True
        except Exception:
            wal_only = True

        # Pre-compute differences for banner text
        diff_cols_count = 0
        if db_row and not wal_only and row_data:
            for col_name, wal_val in row_data.items():
                if col_name in db_row:
                    db_val = db_row.get(col_name)
                    db_val_str = "NULL" if db_val is None else str(db_val)
                    wal_val_str = "NULL" if wal_val is None else str(wal_val)
                    if db_val_str != wal_val_str:
                        diff_cols_count += 1

        if wal_only:
            tk.Label(banner,
                     text="Not in main DB — found only in WAL file. "
                          "(Note: extraction timing may affect this — "
                          "verify against your extraction timestamps.)",
                     font=("Segoe UI", 8), bg=status_bg, fg="#5e6c84",
                     anchor="w").pack(fill="x")
        elif diff_cols_count > 0:
            tk.Label(banner,
                     text=f"Also in main DB but {diff_cols_count} column(s) differ "
                          f"(marked \u25cf red on left). "
                          "Differences may reflect extraction timing.",
                     font=("Segoe UI", 8), bg=status_bg, fg="#5e6c84",
                     anchor="w").pack(fill="x")
        else:
            tk.Label(banner,
                     text="Also exists in main DB with same values.",
                     font=("Segoe UI", 8), bg=status_bg, fg="#5e6c84",
                     anchor="w").pack(fill="x")

        # ── Matched info (only shown when coming from search) ──
        if match_col:
            match_display = match_val[:200] if match_val and len(match_val) > 200 else (match_val or "")
            tk.Label(win, text=f"Matched column: {match_col}   |   Value: {match_display}",
                     font=("Segoe UI", 9), bg=C["bg"], fg=C["text2"],
                     anchor="w", wraplength=830).pack(fill="x", padx=10, pady=(6, 2))
        ttk.Separator(win, orient="horizontal").pack(fill="x", padx=10, pady=4)

        # ── Row data display — scrolled Text widget for multi-line support ──
        if row_data:
            # PanedWindow: column list (left) + value viewer (right)
            pw = ttk.PanedWindow(win, orient="horizontal")
            pw.pack(fill="both", expand=True, padx=10, pady=(2, 6))

            # Left: column list
            left_frame = ttk.Frame(pw)
            pw.add(left_frame, weight=2)

            tk.Label(left_frame, text="Columns",
                     font=("Segoe UI", 9, "bold"), bg=C["bg"],
                     anchor="w").pack(fill="x")
            col_listbox = tk.Listbox(left_frame, font=("Consolas", 10),
                                      selectmode="browse", bg=C["bg2"],
                                      relief="solid", bd=1, width=30)
            col_sb = ttk.Scrollbar(left_frame, orient="vertical",
                                    command=col_listbox.yview)
            col_xsb = ttk.Scrollbar(left_frame, orient="horizontal",
                                     command=col_listbox.xview)
            col_listbox.configure(yscrollcommand=col_sb.set,
                                   xscrollcommand=col_xsb.set)
            col_sb.pack(side="right", fill="y")
            col_xsb.pack(side="bottom", fill="x")
            col_listbox.pack(fill="both", expand=True)

            # Right: value display
            right_frame = ttk.Frame(pw)
            pw.add(right_frame, weight=3)

            val_top = tk.Frame(right_frame, bg=C["bg"])
            val_top.pack(fill="x")
            tk.Label(val_top, text="Value",
                     font=("Segoe UI", 9, "bold"), bg=C["bg"],
                     anchor="w").pack(side="left")

            def _copy_current_value():
                txt = val_text.get("1.0", "end-1c").strip()
                if txt:
                    win.clipboard_clear()
                    win.clipboard_append(txt)
            tk.Button(val_top, text="Copy Value", font=("Segoe UI", 7),
                      relief="flat", bd=0, bg=C["bg"], fg=C["accent"],
                      cursor="hand2", padx=4, pady=1,
                      command=_copy_current_value).pack(side="right", padx=4)

            val_text = tk.Text(right_frame, wrap="word", font=("Consolas", 10),
                               bg=C["bg2"], fg=C["text"], relief="solid", bd=1,
                               padx=8, pady=6)
            val_sb = ttk.Scrollbar(right_frame, orient="vertical",
                                    command=val_text.yview)
            val_text.configure(yscrollcommand=val_sb.set)
            val_sb.pack(side="right", fill="y")
            val_text.pack(fill="both", expand=True)
            val_text.configure(state="disabled")

            # Tag for highlighting differences
            val_text.tag_configure("diff", foreground="#de350b",
                                    font=("Consolas", 10, "bold"))
            val_text.tag_configure("same", foreground="#6b778c")

            # Pre-compute which columns differ from DB
            diff_cols = set()
            if db_row and not wal_only:
                for col_name, wal_val in row_data.items():
                    if col_name in db_row:
                        db_val = db_row.get(col_name)
                        db_val_str = "NULL" if db_val is None else str(db_val)
                        wal_val_str = "NULL" if wal_val is None else str(wal_val)
                        if db_val_str != wal_val_str:
                            diff_cols.add(col_name)

            # Populate column list
            col_items = list(row_data.items())
            for i, (col_name, _) in enumerate(col_items):
                prefix = ""
                if col_name == match_col:
                    prefix = "\u2192 "  # Arrow for matched column
                elif col_name in diff_cols:
                    prefix = "\u25cf "  # Red dot for changed column
                col_listbox.insert("end", f"{prefix}{col_name}")
                if col_name == match_col:
                    col_listbox.itemconfigure(i, bg=C["hl"],
                                               selectbackground="#ffc400")
                elif col_name in diff_cols:
                    col_listbox.itemconfigure(i, fg="#de350b",
                                               selectforeground="#de350b")
            # WAL-only indicator
            if wal_only:
                for i in range(col_listbox.size()):
                    col_listbox.itemconfigure(i, fg="#c25100")

            def _on_col_select(event=None):
                sel = col_listbox.curselection()
                if not sel:
                    return
                idx = sel[0]
                col_name, col_val = col_items[idx]

                val_text.configure(state="normal")
                val_text.delete("1.0", "end")

                # Show WAL value
                val_text.insert("end", f"Column: {col_name}\n", "")
                val_text.insert("end", f"{'─' * 40}\n\n")
                val_text.insert("end", "WAL Value:\n", "")
                val_text.insert("end", f"{col_val}\n")

                # Show DB comparison if record exists in main DB
                if db_row and not wal_only:
                    db_val = db_row.get(col_name, db_row.get("_rid"))
                    if col_name == "_rid":
                        db_val = db_row.get("_rid")
                    # Skip _rid for comparison
                    if col_name in db_row:
                        db_val_str = "NULL" if db_val is None else str(db_val)
                        val_text.insert("end", f"\n{'─' * 40}\n\n")
                        val_text.insert("end", "DB Value:\n", "")
                        if db_val_str == col_val:
                            val_text.insert("end", f"{db_val_str}\n", "same")
                            val_text.insert("end", "\n(Same as WAL)", "same")
                        else:
                            val_text.insert("end", f"{db_val_str}\n", "diff")
                            val_text.insert("end", "\n\u26a0 DIFFERENT from WAL value!", "diff")
                elif wal_only:
                    val_text.insert("end", "\n\n")
                    val_text.insert("end", "\u26a0 WAL-ONLY: No matching record in main DB", "diff")

                val_text.configure(state="disabled")

            col_listbox.bind("<<ListboxSelect>>", _on_col_select)

            # Auto-select the matched column
            for i, (cn, _) in enumerate(col_items):
                if cn == match_col:
                    col_listbox.selection_set(i)
                    col_listbox.see(i)
                    win.after(50, _on_col_select)
                    break
            else:
                # Select first column if no match
                if col_items:
                    col_listbox.selection_set(0)
                    win.after(50, _on_col_select)
        else:
            tk.Label(win, text="Row data not available (column mapping may be incomplete)",
                     font=("Segoe UI", 9), bg=C["bg"], fg=C["text2"],
                     anchor="w").pack(fill="x", padx=10)

        # Bottom toolbar — styled buttons like RowWin
        bot = tk.Frame(win, bg=C["bg3"], bd=0)
        bot.pack(side="bottom", fill="x")
        tk.Frame(bot, bg=C["border"], height=1).pack(fill="x", side="top")
        bot_inner = tk.Frame(bot, bg=C["bg3"])
        bot_inner.pack(fill="x", padx=6, pady=4)
        btn_cfg = dict(font=("Segoe UI", 8), relief="flat", bd=0, cursor="hand2", padx=8, pady=3)

        # Copy group
        grp1 = tk.Frame(bot_inner, bg=C["bg3"])
        grp1.pack(side="left")
        tk.Label(grp1, text="Copy:", font=("Segoe UI", 7), fg=C["text2"], bg=C["bg3"]).pack(side="left", padx=(0, 2))

        source_label = source

        def _copy_json():
            import json as _json
            d = {"table": table, "rowid": rowid, "source": source_label}
            if row_data:
                d["data"] = dict(row_data)
            if frame_idx is not None:
                d["frame"] = frame_idx
                d["page"] = page_num
            win.clipboard_clear()
            win.clipboard_append(_json.dumps(d, indent=2, default=str))

        def _copy_csv():
            import io, csv as _csv
            if row_data:
                buf = io.StringIO()
                w = _csv.writer(buf)
                w.writerow(list(row_data.keys()))
                w.writerow(list(row_data.values()))
                win.clipboard_clear()
                win.clipboard_append(buf.getvalue())

        def _copy_text():
            lines = [f"Table: {table}", f"RowID: {rowid}", f"Source: {source_label}"]
            if row_data:
                for k, v in row_data.items():
                    lines.append(f"  {k}: {v}")
            win.clipboard_clear()
            win.clipboard_append("\n".join(lines))

        for txt, cmd in [("JSON", _copy_json), ("CSV", _copy_csv), ("Text", _copy_text)]:
            tk.Button(grp1, text=txt, command=cmd, bg=C["bg"], fg=C["accent"],
                      activebackground=C["acl"], **btn_cfg).pack(side="left", padx=1)

        # WAL Frame button
        if frame_idx is not None:
            tk.Frame(bot_inner, bg=C["border"], width=1).pack(side="left", fill="y", padx=6, pady=2)
            tk.Button(bot_inner, text=f"Go to Frame #{frame_idx}", bg=C["bg"], fg=C["purple"],
                      activebackground=C["bg2"],
                      command=lambda: (win.destroy(), self._navigate_to_wal_frame(frame_idx, page_num)),
                      **btn_cfg).pack(side="left", padx=1)

        # Close
        tk.Button(bot_inner, text="Close", command=win.destroy,
                  bg=C["bg4"], fg=C["text"], activebackground=C["border"],
                  **btn_cfg).pack(side="right", padx=1)

    def _navigate_to_wal_frame(self, frame_idx, page_num=None):
        """Navigate to a specific WAL frame in the WAL tab."""
        if not self._wal_tab_added or not self.db.has_wal:
            return
        # Switch to WAL tab
        self._nb.select(self._wal_frame)
        # Ensure we are in frame view
        self._switch_wal_view("frames")
        # Reset filters so the frame is visible
        self._wal_cat_var.set("All")
        self._wal_table_var.set("All")
        self._wal_pt_var.set("All")
        self._wal_page_var.set("")
        self._wal_filtered_frames = list(self._wal_all_frames)
        self._display_wal_frames()
        # Select the frame in the tree
        iid = str(frame_idx)
        try:
            self._wal_tree.selection_set(iid)
            self._wal_tree.focus(iid)
            self._wal_tree.see(iid)
            # Trigger the select event to show detail
            self._on_wal_select()
        except Exception:
            pass

    def _show_search_errors(self):
        if not self._search_errors:
            messagebox.showinfo("Errors", "No errors")
            return
        messagebox.showwarning("Search Errors", "\n".join(self._search_errors[:50]))

    def _get_export_scope(self):
        """Ask user which results to export: all, filtered, or current page."""
        if not self._search_results:
            return None
        all_n = len(self._search_results)
        filt_n = len(self._sr_filtered)
        page_start = self._sr_page * self._sr_page_size
        page_data = self._sr_filtered[page_start:page_start + self._sr_page_size]
        page_n = len(page_data)

        dlg = tk.Toplevel(self)
        dlg.title("Export Scope")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()
        dlg.update_idletasks()
        _dw, _dh = 340, 170
        dlg.geometry(f"{_dw}x{_dh}+{self.winfo_rootx() + (self.winfo_width() - _dw) // 2}+{self.winfo_rooty() + (self.winfo_height() - _dh) // 2}")
        result = [None]

        tk.Label(dlg, text="What to export?", font=("Segoe UI", 10, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(pady=(12, 8))
        bf = tk.Frame(dlg, bg=C["bg"])
        bf.pack(fill="x", padx=20)
        btn_cfg = dict(font=("Segoe UI", 9), relief="flat", bd=0, padx=12, pady=5, cursor="hand2")
        def pick(which):
            result[0] = which
            dlg.destroy()
        tk.Button(bf, text=f"All Results ({all_n})", bg=C["acl"], fg=C["accent"],
                  command=lambda: pick("all"), **btn_cfg).pack(fill="x", pady=2)
        if filt_n != all_n:
            tk.Button(bf, text=f"Filtered ({filt_n})", bg="#e3fcef", fg=C["green"],
                      command=lambda: pick("filtered"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text=f"Current Page ({page_n})", bg="#f0f0ff", fg=C["purple"],
                  command=lambda: pick("page"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text="Cancel", bg=C["bg3"], fg=C["text2"],
                  command=dlg.destroy, **btn_cfg).pack(fill="x", pady=2)
        self.wait_window(dlg)
        if result[0] is None:
            return None
        if result[0] == "all":
            return self._search_results
        elif result[0] == "filtered":
            return list(self._sr_filtered)
        else:
            return page_data

    def _search_export_csv(self):
        data = self._get_export_scope()
        if not data:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                             filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["#", "Source", "Table", "Column", "RowID", "Value", "Type",
                            "Page#", "Frame#"])
                for i, r in enumerate(data):
                    w.writerow([i + 1, r.get("source", "DB"), r["table"], r["column"],
                                r["rowid"], r["value"], r["type"],
                                r.get("page_num", ""), r.get("frame_idx", "")])
            messagebox.showinfo("Exported", f"Exported {len(data)} results to:\n{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _search_export_json(self):
        data = self._get_export_scope()
        if not data:
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                             filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            out = [{"source": r.get("source", "DB"), "table": r["table"], "column": r["column"],
                    "rowid": r["rowid"], "value": r["value"], "type": r["type"],
                    "page_num": r.get("page_num", ""), "frame_idx": r.get("frame_idx", "")} for r in data]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2, default=str)
            messagebox.showinfo("Exported", f"Exported {len(out)} results to:\n{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _search_export_details(self):
        """Export search results as forensic JSON with full metadata."""
        data = self._get_export_scope()
        if not data:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile="forensic_export.json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            out = []
            for i, r in enumerate(data):
                entry = {
                    "index": i + 1,
                    "source": r.get("source", "DB"),
                    "table": r["table"],
                    "column": r["column"],
                    "rowid": r["rowid"],
                    "value": r["value"],
                    "type": r["type"],
                    "page_num": r.get("page_num", ""),
                    "frame_idx": r.get("frame_idx", ""),
                    "category": r.get("category", ""),
                }
                # Include full row_data if available (WAL results)
                rd = r.get("row_data")
                if rd:
                    entry["row_data"] = {k: str(v) for k, v in rd.items()}
                out.append(entry)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"forensic_export": True,
                           "total_results": len(out),
                           "results": out}, f, indent=2, default=str)
            messagebox.showinfo("Forensic Export",
                                f"Exported {len(out)} results with full metadata to:\n"
                                f"{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _search_copy(self):
        data = self._get_export_scope()
        if not data:
            return
        lines = ["#\tSource\tTable\tColumn\tRowID\tValue\tType"]
        for i, r in enumerate(data):
            lines.append(f"{i+1}\t{r.get('source', 'DB')}\t{r['table']}\t{r['column']}\t{r['rowid']}\t{r['value']}\t{r['type']}")
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))

    # ── Browse Tab ───────────────────────────────────────────────────
    def _build_browse_tab(self):
        bf = self._browse_frame

        # Top controls
        top = ttk.Frame(bf)
        top.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(top, text="Table:").pack(side="left")
        self._browse_table_var = tk.StringVar()
        self._browse_table_combo = ttk.Combobox(top, textvariable=self._browse_table_var,
                                                  state="readonly", width=28)
        self._browse_table_combo.pack(side="left", padx=4)
        self._browse_table_combo.bind("<<ComboboxSelected>>", lambda e: self._load_browse_table())

        ttk.Label(top, text="Rows/page:").pack(side="left", padx=(12, 2))
        self._browse_limit_var = tk.StringVar(value="200")
        ttk.Combobox(top, textvariable=self._browse_limit_var,
                     values=["50", "100", "200", "500", "1000"], state="readonly",
                     width=6).pack(side="left")

        ttk.Button(top, text="Prev", command=self._browse_prev).pack(side="left", padx=(12, 2))
        ttk.Button(top, text="Next", command=self._browse_next).pack(side="left", padx=2)

        self._browse_page_lbl = ttk.Label(top, text="", style="M.TLabel")
        self._browse_page_lbl.pack(side="left", padx=8)

        ttk.Button(top, text="Export CSV", command=self._browse_export_csv).pack(side="right", padx=3)
        ttk.Button(top, text="Export BLOBs", command=self._browse_export_blobs).pack(side="right", padx=3)

        # Filter row: Column dropdown + filter entry
        filt_f = ttk.Frame(bf)
        filt_f.pack(fill="x", padx=8, pady=2)
        ttk.Label(filt_f, text="Filter:").pack(side="left")
        self._browse_filter_var = tk.StringVar()
        self._browse_filter_var.trace_add("write", self._on_browse_filter)
        self._browse_filter_entry = ttk.Entry(filt_f, textvariable=self._browse_filter_var, width=30)
        self._browse_filter_entry.pack(side="left", padx=4, fill="x", expand=True)
        ttk.Label(filt_f, text="in", style="M.TLabel").pack(side="left", padx=2)
        self._browse_col_filter_var = tk.StringVar(value="All Columns")
        self._browse_col_combo = ttk.Combobox(filt_f, textvariable=self._browse_col_filter_var,
                                               values=["All Columns"], state="readonly", width=20)
        self._browse_col_combo.pack(side="left", padx=4)
        self._browse_col_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_browse_filter())
        self._browse_display_rows = []

        # Per-column filter row (populated dynamically when table loads)
        self._col_filter_frame = tk.Frame(bf, bg=C["bg"])
        self._col_filter_frame.pack(fill="x", padx=8, pady=(0, 0))
        self._col_filters = {}  # col_name -> StringVar
        self._col_filter_timer = None

        # PanedWindow
        pw = ttk.PanedWindow(bf, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=4)

        # TOP pane: bordered treeview container
        tree_outer = tk.Frame(pw, bg=C["border"], bd=1, relief="solid")
        pw.add(tree_outer, weight=3)

        tree_frame = ttk.Frame(tree_outer)
        tree_frame.pack(fill="both", expand=True)

        self._browse_tree = ttk.Treeview(tree_frame, show="headings", selectmode="browse")
        bxsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._browse_tree.xview)
        bysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._browse_tree.yview)
        self._browse_tree.configure(xscrollcommand=bxsb.set, yscrollcommand=bysb.set)
        bysb.pack(side="right", fill="y")
        bxsb.pack(side="bottom", fill="x")
        self._browse_tree.pack(fill="both", expand=True)
        self._browse_tree.bind("<<TreeviewSelect>>", self._on_browse_select)
        self._browse_tree.bind("<Double-1>", self._on_browse_dblclick)
        self._browse_tree.bind("<Return>", self._on_browse_dblclick)
        TreeviewTooltip(self._browse_tree)

        # BOTTOM pane: preview with border
        preview_outer = tk.Frame(pw, bg=C["border"], bd=1, relief="solid")
        pw.add(preview_outer, weight=1)

        preview_frame = ttk.Frame(preview_outer)
        preview_frame.pack(fill="both", expand=True)

        # Row Preview header with copy buttons
        prev_header = tk.Frame(preview_frame, bg=C["bg"])
        prev_header.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(prev_header, text="Row Preview", style="B.TLabel").pack(side="left")

        prev_btn_cfg = dict(font=("Segoe UI", 7), relief="flat", bd=0,
                            cursor="hand2", padx=6, pady=1, bg=C["bg"],
                            fg=C["accent"], activebackground=C["acl"])
        tk.Button(prev_header, text="Copy JSON",
                  command=self._preview_copy_json, **prev_btn_cfg).pack(side="right", padx=1)
        tk.Button(prev_header, text="Copy CSV",
                  command=self._preview_copy_csv, **prev_btn_cfg).pack(side="right", padx=1)
        tk.Button(prev_header, text="Copy Text",
                  command=self._preview_copy_text, **prev_btn_cfg).pack(side="right", padx=1)
        tk.Label(prev_header, text="Copy:", font=("Segoe UI", 7),
                 fg=C["text2"], bg=C["bg"]).pack(side="right", padx=(0, 2))

        prev_canvas = tk.Canvas(preview_frame, bg=C["bg"], highlightthickness=0)
        prev_sb = ttk.Scrollbar(preview_frame, orient="vertical", command=prev_canvas.yview)
        self._preview_inner = ttk.Frame(prev_canvas)
        self._preview_inner.bind("<Configure>",
            lambda e: prev_canvas.configure(scrollregion=prev_canvas.bbox("all")))
        prev_canvas.create_window((0, 0), window=self._preview_inner, anchor="nw")
        prev_canvas.configure(yscrollcommand=prev_sb.set)
        prev_sb.pack(side="right", fill="y")
        prev_canvas.pack(fill="both", expand=True)
        self._preview_canvas = prev_canvas
        def _prev_scroll(e):
            try:
                prev_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        prev_canvas.bind("<MouseWheel>", _prev_scroll)
        self._preview_inner.bind("<MouseWheel>", _prev_scroll)
        self._preview_tk_imgs = []

    def _load_browse_table(self, reset_offset=True):
        tbl = self._browse_table_var.get()
        if not tbl or not self.db.ok:
            return
        # Skip separator item
        if tbl == "---WAL-Only Tables---":
            return
        if reset_offset:
            self._browse_offset = 0
            self._browse_sort_col = None
            self._browse_sort_dir = "ASC"
        lim = int(self._browse_limit_var.get())

        # WAL-only table browsing
        if tbl.startswith("WAL: "):
            real_name = tbl[5:]
            cols, rows, total = self.db.wal_browse(real_name, lim, self._browse_offset)
            self._browse_cache_cols = cols
            self._browse_cache_data = rows
            self._browse_filter_var.set("")
            self._browse_col_combo.configure(values=["All Columns"] + cols)
            self._browse_col_filter_var.set("All Columns")
            self._display_browse_data(cols, rows)
            page = self._browse_offset // lim + 1
            row_start = self._browse_offset + 1
            row_end = self._browse_offset + len(rows)
            ncols = len(cols)
            self._browse_page_lbl.configure(
                text=f"Page {page}  |  Rows {row_start}-{row_end} of {total}  |  {ncols} columns  (WAL-only)")
            return

        cols, rows = self.db.browse(tbl, lim, self._browse_offset,
                                     self._browse_sort_col, self._browse_sort_dir)
        self._browse_cache_cols = cols
        self._browse_cache_data = rows
        self._browse_filter_var.set("")
        # Update column filter dropdown
        self._browse_col_combo.configure(values=["All Columns"] + cols)
        self._browse_col_filter_var.set("All Columns")
        self._display_browse_data(cols, rows)

        cnt = self._count_cache.get(tbl, "?")
        page = self._browse_offset // lim + 1
        ncols = len([c for c in cols if c != "_rid"])
        row_start = self._browse_offset + 1
        row_end = self._browse_offset + len(rows)
        self._browse_page_lbl.configure(
            text=f"Page {page}  |  Rows {row_start}-{row_end} of {fmt_count(cnt)}  |  {ncols} columns")

    def _display_browse_data(self, cols, rows):
        tree = self._browse_tree
        tree.delete(*tree.get_children())

        if not cols:
            tree.configure(columns=())
            return

        tree.configure(columns=cols)
        ncols = len(cols)
        for c in cols:
            tree.heading(c, text=c, command=lambda col=c: self._browse_sort(col))

        # Auto-size columns
        if not self._measure_font:
            self._measure_font = tkfont.Font(family="Segoe UI", size=9)
            self._measure_bold_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        mf = self._measure_font
        bf = self._measure_bold_font

        if ncols <= 8:
            max_w = 500
        elif ncols <= 15:
            max_w = 350
        else:
            max_w = 200

        for ci, c in enumerate(cols):
            hw = bf.measure(c) + 30
            data_w = 60
            for ri in range(min(50, len(rows))):
                if ri < len(rows) and ci < len(rows[ri]):
                    val = vb(rows[ri][ci])
                    cw = mf.measure(str(val)[:60]) + 20
                    data_w = max(data_w, cw)
            # Header width is minimum - column must at least show full header name
            w = max(hw, min(data_w, max_w))
            if c == "_rid":
                w = 72
            tree.column(c, width=w, minwidth=hw, anchor="w")

        # Rebuild per-column filter entries
        for w in self._col_filter_frame.winfo_children():
            w.destroy()
        self._col_filters = {}
        display_cols = [c for c in cols if c != "_rid"]
        for col in display_cols:
            var = tk.StringVar()
            var.trace_add("write", lambda *a: self._schedule_col_filter())
            e = tk.Entry(self._col_filter_frame, textvariable=var, font=("Segoe UI", 8),
                         relief="solid", bd=1, bg="#fffff0")
            e.pack(side="left", fill="x", expand=True, padx=0)
            self._col_filters[col] = var

        # Insert rows with alternating colors
        self._display_browse_rows(rows)
        self._browse_display_rows = rows

    def _display_browse_rows(self, rows):
        """Display rows in the browse treeview (used by both load and filter)."""
        tree = self._browse_tree
        tree.delete(*tree.get_children())
        for i, row in enumerate(rows):
            vals = [vb(v) for v in row]
            tag = "odd" if i % 2 else "even"
            tree.insert("", "end", values=vals, tags=(tag,))
        tree.tag_configure("odd", background=C["alt"])
        tree.tag_configure("even", background=C["bg"])

    def _schedule_col_filter(self):
        """Debounce per-column filter to avoid rapid re-queries."""
        if self._col_filter_timer:
            self.after_cancel(self._col_filter_timer)
        self._col_filter_timer = self.after(400, self._apply_col_filter)

    def _apply_col_filter(self):
        """Filter browse data using per-column filters."""
        self._col_filter_timer = None
        filters = {col: var.get().strip()
                   for col, var in self._col_filters.items() if var.get().strip()}
        if not filters:
            # No filters -- show original data
            self._browse_display_rows = self._browse_cache_data
            self._display_browse_rows(self._browse_cache_data)
            tbl = self._browse_table_var.get()
            lim = int(self._browse_limit_var.get())
            cnt = self._count_cache.get(tbl, "?")
            page = self._browse_offset // lim + 1
            self._browse_page_lbl.configure(
                text=f"Page {page}  |  {fmt_count(cnt)} rows total")
            return

        tbl = self._browse_table_var.get()
        is_wal = tbl.startswith("WAL: ")

        if is_wal:
            # In-memory filter for WAL tables
            cols = self._browse_cache_cols
            matched = []
            for row in self._browse_cache_data:
                match = True
                for col, term in filters.items():
                    if col in cols:
                        idx = cols.index(col)
                        val = str(row[idx]) if idx < len(row) else ""
                        if term.lower() not in val.lower():
                            match = False
                            break
                if match:
                    matched.append(row)
            self._browse_display_rows = matched
            self._display_browse_rows(matched)
            self._browse_page_lbl.configure(
                text=f"Column filter: {len(matched)} matches")
        else:
            # SQL filter for DB tables
            cols = self._browse_cache_cols
            where_parts = []
            params = []
            for col, term in filters.items():
                escaped = _le(term)
                where_parts.append(f"CAST({_q(col)} AS TEXT) LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped}%")
            try:
                sql = (f"SELECT rowid AS _rid, * FROM {_q(tbl)} "
                       f"WHERE {' AND '.join(where_parts)} LIMIT 1000")
                rows = self.db._search_conn.execute(sql, params).fetchall()
                self._browse_display_rows = rows
                self._display_browse_rows(rows)
                self._browse_page_lbl.configure(
                    text=f"Column filter: {len(rows)} matches (max 1000)")
            except Exception:
                pass

    def _browse_sort(self, col):
        if col == self._browse_sort_col:
            self._browse_sort_dir = "DESC" if self._browse_sort_dir == "ASC" else "ASC"
        else:
            self._browse_sort_col = col
            self._browse_sort_dir = "ASC"
        self._load_browse_table(reset_offset=False)

    def _browse_prev(self):
        lim = int(self._browse_limit_var.get())
        self._browse_offset = max(0, self._browse_offset - lim)
        self._load_browse_table(reset_offset=False)

    def _browse_next(self):
        lim = int(self._browse_limit_var.get())
        tbl = self._browse_table_var.get()
        cnt = _int_count(self._count_cache.get(tbl, 0))
        if self._browse_offset + lim >= cnt:
            return
        self._browse_offset += lim
        self._load_browse_table(reset_offset=False)

    def _on_browse_filter(self, *args):
        if self._browse_filter_after:
            self.after_cancel(self._browse_filter_after)
        self._browse_filter_after = self.after(200, self._apply_browse_filter)

    def _apply_browse_filter(self):
        """Filter browse rows by text, optionally restricted to a specific column.

        For regular tables, runs a SQL query against the full table for accurate
        results across all pages. For WAL tables, filters in-memory.
        """
        filt = self._browse_filter_var.get().lower().strip()
        cols = self._browse_cache_cols
        rows = self._browse_cache_data
        col_sel = self._browse_col_filter_var.get()
        if not filt:
            self._display_browse_data(cols, rows)
            return

        tbl = self._browse_table_var.get()

        # WAL tables: filter in-memory only
        if tbl.startswith("WAL: "):
            self._apply_browse_filter_inmemory(filt, cols, rows, col_sel)
            return

        # Regular tables: run SQL LIKE query against the full table
        if self.db.ok and tbl and tbl != "---WAL-Only Tables---":
            try:
                real_cols = [c for c in cols if c != "_rid"]
                escaped = _le(filt)
                if col_sel != "All Columns" and col_sel in real_cols:
                    where = f"CAST({_q(col_sel)} AS TEXT) LIKE ? ESCAPE '\\'"
                    params = [f"%{escaped}%"]
                else:
                    clauses = [f"CAST({_q(c)} AS TEXT) LIKE ? ESCAPE '\\'"
                               for c in real_cols]
                    where = " OR ".join(clauses)
                    params = [f"%{escaped}%"] * len(real_cols)
                sql = (f"SELECT rowid AS _rid, * FROM {_q(tbl)} "
                       f"WHERE {where} LIMIT 1000")
                cur = self.db._conn.execute(sql, params)
                col_descs = [d[0] for d in cur.description]
                result_rows = [list(r) for r in cur.fetchall()]
                if result_rows:
                    self._display_browse_data(col_descs, result_rows)
                    return
            except Exception:
                pass  # Fall back to in-memory filter

        # Fallback: in-memory filter of loaded data
        self._apply_browse_filter_inmemory(filt, cols, rows, col_sel)

    def _apply_browse_filter_inmemory(self, filt, cols, rows, col_sel):
        """Filter browse rows in-memory by text."""
        col_idx = None
        if col_sel != "All Columns":
            try:
                col_idx = cols.index(col_sel)
            except ValueError:
                pass
        filtered = []
        for row in rows:
            if col_idx is not None:
                if col_idx < len(row) and filt in str(vb(row[col_idx])).lower():
                    filtered.append(row)
            else:
                for v in row:
                    if filt in str(vb(v)).lower():
                        filtered.append(row)
                        break
        self._display_browse_data(cols, filtered)

    def _on_browse_select(self, event):
        sel = self._browse_tree.selection()
        if not sel:
            return
        vals = self._browse_tree.item(sel[0], "values")
        cols = self._browse_cache_cols
        if not vals or not cols:
            return

        # Clear preview
        for w in self._preview_inner.winfo_children():
            w.destroy()
        self._preview_tk_imgs = []

        # Get actual data from display rows
        idx = self._browse_tree.index(sel[0])
        display_rows = getattr(self, '_browse_display_rows', self._browse_cache_data)
        if idx >= len(display_rows):
            return
        actual_row = display_rows[idx]

        for i, c in enumerate(cols):
            v = actual_row[i] if i < len(actual_row) else None
            bg = C["bg"] if i % 2 == 0 else C["alt"]
            row_f = tk.Frame(self._preview_inner, bg=bg)
            row_f.pack(fill="x", padx=2, pady=1)

            tk.Label(row_f, text=c, font=("Segoe UI", 9, "bold"),
                     fg=C["accent"], bg=bg, width=18, anchor="nw").pack(side="left", padx=4, pady=2)

            if v is None:
                tk.Label(row_f, text="NULL", fg=C["text2"], bg=bg,
                         font=("Segoe UI", 9, "italic")).pack(side="left", padx=4)
            elif isinstance(v, bytes):
                bt = blob_type(v)
                tk.Label(row_f, text=f"{bt} ({fmtb(len(v))})",
                         fg=C["orange"], bg=bg, font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)
                tk.Button(row_f, text="View BLOB", font=("Segoe UI", 7),
                          command=lambda bv=v, cn=c: BlobViewer(self, bv, cn)).pack(side="left", padx=4)
            else:
                sv = str(v)
                display = sv[:500] if len(sv) > 500 else sv
                tk.Label(row_f, text=display, fg=C["text"], bg=bg,
                         wraplength=500, justify="left", anchor="nw").pack(side="left", padx=4)
                # Timestamp
                if isinstance(v, (int, float)):
                    ts = try_decode_timestamp(v)
                    if ts:
                        for fmt_name, decoded in ts:
                            tk.Label(row_f, text=f"{fmt_name}: {decoded}",
                                     fg=C["green"], bg=bg, font=("Segoe UI", 8)).pack(side="left", padx=8)

        # Store for copy
        self._preview_row_cols = cols
        self._preview_row_data = actual_row

        # Bind scroll to all preview children
        def _bind_prev_scroll(w):
            w.bind("<MouseWheel>", lambda e: self._preview_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
            for child in w.winfo_children():
                _bind_prev_scroll(child)
        _bind_prev_scroll(self._preview_inner)

    def _preview_copy_json(self):
        """Copy the currently previewed browse row as JSON."""
        cols = getattr(self, '_preview_row_cols', None)
        data = getattr(self, '_preview_row_data', None)
        if not cols or not data:
            return
        d = {}
        for i, c in enumerate(cols):
            if c == "_rid":
                continue
            v = data[i] if i < len(data) else None
            d[c] = str(v) if isinstance(v, bytes) else v
        self.clipboard_clear()
        self.clipboard_append(json.dumps(d, indent=2, default=str))

    def _preview_copy_csv(self):
        """Copy the currently previewed browse row as CSV."""
        import io
        cols = getattr(self, '_preview_row_cols', None)
        data = getattr(self, '_preview_row_data', None)
        if not cols or not data:
            return
        real_cols = [c for c in cols if c != "_rid"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(real_cols)
        vals = []
        for i, c in enumerate(cols):
            if c == "_rid":
                continue
            v = data[i] if i < len(data) else None
            vals.append(str(v) if isinstance(v, bytes) else v)
        w.writerow(vals)
        self.clipboard_clear()
        self.clipboard_append(buf.getvalue())

    def _preview_copy_text(self):
        """Copy the currently previewed browse row as plain text."""
        cols = getattr(self, '_preview_row_cols', None)
        data = getattr(self, '_preview_row_data', None)
        if not cols or not data:
            return
        lines = []
        for i, c in enumerate(cols):
            if c == "_rid":
                continue
            v = data[i] if i < len(data) else None
            lines.append(f"{c}: {v}")
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))

    def _on_browse_dblclick(self, event):
        sel = self._browse_tree.selection()
        if not sel:
            return
        tbl = self._browse_table_var.get()
        vals = self._browse_tree.item(sel[0], "values")
        cols = self._browse_cache_cols
        if not vals or not cols or "_rid" not in cols:
            return
        rid_idx = cols.index("_rid")
        try:
            rid = int(vals[rid_idx])
        except (ValueError, TypeError, IndexError):
            return
        # WAL-only tables: use WAL row detail instead of SQL-based RowWin
        if tbl.startswith("WAL: ") and self.db.has_wal:
            real_name = tbl[5:]
            for rec in self.db.wal.recover_all_records(table_filter=real_name):
                if rec["rowid"] == rid:
                    status_map = {"committed": "In DB", "uncommitted": "WAL Only", "old": "Older Version"}
                    src = f"WAL ({status_map.get(rec['category'], rec['category'])})"
                    self._show_wal_row_detail(
                        source=src, table=real_name,
                        match_col="", rowid=rid, match_val="",
                        row_data=rec.get("values_dict", {}),
                        frame_idx=rec["frame_idx"],
                        page_num=rec["page_num"])
                    return
        else:
            RowWin.show(self, self.db, tbl, rid)

    def _browse_export_csv(self):
        tbl = self._browse_table_var.get()
        if not tbl or not self.db.ok:
            return
        is_wal = tbl.startswith("WAL: ")
        real_name = tbl[5:] if is_wal else tbl
        loaded_rows = self._browse_cache_data
        display_rows = getattr(self, '_browse_display_rows', loaded_rows)
        total_count = _int_count(self._count_cache.get(tbl, 0), len(loaded_rows))
        # Scope dialog
        dlg = tk.Toplevel(self)
        dlg.title("Export CSV")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()
        dlg.update_idletasks()
        _dw, _dh = 340, 180
        dlg.geometry(f"{_dw}x{_dh}+{self.winfo_rootx() + (self.winfo_width() - _dw) // 2}+{self.winfo_rooty() + (self.winfo_height() - _dh) // 2}")
        result = [None]
        tk.Label(dlg, text="What to export?", font=("Segoe UI", 10, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(pady=(12, 8))
        bf = tk.Frame(dlg, bg=C["bg"])
        bf.pack(fill="x", padx=20)
        btn_cfg = dict(font=("Segoe UI", 9), relief="flat", bd=0, padx=12, pady=5, cursor="hand2")
        def pick(which):
            result[0] = which
            dlg.destroy()
        tk.Button(bf, text=f"All Rows ({total_count})", bg=C["acl"], fg=C["accent"],
                  command=lambda: pick("all"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text=f"Loaded Page ({len(loaded_rows)})", bg=C["bg2"], fg=C["text"],
                  command=lambda: pick("loaded"), **btn_cfg).pack(fill="x", pady=2)
        if len(display_rows) != len(loaded_rows):
            tk.Button(bf, text=f"Filtered ({len(display_rows)})", bg="#e3fcef", fg=C["green"],
                      command=lambda: pick("filtered"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text="Cancel", bg=C["bg3"], fg=C["text2"],
                  command=dlg.destroy, **btn_cfg).pack(fill="x", pady=2)
        self.wait_window(dlg)
        if result[0] is None:
            return
        fname = real_name if is_wal else tbl
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                             initialfile=f"{fname}.csv",
                                             filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            cols = self._browse_cache_cols
            if result[0] == "all":
                if is_wal:
                    # Export all WAL records for this table
                    all_cols, all_rows, _ = self.db.wal_browse(real_name, limit=999999, offset=0)
                    cols = all_cols
                    rows = all_rows
                else:
                    # Query full table from DB
                    rows = []
                    try:
                        cur = self.db._conn.execute(f"SELECT * FROM {_q(tbl)}")
                        db_cols = [d[0] for d in cur.description]
                        cols = ["_rid"] + db_cols
                        for row in cur:
                            rows.append([row[db_cols.index(c)] if c in db_cols else "" for c in db_cols])
                    except Exception as e2:
                        messagebox.showerror("Error", f"Query failed: {e2}")
                        return
            elif result[0] == "filtered":
                rows = display_rows
            else:
                rows = loaded_rows
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for row in rows:
                    w.writerow([vb(v) for v in row])
            messagebox.showinfo("Exported", f"Exported {len(rows)} rows to:\n{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _browse_export_blobs(self):
        tbl = self._browse_table_var.get()
        if not tbl or not self.db.ok:
            return
        # Scope selection dialog
        total_rows = _int_count(self._count_cache.get(tbl, 0), len(self._browse_cache_data))
        loaded = len(self._browse_cache_data)
        dlg = tk.Toplevel(self)
        dlg.title("Export BLOBs")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()
        # Center on parent window
        dlg.update_idletasks()
        pw, ph = self.winfo_width(), self.winfo_height()
        px, py = self.winfo_rootx(), self.winfo_rooty()
        dw, dh = 340, 200
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        dlg.geometry(f"{dw}x{dh}+{x}+{y}")
        result = [None]
        tk.Label(dlg, text="Export BLOBs from:", font=("Segoe UI", 10, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(pady=(12, 8))
        bf = tk.Frame(dlg, bg=C["bg"])
        bf.pack(fill="x", padx=20)
        btn_cfg = dict(font=("Segoe UI", 9), relief="flat", bd=0, padx=12, pady=5, cursor="hand2")
        def pick(w):
            result[0] = w
            dlg.destroy()
        tk.Button(bf, text=f"All Rows in Table ({fmt_count(total_rows)})", bg=C["acl"], fg=C["accent"],
                  command=lambda: pick("all"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text=f"Loaded Page ({loaded})", bg="#e3fcef", fg=C["green"],
                  command=lambda: pick("loaded"), **btn_cfg).pack(fill="x", pady=2)
        tk.Button(bf, text="Cancel", bg=C["bg3"], fg=C["text2"],
                  command=dlg.destroy, **btn_cfg).pack(fill="x", pady=2)
        self.wait_window(dlg)
        if result[0] is None:
            return
        folder = filedialog.askdirectory(title="Select folder for BLOBs")
        if not folder:
            return
        # Find BLOB-type columns
        col_info = self.db.columns(tbl)
        blob_col_names = [c[0] for c in col_info if c[1] and "BLOB" in c[1].upper()]
        if not blob_col_names and result[0] == "all":
            # Fallback: check loaded data for bytes
            blob_col_names = []
            cols = self._browse_cache_cols
            for ri, row in enumerate(self._browse_cache_data[:5]):
                for ci, v in enumerate(row):
                    if isinstance(v, bytes) and ci < len(cols) and cols[ci] not in blob_col_names:
                        blob_col_names.append(cols[ci])
        # Progress dialog — centered on parent
        prog_dlg = tk.Toplevel(self)
        prog_dlg.title("Exporting BLOBs...")
        prog_dlg.configure(bg=C["bg"])
        prog_dlg.update_idletasks()
        pdw, pdh = 350, 100
        px2 = self.winfo_rootx() + (self.winfo_width() - pdw) // 2
        py2 = self.winfo_rooty() + (self.winfo_height() - pdh) // 2
        prog_dlg.geometry(f"{pdw}x{pdh}+{px2}+{py2}")
        prog_lbl = tk.Label(prog_dlg, text="Starting export...", bg=C["bg"], font=("Segoe UI", 9))
        prog_lbl.pack(pady=(10, 5))
        prog_bar = ttk.Progressbar(prog_dlg, mode="determinate")
        prog_bar.pack(fill="x", padx=20, pady=5)
        self.update_idletasks()
        count = 0
        errors = 0
        if result[0] == "loaded":
            # Export from loaded page data
            cols = self._browse_cache_cols
            rows = self._browse_cache_data
            prog_bar.configure(maximum=max(len(rows), 1))
            for ri, row in enumerate(rows):
                for ci, v in enumerate(row):
                    if isinstance(v, bytes) and len(v) > 0:
                        bt = blob_type(v)
                        ext = _EXT_MAP.get(bt, ".bin")
                        cn = cols[ci] if ci < len(cols) else f"col{ci}"
                        fname = f"{tbl}_r{ri}_{cn}{ext}"
                        try:
                            with open(os.path.join(folder, fname), "wb") as f:
                                f.write(v)
                            count += 1
                        except Exception:
                            errors += 1
                if ri % 50 == 0:
                    prog_bar.configure(value=ri + 1)
                    prog_lbl.configure(text=f"Exported {count} BLOBs ({ri+1}/{len(rows)} rows)")
                    self.update_idletasks()
        else:
            # Export ALL rows from table — query in batches
            batch_size = 500
            offset = 0
            total = total_rows if isinstance(total_rows, int) else 10000
            prog_bar.configure(maximum=max(total, 1))
            while True:
                try:
                    sql = f"SELECT rowid, * FROM {_q(tbl)} LIMIT {batch_size} OFFSET {offset}"
                    cur = self.db._conn.execute(sql)
                    col_descs = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                except Exception:
                    break
                if not rows:
                    break
                for row in rows:
                    rid = row[0]
                    for ci in range(1, len(row)):
                        v = row[ci]
                        if isinstance(v, bytes) and len(v) > 0:
                            bt = blob_type(v)
                            ext = _EXT_MAP.get(bt, ".bin")
                            cn = col_descs[ci] if ci < len(col_descs) else f"col{ci}"
                            fname = f"{tbl}_r{rid}_{cn}{ext}"
                            try:
                                with open(os.path.join(folder, fname), "wb") as f:
                                    f.write(v)
                                count += 1
                            except Exception:
                                errors += 1
                offset += batch_size
                prog_bar.configure(value=min(offset, total))
                prog_lbl.configure(text=f"Exported {count} BLOBs ({offset}/{fmt_count(total)} rows)")
                self.update_idletasks()
        prog_dlg.destroy()
        msg = f"Exported {count} BLOB(s) to:\n{folder}"
        if errors:
            msg += f"\n({errors} error(s))"
        messagebox.showinfo("Export Complete", msg)

    # ── Key bindings ─────────────────────────────────────────────────
    def _toggle_sidebar(self):
        if self._sidebar_visible:
            self._sidebar.pack_forget()
            self._sb_toggle.config(text="\u25B6")
            self._sidebar_visible = False
        else:
            self._sidebar.pack(side="left", fill="y", before=self._sb_toggle)
            self._sb_toggle.config(text="\u25C0")
            self._sidebar_visible = True

    def _bind_keys(self):
        self.bind("<Control-o>", lambda e: self._open_file())
        self.bind("<Control-f>", lambda e: self._focus_search())
        self.bind("<Escape>", lambda e: self._stop_search())
        self._search_entry.bind("<Return>", lambda e: self._do_search())

    def _setup_tooltips(self):
        """Add tooltips to all interactive widgets."""
        # Search tab
        ToolTip(self._search_entry, "Type your search term and press Enter")
        ToolTip(self._mode_combo, "Choose how to match: case-insensitive, exact, regex, etc.")
        ToolTip(self._deep_blob_cb, "Include BLOB columns in text search (slower)")
        ToolTip(self._search_wal_cb,
               "Search the WAL (Write-Ahead Log) file for hidden data:\n"
               "  - WAL-only drafts and crashed transactions\n"
               "  - Deleted or edited records (older versions)\n"
               "  - Data that normal tools cannot see\n"
               "Results appear color-coded: green=in DB, orange=WAL-only, red=older version")
        ToolTip(self._search_btn, "Start searching across all tables (Enter)")
        ToolTip(self._stop_btn, "Cancel the running search (Escape)")
        ToolTip(self._scope_btn, "Select which tables to include in the search")
        ToolTip(self._reset_scope_btn, "Reset scope to include all tables")
        ToolTip(self._sr_table_filter, "Filter results by table (shows result count per table)")
        ToolTip(self._sr_col_filter, "Filter results by column name")
        ToolTip(self._search_tree, "Double-click or press Enter to open row detail")
        # Browse tab
        ToolTip(self._browse_table_combo, "Select a table to browse")
        ToolTip(self._browse_filter_entry, "Type to filter rows in real-time")
        ToolTip(self._browse_col_combo, "Filter by a specific column or all columns")
        ToolTip(self._browse_tree, "Double-click or press Enter to open row detail")
        # Sidebar
        ToolTip(self._schema_tree, "Expand tables to see columns, types, and indexes.\nRight-click for more options.")
        ToolTip(self._sb_toggle, "Toggle schema sidebar")

    def _focus_search(self):
        self._nb.select(self._search_frame)
        self._search_entry.focus_set()
        self._search_entry.select_range(0, "end")

    # ── DB operations ────────────────────────────────────────────────
    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Open SQLite Database",
            filetypes=[("SQLite files", "*.db *.sqlite *.sqlite3 *.db3"),
                       ("All files", "*.*")]
        )
        if path:
            self._open_db(path)

    def _open_db(self, path):
        # If a DB with WAL is already open, confirm before switching
        if self.db.ok and self.db.has_wal:
            bak = self.db.wal_backup_path
            msg = "Close the current database and open a new one?"
            if bak:
                msg += f"\n\nWAL data backed up at:\n{os.path.basename(bak)}"
            if not messagebox.askokcancel("Open Database", msg):
                return
        t0 = time.time()
        try:
            self.db.open(path)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot open database:\n{e}")
            return
        self._load_time = time.time() - t0

        tables = self.db.tables()
        self._scope_tables = list(tables)
        self._count_cache = {t: "?" for t in tables}

        # Update UI
        fname = os.path.basename(path)
        fsize = fmtb(os.path.getsize(path)) if os.path.exists(path) else "?"
        self._db_info.configure(
            text=f"  {fname}  |  {fsize}  |  {len(tables)} tables  |  loaded in {self._load_time:.2f}s")

        # Build browse combo: main tables + WAL-only tables
        browse_vals = list(tables)
        if self.db.has_wal:
            wal_only = self.db.wal_tables()
            if wal_only:
                browse_vals.append("---WAL-Only Tables---")
                for wt in wal_only:
                    browse_vals.append(f"WAL: {wt}")
        self._browse_table_combo.configure(values=browse_vals)
        if tables:
            self._browse_table_var.set(tables[0])
            self._load_browse_table()

        self._populate_schema()

        # WAL tab & checkbox: add/remove based on whether WAL file exists
        if self.db.has_wal:
            if not self._wal_tab_added:
                self._nb.add(self._wal_frame, text="  Hidden Data (WAL)  ")
                self._wal_tab_added = True
                self._build_wal_tab()
            self._populate_wal_tab()
            # Show WAL checkbox (insert before Max/table label to keep order)
            self._search_wal_cb.pack(side="left", padx=4,
                                     before=self._search_max_lbl)
        else:
            if self._wal_tab_added:
                self._nb.forget(self._wal_frame)
                self._wal_tab_added = False
            self._search_wal_cb.pack_forget()
            self._search_wal_var.set(False)

        # Background count using SEPARATE connection (non-blocking)
        self._count_cancel = False
        self._bg_count_thread = threading.Thread(
            target=self._bg_count, args=(list(tables), self.db._path), daemon=True)
        self._bg_count_thread.start()

    def _bg_count(self, tables, db_path):
        """Count rows using a separate connection so search is never blocked.

        Two-pass approach for speed:
          Pass 1: max(rowid) — O(1), instant approximate counts (~0.01s total)
          Pass 2: SELECT COUNT(*) — exact counts (can take 10-15s on large DBs)
        Approximate counts show with '~' prefix until exact count arrives.
        """
        try:
            uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("PRAGMA cache_size = -2000")
            conn.execute("PRAGMA mmap_size = 67108864")
            conn.execute("PRAGMA query_only = ON")
        except Exception:
            return
        # Pass 1: fast approximate counts via max(rowid) — nearly instant
        for t in tables:
            if self._count_cancel:
                break
            try:
                r = conn.execute(f"SELECT max(rowid) FROM {_q(t)}").fetchone()
                approx = r[0] if r and r[0] is not None else 0
                self._count_cache[t] = f"~{approx}"  # ~ prefix = approximate
            except Exception:
                pass  # stays as "?", will be resolved in pass 2
        if not self._count_cancel:
            self.after(0, self._update_schema_counts)
        # Pass 2: exact counts — replaces approximations
        last_update = 0
        for t in tables:
            if self._count_cancel:
                break
            try:
                r = conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()
                self._count_cache[t] = r[0] if r else 0
            except Exception:
                if not isinstance(self._count_cache.get(t), int):
                    self._count_cache[t] = "?"
            now = time.time()
            if now - last_update >= 0.3:
                last_update = now
                self.after(0, self._update_schema_counts)
        try:
            conn.close()
        except Exception:
            pass
        if not self._count_cancel:
            self.after(0, self._update_schema_counts)

    def _update_schema_counts(self):
        tree = self._schema_tree
        for iid in tree.get_children():
            text = tree.item(iid, "text")
            if text == "Tables":
                for child in tree.get_children(iid):
                    vals = tree.item(child, "values")
                    if vals and len(vals) >= 1:
                        tbl = vals[0]
                        cnt = self._count_cache.get(tbl, "?")
                        tree.item(child, text=f"{tbl}  ({fmt_count(cnt)})")

    def _on_app_close(self):
        """Handle window close — confirm if DB is open with WAL data."""
        if self.db.ok and self.db.has_wal:
            bak = self.db.wal_backup_path
            msg = "Close the application?"
            if bak:
                msg += f"\n\nWAL backup preserved at:\n{os.path.basename(bak)}"
            if not messagebox.askokcancel("Close", msg):
                return
        self.db.close()
        self.destroy()

    def _close_db(self, confirm=True):
        if confirm and self.db.ok and self.db.has_wal:
            bak = self.db.wal_backup_path
            msg = "Close the current database?"
            if bak:
                msg += f"\n\nWAL data has been backed up to:\n{os.path.basename(bak)}\n(next to your database file)"
            if not messagebox.askokcancel("Close Database", msg):
                return
        self._count_cancel = True
        self.db.close()
        self._count_cache = {}
        self._scope_tables = []
        self._search_results = []
        self._search_errors = []
        self._browse_cache_data = []
        self._browse_cache_cols = []
        self._browse_display_rows = []
        # Clear per-column filters
        for w in self._col_filter_frame.winfo_children():
            w.destroy()
        self._col_filters = {}
        self._col_filter_timer = None
        self._search_tree.delete(*self._search_tree.get_children())
        self._browse_tree.delete(*self._browse_tree.get_children())
        self._schema_tree.delete(*self._schema_tree.get_children())
        self._browse_table_combo.configure(values=[])
        self._browse_table_var.set("")
        self._db_info.configure(text="  No database loaded")
        self._sql_preview.configure(state="normal")
        self._sql_preview.delete("1.0", "end")
        self._sql_preview.configure(state="disabled")
        for w in self._preview_inner.winfo_children():
            w.destroy()
        # Remove WAL tab if present
        if self._wal_tab_added:
            try:
                self._nb.forget(self._wal_frame)
            except Exception:
                pass
            self._wal_tab_added = False
            # Clear WAL tab contents
            for w in self._wal_frame.winfo_children():
                w.destroy()

    def _show_info(self):
        if not self.db.ok:
            messagebox.showinfo("Info", "No database loaded")
            return
        m = self.db.meta()
        total_rows = sum(_int_count(v) for v in self._count_cache.values())
        info = (
            f"Path: {m.get('path', '')}\n"
            f"Size: {fmtb(m.get('size', 0))}\n"
            f"Page size: {m.get('page_size', '')}\n"
            f"Page count: {m.get('page_count', '')}\n"
            f"Journal mode: {m.get('journal_mode', '')}\n"
            f"Encoding: {m.get('encoding', '')}\n"
            f"Auto vacuum: {m.get('auto_vacuum', '')}\n"
            f"User version: {m.get('user_version', '')}\n"
            f"Freelist count: {m.get('freelist_count', '')}\n"
            f"Total rows: {total_rows:,}\n"
            f"Tables: {len(self.db.tables())}\n"
            f"Views: {len(self.db.views())}\n"
            f"Indexes: {len(self.db.all_indexes())}\n"
            f"Triggers: {len(self.db.triggers())}"
        )
        # Add WAL info if available
        if self.db.has_wal:
            ws = self.db.wal.summary()
            info += (
                f"\n\nWAL File:\n"
                f"WAL size: {fmtb(ws.get('wal_size', 0))}\n"
                f"WAL frames: {ws.get('total_frames', 0)}\n"
                f"  Committed: {ws.get('committed', 0)}\n"
                f"  Uncommitted: {ws.get('uncommitted', 0)}\n"
                f"  Old/Pre-checkpoint: {ws.get('old', 0)}\n"
                f"Unique pages: {ws.get('unique_pages', 0)}"
            )
        messagebox.showinfo("Database Info", info)

    # ── WAL Tab ───────────────────────────────────────────────────────
    def _build_wal_tab(self):
        """Build the WAL forensic analysis tab with clear explanations."""
        wf = self._wal_frame

        # Clear any existing children (rebuild on new DB)
        for w in wf.winfo_children():
            w.destroy()

        # ── Header title ──
        ttk.Label(wf, text="Hidden Data (WAL)", style="B.TLabel").pack(fill="x", padx=10, pady=(8, 2))

        # ── Collapsible Summary stats panel ──
        self._wal_stats_frame = tk.Frame(wf, bg="#f5f0ff", relief="groove", bd=1)
        self._wal_stats_frame.pack(fill="x", padx=10, pady=(2, 2))
        self._wal_stats_expanded = False
        self._wal_stats_toggle = tk.Button(
            self._wal_stats_frame, text="\u25b6 Summary",
            font=("Segoe UI", 10, "bold"), bg="#f5f0ff", fg=C["purple"],
            anchor="w", relief="flat", bd=0, cursor="hand2",
            command=self._toggle_wal_stats)
        self._wal_stats_toggle.pack(fill="x", padx=10, pady=(4, 0))
        self._wal_stats_content = ttk.Frame(self._wal_stats_frame)
        # Hidden by default (collapsed)

        # ── Summary bar ──
        self._wal_summary = ttk.Label(wf, text="", style="B.TLabel")
        self._wal_summary.pack(fill="x", padx=10, pady=(4, 2))

        # ── Filter bar (Row 1: view toggle + filters) ──
        fbar = ttk.Frame(wf)
        fbar.pack(fill="x", padx=10, pady=(2, 0))

        # View toggle buttons
        self._wal_view_mode = "frames"
        self._wal_frame_view_btn = tk.Button(
            fbar, text="Frame View", font=("Segoe UI", 8, "bold"),
            relief="sunken", bd=1, bg=C["acl"], fg=C["accent"],
            cursor="hand2", padx=6, pady=1,
            command=lambda: self._switch_wal_view("frames"))
        self._wal_frame_view_btn.pack(side="left", padx=(0, 1))
        self._wal_all_rec_btn = tk.Button(
            fbar, text="All Records", font=("Segoe UI", 8),
            relief="raised", bd=1, bg=C["bg2"], fg=C["text2"],
            cursor="hand2", padx=6, pady=1,
            command=lambda: self._switch_wal_view("records"))
        self._wal_all_rec_btn.pack(side="left", padx=(1, 8))

        ttk.Label(fbar, text="Status:", font=("Segoe UI", 9)).pack(side="left")
        self._wal_cat_var = tk.StringVar(value="All")
        cat_combo = ttk.Combobox(fbar, textvariable=self._wal_cat_var,
                                  values=["All", "committed", "uncommitted", "old"],
                                  state="readonly", width=14)
        cat_combo.pack(side="left", padx=(2, 8))
        cat_combo.bind("<<ComboboxSelected>>", lambda e: self._filter_wal_frames())
        ToolTip(cat_combo, "Filter frames by status:\n"
                "  In DB = committed and written to DB\n"
                "  WAL Only = not yet in main DB\n"
                "  Older Version = replaced by newer data")

        ttk.Label(fbar, text="Table:", font=("Segoe UI", 9)).pack(side="left")
        self._wal_table_var = tk.StringVar(value="All")
        self._wal_table_combo = ttk.Combobox(fbar, textvariable=self._wal_table_var,
                                              values=["All"], state="readonly", width=22)
        self._wal_table_combo.pack(side="left", padx=(2, 8))
        self._wal_table_combo.bind("<<ComboboxSelected>>", lambda e: self._filter_wal_frames())
        ToolTip(self._wal_table_combo, "Filter frames by table name (type to search)")

        ttk.Label(fbar, text="Data Type:", font=("Segoe UI", 9)).pack(side="left")
        self._wal_pt_var = tk.StringVar(value="All")
        self._wal_pt_combo = ttk.Combobox(fbar, textvariable=self._wal_pt_var,
                                           values=["All"], state="readonly", width=14)
        self._wal_pt_combo.pack(side="left", padx=(2, 8))
        self._wal_pt_combo.bind("<<ComboboxSelected>>", lambda e: self._filter_wal_frames())
        ToolTip(self._wal_pt_combo, "Filter by page type:\n"
                "  Table Leaf = contains actual row data\n"
                "  Table Interior = internal tree structure\n"
                "  Index Leaf/Interior = index data")

        ttk.Label(fbar, text="Page#:", font=("Segoe UI", 9)).pack(side="left")
        self._wal_page_var = tk.StringVar()
        wal_page_entry = ttk.Entry(fbar, textvariable=self._wal_page_var, width=6)
        wal_page_entry.pack(side="left", padx=(2, 4))
        self._wal_page_var.trace_add("write", lambda *a: self._filter_wal_frames())
        ToolTip(wal_page_entry, "Filter to a specific database page number")

        # ── Action bar (Row 2: actions + exports + details) ──
        abar = ttk.Frame(wf)
        abar.pack(fill="x", padx=10, pady=(1, 2))

        ttk.Button(abar, text="Refresh", command=self._populate_wal_tab).pack(side="left", padx=2)
        ttk.Button(abar, text="Export CSV", command=self._export_wal_summary).pack(side="left", padx=2)
        ttk.Button(abar, text="Export All Records", command=self._export_wal_all_records).pack(side="left", padx=2)
        ttk.Button(abar, text="Export BLOBs", command=self._export_wal_blobs).pack(side="left", padx=2)
        # Details button — always visible on right of action bar
        details_btn = ttk.Button(abar, text="Technical Details", command=self._show_wal_header)
        details_btn.pack(side="right", padx=2)
        ToolTip(details_btn, "Show the raw WAL file header and technical metadata")

        # ── PanedWindow: frame list (top) + detail (bottom) ──
        self._wal_pw = ttk.PanedWindow(wf, orient="vertical")
        self._wal_pw.pack(fill="both", expand=True, padx=10, pady=(2, 10))

        # Frame list treeview
        top_frame = ttk.Frame(self._wal_pw)
        self._wal_pw.add(top_frame, weight=3)

        border = tk.Frame(top_frame, relief="solid", bd=1, bg=C["border"])
        border.pack(fill="both", expand=True)

        wal_cols = ("Table", "Status", "Data Type", "Records")
        self._wal_tree = ttk.Treeview(border, columns=wal_cols, show="headings", selectmode="browse")
        for c in wal_cols:
            self._wal_tree.heading(c, text=c, command=lambda col=c: self._sort_wal_tree(col))
        self._wal_tree.column("Table", width=280, minwidth=150, stretch=True)
        self._wal_tree.column("Status", width=100, minwidth=80, stretch=False)
        self._wal_tree.column("Data Type", width=120, minwidth=90, stretch=False)
        self._wal_tree.column("Records", width=70, minwidth=50, stretch=False)

        ysb = ttk.Scrollbar(border, orient="vertical", command=self._wal_tree.yview)
        self._wal_tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self._wal_tree.pack(fill="both", expand=True)
        self._wal_tree.bind("<<TreeviewSelect>>", self._on_wal_select)

        # Detail panel (bottom)
        detail_frame = ttk.Frame(self._wal_pw)
        self._wal_pw.add(detail_frame, weight=2)

        # Detail notebook: Summary | Recovered Data | Raw Hex (3 tabs only)
        self._wal_detail_nb = ttk.Notebook(detail_frame)
        self._wal_detail_nb.pack(fill="both", expand=True)

        # Summary tab (replaces "Page Info")
        info_frame = ttk.Frame(self._wal_detail_nb)
        self._wal_detail_nb.add(info_frame, text=" Summary ")
        self._wal_page_info = tk.Text(info_frame, wrap="word", height=8,
                                       bg=C["bg2"], fg=C["text"], font=("Consolas", 10))
        self._wal_page_info.pack(fill="both", expand=True)
        self._wal_page_info.configure(state="disabled")

        # Recovered Data tab (replaces "Parsed Records")
        rec_frame = ttk.Frame(self._wal_detail_nb)
        self._wal_detail_nb.add(rec_frame, text=" Recovered Data ")
        self._wal_rec_border = tk.Frame(rec_frame, relief="solid", bd=1, bg=C["border"])
        self._wal_rec_border.pack(fill="both", expand=True)

        # Raw Hex tab (replaces "Hex View")
        hex_frame = ttk.Frame(self._wal_detail_nb)
        self._wal_detail_nb.add(hex_frame, text=" Raw Hex ")
        hex_btn_bar = ttk.Frame(hex_frame)
        hex_btn_bar.pack(fill="x")
        ttk.Button(hex_btn_bar, text="Copy Raw Hex", command=self._copy_wal_hex_raw).pack(side="left", padx=4, pady=2)
        ttk.Button(hex_btn_bar, text="Copy Formatted", command=self._copy_wal_hex).pack(side="left", padx=4, pady=2)
        ttk.Button(hex_btn_bar, text="Copy Base64", command=self._copy_wal_hex_b64).pack(side="left", padx=4, pady=2)
        self._wal_hex_view = tk.Text(hex_frame, wrap="none", height=8,
                                      bg=C["bg2"], fg=C["text"], font=("Consolas", 10))
        hex_sb = ttk.Scrollbar(hex_frame, orient="vertical", command=self._wal_hex_view.yview)
        hex_xsb = ttk.Scrollbar(hex_frame, orient="horizontal", command=self._wal_hex_view.xview)
        self._wal_hex_view.configure(yscrollcommand=hex_sb.set, xscrollcommand=hex_xsb.set)
        hex_sb.pack(side="right", fill="y")
        hex_xsb.pack(side="bottom", fill="x")
        self._wal_hex_view.pack(fill="both", expand=True)
        self._wal_hex_view.configure(state="disabled")

        # ── All Records view (standalone frame, hidden by default) ──
        self._wal_ar_frame = ttk.Frame(wf)
        # Not packed — shown only when "All Records" view is active

        ar_top = ttk.Frame(self._wal_ar_frame)
        ar_top.pack(fill="x", padx=4, pady=4)

        ttk.Button(ar_top, text="Load", command=self._load_all_wal_records).pack(side="left", padx=4)

        # Show filter: All / Different from DB / WAL Only (not in DB)
        tk.Frame(ar_top, width=8).pack(side="left")
        ttk.Label(ar_top, text="Show:", font=("Segoe UI", 8)).pack(side="left", padx=(0, 2))
        self._ar_show_var = tk.StringVar(value="All")
        ar_show_combo = ttk.Combobox(ar_top, textvariable=self._ar_show_var,
                                      values=["All", "Different from DB",
                                              "WAL Only (not in DB)",
                                              "★ WAL-Only Tables",
                                              "Same as DB"],
                                      state="readonly", width=20, font=("Segoe UI", 8))
        ar_show_combo.pack(side="left", padx=2)
        ar_show_combo.bind("<<ComboboxSelected>>",
                           lambda e: self._apply_ar_show_filter())

        # Pagination
        tk.Frame(ar_top, width=12).pack(side="left")
        ttk.Button(ar_top, text="\u25c0", command=self._ar_prev_page, width=3).pack(side="left", padx=1)
        ttk.Button(ar_top, text="\u25b6", command=self._ar_next_page, width=3).pack(side="left", padx=1)
        self._ar_page_label = ttk.Label(ar_top, text="", font=("Segoe UI", 8))
        self._ar_page_label.pack(side="left", padx=6)

        # Copy buttons for All Records view
        tk.Frame(ar_top, width=8).pack(side="left")
        self._ar_copy_json_btn = ttk.Button(ar_top, text="Copy Row JSON",
            command=self._ar_copy_row_json)
        self._ar_copy_json_btn.pack(side="left", padx=2)
        self._ar_copy_csv_btn = ttk.Button(ar_top, text="Copy Row CSV",
            command=self._ar_copy_row_csv)
        self._ar_copy_csv_btn.pack(side="left", padx=2)

        self._ar_border = tk.Frame(self._wal_ar_frame, relief="solid", bd=1, bg=C["border"])
        self._ar_border.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # Placeholder label
        ttk.Label(self._ar_border,
                  text="Click 'Load' to recover all records from the WAL file.",
                  style="M.TLabel").pack(padx=10, pady=10)

        self._ar_page = 0
        self._ar_page_size = 200
        self._ar_records = []
        self._ar_records_all = []  # Unfiltered master list with diff status

        # Store sorted state
        self._wal_sort_col = "Table"
        self._wal_sort_reverse = False
        self._wal_filtered_frames = []

    def _toggle_wal_stats(self):
        """Toggle visibility of the WAL summary stats panel."""
        if self._wal_stats_expanded:
            self._wal_stats_content.pack_forget()
            self._wal_stats_expanded = False
            # Restore collapsed label (with counts if available)
            lbl = self._wal_stats_toggle.cget("text")
            self._wal_stats_toggle.configure(
                text=lbl.replace("\u25bc", "\u25b6"))
        else:
            self._wal_stats_content.pack(fill="x", padx=10, pady=(2, 6))
            self._wal_stats_expanded = True
            lbl = self._wal_stats_toggle.cget("text")
            self._wal_stats_toggle.configure(
                text=lbl.replace("\u25b6", "\u25bc"))

    def _switch_wal_view(self, mode):
        """Switch between Frame View and All Records view in the WAL tab."""
        if mode == self._wal_view_mode:
            return
        self._wal_view_mode = mode
        if mode == "records":
            # Hide the PanedWindow, show All Records frame
            self._wal_pw.pack_forget()
            self._wal_ar_frame.pack(fill="both", expand=True, padx=10, pady=(2, 10))
            # Update button styles
            self._wal_frame_view_btn.configure(
                relief="raised", bg=C["bg2"], fg=C["text2"],
                font=("Segoe UI", 8))
            self._wal_all_rec_btn.configure(
                relief="sunken", bg=C["acl"], fg=C["accent"],
                font=("Segoe UI", 8, "bold"))
        else:
            # Hide All Records, show PanedWindow
            self._wal_ar_frame.pack_forget()
            self._wal_pw.pack(fill="both", expand=True, padx=10, pady=(2, 10))
            # Update button styles
            self._wal_frame_view_btn.configure(
                relief="sunken", bg=C["acl"], fg=C["accent"],
                font=("Segoe UI", 8, "bold"))
            self._wal_all_rec_btn.configure(
                relief="raised", bg=C["bg2"], fg=C["text2"],
                font=("Segoe UI", 8))

    def _populate_wal_tab(self):
        """Populate the WAL tab with frame data from the parser."""
        if not self.db.has_wal:
            return

        ws = self.db.wal.summary()

        # Human-readable summary
        parts = [f"{ws['total_frames']} frames found in WAL file"]
        if ws['committed']:
            parts.append(f"{ws['committed']} in DB")
        if ws['uncommitted']:
            parts.append(f"{ws['uncommitted']} WAL-only")
        if ws['old']:
            parts.append(f"{ws['old']} older versions")
        parts.append(f"WAL size: {fmtb(ws['wal_size'])}")
        parts.append(f"Page size: {ws['page_size']} bytes")
        self._wal_summary.configure(text="  |  ".join(parts))

        # Update page type filter values
        pt_types = sorted(ws.get("page_types", {}).keys())
        self._wal_pt_combo.configure(values=["All"] + pt_types)

        # Update table name filter values from page_map
        page_map = getattr(self.db.wal, 'page_map', {})
        # Get unique table names that appear in WAL frames
        # Filter out: system tables, unmapped pages (page_XXXX)
        _skip_tables = {"sqlite_master", "sqlite_sequence"}
        wal_tables_set = set()
        for f in self.db.wal.frames:
            tbl = page_map.get(f.page_num)
            if tbl and tbl not in _skip_tables and not tbl.startswith("page_"):
                wal_tables_set.add(tbl)
        # Mark WAL-only tables with ★ prefix in dropdown
        try:
            wal_only_set = set(self.db.wal_tables())
        except Exception:
            wal_only_set = set()
        wal_table_list = []
        for t in sorted(wal_tables_set):
            if t in wal_only_set:
                wal_table_list.append(f"★ {t}  (WAL-only)")
            else:
                wal_table_list.append(t)
        self._wal_table_combo.configure(values=["All"] + wal_table_list)

        # Store all frames and display
        self._wal_all_frames = list(self.db.wal.frames)
        self._wal_filtered_frames = list(self._wal_all_frames)
        self._display_wal_frames()

        # Populate forensic summary stats
        self._populate_wal_stats()

    def _populate_wal_stats(self):
        """Populate the Summary panel with per-table WAL statistics."""
        # Clear previous content
        for w in self._wal_stats_content.winfo_children():
            w.destroy()

        if not self.db.has_wal:
            return

        try:
            stats = self.db.wal.table_stats()
        except Exception:
            stats = {}
        if not stats:
            ttk.Label(self._wal_stats_content,
                      text="No table leaf data found in WAL.").pack(anchor="w")
            return

        # Identify WAL-only tables
        try:
            wal_only_tables = set(self.db.wal_tables())
        except Exception:
            wal_only_tables = set()

        # Summary line
        total_recs = sum(s["total_records"] for s in stats.values())
        total_saved = sum(s["committed"] for s in stats.values())
        total_unsaved = sum(s["uncommitted"] for s in stats.values())
        total_old = sum(s["old"] for s in stats.values())
        total_frames = sum(s["frames"] for s in stats.values())

        # Update toggle label with key numbers
        arrow = "\u25bc" if self._wal_stats_expanded else "\u25b6"
        toggle_text = (f"{arrow} Summary \u2014 {total_recs} records across "
                       f"{len(stats)} tables | {total_unsaved} WAL-only")
        self._wal_stats_toggle.configure(text=toggle_text)

        summary_text = (
            f"Total: {total_recs} records across {len(stats)} tables  |  "
            f"In DB: {total_saved}  |  WAL Only: {total_unsaved}  |  "
            f"Older: {total_old}  |  Frames: {total_frames}")
        tk.Label(self._wal_stats_content, text=summary_text,
                 font=("Segoe UI", 9), bg="#f5f0ff", fg=C["text"],
                 anchor="w").pack(fill="x", pady=(0, 4))

        # Per-table treeview with scrollbar
        stat_cols = ("Table", "Records", "In DB", "WAL Only", "Older",
                     "Frames", "Pages", "Notes")
        tree_frame = ttk.Frame(self._wal_stats_content)
        tree_frame.pack(fill="x")
        stat_tree = ttk.Treeview(tree_frame, columns=stat_cols,
                                  show="headings", height=min(len(stats) + 1, 10))
        for c in stat_cols:
            stat_tree.heading(c, text=c)
        stat_tree.column("Table", width=180, minwidth=120, stretch=True)
        stat_tree.column("Records", width=70, minwidth=50, stretch=False)
        stat_tree.column("In DB", width=60, minwidth=45, stretch=False)
        stat_tree.column("WAL Only", width=75, minwidth=55, stretch=False)
        stat_tree.column("Older", width=60, minwidth=45, stretch=False)
        stat_tree.column("Frames", width=60, minwidth=45, stretch=False)
        stat_tree.column("Pages", width=60, minwidth=45, stretch=False)
        stat_tree.column("Notes", width=160, minwidth=100, stretch=True)
        # Scrollbar — needed when table count exceeds visible height
        stat_sb = ttk.Scrollbar(tree_frame, orient="vertical",
                                command=stat_tree.yview)
        stat_tree.configure(yscrollcommand=stat_sb.set)
        stat_sb.pack(side="right", fill="y")
        stat_tree.pack(side="left", fill="x", expand=True)

        for i, (tbl_name, s) in enumerate(sorted(stats.items())):
            notes = []
            if tbl_name in wal_only_tables:
                notes.append("WAL-only")
            if s["uncommitted"] > 0:
                notes.append("has WAL-only data")
            if s["old"] > 0:
                notes.append("has older versions")
            tag = "odd" if i % 2 else "even"
            stat_tree.insert("", "end", values=(
                tbl_name, s["total_records"], s["committed"],
                s["uncommitted"], s["old"], s["frames"],
                len(s["pages"]), "; ".join(notes)
            ), tags=(tag,))
        stat_tree.tag_configure("odd", background=C["alt"])
        stat_tree.tag_configure("even", background=C["bg"])

    def _load_all_wal_records(self):
        """Load all WAL records using main filter bar's Table/Status values.

        Pre-computes diff status for each record by comparing against the
        main database.  Stores ``_diff_status`` in each record dict:
            "same"       – values identical to current DB row
            "different"  – row exists in DB but some columns differ
            "not_in_db"  – row not found in the main DB at all
            "wal_table"  – entire table exists only in WAL (no DB table)
        Also stores ``_diff_cols`` (set of column names that differ).
        """
        if not self.db.has_wal:
            return
        tbl_filter = self._wal_table_var.get()
        status_filter = self._wal_cat_var.get()
        # Strip ★ prefix and "(WAL-only)" suffix from WAL-only table names
        tf_raw = tbl_filter
        if tf_raw.startswith("★ "):
            tf_raw = tf_raw[2:].split("  (WAL-only)")[0].strip()
        tf = None if tbl_filter == "All" else tf_raw
        sf = None if status_filter == "All" else status_filter
        records = list(
            self.db.wal.recover_all_records(table_filter=tf,
                                             category_filter=sf))

        # Pre-compute diff status by comparing each record to main DB
        db_tables = set(self.db.tables())
        try:
            wal_only_tables = set(self.db.wal_tables())
        except Exception:
            wal_only_tables = set()
        # Cache full_row lookups: (table, rowid) -> db_row_dict
        _row_cache = {}
        for rec in records:
            tbl = rec["table"]
            rid = rec["rowid"]
            vals = rec.get("values_dict", {})

            if tbl not in db_tables:
                # Distinguish WAL-only tables from individual missing rows
                rec["_diff_status"] = "wal_table" if tbl in wal_only_tables else "not_in_db"
                rec["_diff_cols"] = set(vals.keys())
                continue

            cache_key = (tbl, rid)
            if cache_key not in _row_cache:
                db_row, _ = self.db.full_row(tbl, rid)
                _row_cache[cache_key] = db_row
            db_row = _row_cache[cache_key]

            if not db_row:
                rec["_diff_status"] = "not_in_db"
                rec["_diff_cols"] = set(vals.keys())
                continue

            # Compare column values
            diff_cols = set()
            for col_name, wal_val in vals.items():
                if col_name in db_row:
                    db_v = db_row[col_name]
                    # Normalize for comparison
                    db_str = "NULL" if db_v is None else str(db_v)
                    wal_str = "NULL" if wal_val is None else str(wal_val)
                    if db_str != wal_str:
                        diff_cols.add(col_name)
                else:
                    # Column not in DB row — treat as different
                    diff_cols.add(col_name)

            rec["_diff_cols"] = diff_cols
            rec["_diff_status"] = "different" if diff_cols else "same"

        self._ar_records_all = records  # Unfiltered master list
        self._ar_page = 0
        self._apply_ar_show_filter()

    def _apply_ar_show_filter(self):
        """Apply the Show filter (All / Different / WAL Only / WAL-Only Tables / Same)."""
        show = self._ar_show_var.get()
        records = getattr(self, '_ar_records_all', [])
        if show == "Different from DB":
            self._ar_records = [r for r in records if r.get("_diff_status") == "different"]
        elif show == "WAL Only (not in DB)":
            # Include both individual missing rows AND WAL-only table rows
            self._ar_records = [r for r in records
                                if r.get("_diff_status") in ("not_in_db", "wal_table")]
        elif show == "★ WAL-Only Tables":
            # Only records from tables that exist ONLY in WAL
            self._ar_records = [r for r in records if r.get("_diff_status") == "wal_table"]
        elif show == "Same as DB":
            self._ar_records = [r for r in records if r.get("_diff_status") == "same"]
        else:
            self._ar_records = list(records)
        self._ar_page = 0
        self._display_all_wal_records()

    def _display_all_wal_records(self):
        """Display a page of all WAL records with diff indicators."""
        for w in self._ar_border.winfo_children():
            w.destroy()

        total = len(self._ar_records)
        if total == 0:
            show = self._ar_show_var.get()
            if show != "All":
                msg = f"No records matching '{show}' with current filters."
            else:
                msg = "No records found matching the current filters."
            ttk.Label(self._ar_border,
                      text=msg,
                      style="M.TLabel").pack(padx=10, pady=10)
            self._ar_page_label.configure(text="0 records")
            return

        start = self._ar_page * self._ar_page_size
        end = start + self._ar_page_size
        page_data = self._ar_records[start:end]
        total_pages = max(1, (total + self._ar_page_size - 1) // self._ar_page_size)
        self._ar_page_label.configure(
            text=f"Page {self._ar_page + 1}/{total_pages}  ({total} records)")

        status_map = {"committed": "In DB", "uncommitted": "WAL Only",
                      "old": "Older Version"}

        # Diff status display labels
        diff_labels = {
            "same": "\u2713",        # ✓ checkmark
            "different": "\u2260",   # ≠ not-equal
            "not_in_db": "\u2205",   # ∅ empty set (WAL only row)
            "wal_table": "\u2605",   # ★ entire table WAL-only (NEW table)
        }

        # Determine data columns from first record
        if page_data:
            sample_cols = list(page_data[0]["values_dict"].keys())
        else:
            sample_cols = []

        # Column layout: Diff | RowID | Table | Status | data columns...
        ar_cols = ["Diff", "RowID", "Table", "Status"] + sample_cols

        ar_tree = ttk.Treeview(self._ar_border, columns=ar_cols,
                                show="headings", selectmode="browse")

        # Compute sensible column widths for data columns
        n_data = len(sample_cols)
        if n_data <= 5:
            data_w = 200
        elif n_data <= 10:
            data_w = 160
        elif n_data <= 20:
            data_w = 140
        else:
            data_w = 120

        for c in ar_cols:
            ar_tree.heading(c, text=c)
            if c == "Diff":
                ar_tree.column(c, width=40, minwidth=35, stretch=False, anchor="center")
            elif c == "RowID":
                ar_tree.column(c, width=70, minwidth=50, stretch=False)
            elif c == "Status":
                ar_tree.column(c, width=90, minwidth=70, stretch=False)
            elif c == "Table":
                ar_tree.column(c, width=140, minwidth=80, stretch=False)
            else:
                ar_tree.column(c, width=data_w, minwidth=60, stretch=True)

        ar_sb = ttk.Scrollbar(self._ar_border, orient="vertical",
                               command=ar_tree.yview)
        ar_xsb = ttk.Scrollbar(self._ar_border, orient="horizontal",
                                command=ar_tree.xview)
        ar_tree.configure(yscrollcommand=ar_sb.set, xscrollcommand=ar_xsb.set)
        ar_sb.pack(side="right", fill="y")
        ar_xsb.pack(side="bottom", fill="x")
        ar_tree.pack(fill="both", expand=True)
        # Double-click to open record detail
        ar_tree.bind("<Double-1>", lambda e: self._ar_dblclick(ar_tree, page_data, sample_cols))
        self._ar_tree_ref = ar_tree
        self._ar_page_data_ref = page_data
        self._ar_sample_cols_ref = sample_cols

        for i, rec in enumerate(page_data):
            diff_status = rec.get("_diff_status", "same")
            diff_icon = diff_labels.get(diff_status, "?")
            diff_cols = rec.get("_diff_cols", set())
            # For "different" records, show count of changed columns
            if diff_status == "different" and diff_cols:
                diff_icon = f"\u2260 {len(diff_cols)}"

            tbl_display = rec["table"]
            if diff_status == "wal_table":
                tbl_display = f"★ {tbl_display}"
            vals = [diff_icon, rec["rowid"], tbl_display,
                    status_map.get(rec["category"], rec["category"])]
            for cn in sample_cols:
                v = rec["values_dict"].get(cn, "")
                vals.append(v[:200] if isinstance(v, str) and len(v) > 200 else v)

            # Tag based on diff status for coloring
            tag = f"diff_{diff_status}"
            ar_tree.insert("", "end", values=vals, tags=(tag,))

        # Color by diff status
        ar_tree.tag_configure("diff_different", foreground="#c45200",
                              background="#fff4e6")
        ar_tree.tag_configure("diff_not_in_db", foreground="#6b2fa0",
                              background="#f5f0ff")
        ar_tree.tag_configure("diff_wal_table", foreground="#0060a8",
                              background="#e8f4fd")  # Blue tint for WAL-only tables
        ar_tree.tag_configure("diff_same", foreground=C["text2"],
                              background=C["bg"])

    def _ar_prev_page(self):
        if self._ar_page > 0:
            self._ar_page -= 1
            self._display_all_wal_records()

    def _ar_next_page(self):
        total_pages = max(1, (len(self._ar_records) + self._ar_page_size - 1) // self._ar_page_size)
        if self._ar_page < total_pages - 1:
            self._ar_page += 1
            self._display_all_wal_records()

    def _ar_dblclick(self, tree, page_data, sample_cols):
        """Double-click on All Records row opens WAL record detail."""
        sel = tree.selection()
        if not sel:
            return
        idx = tree.index(sel[0])
        if idx >= len(page_data):
            return
        rec = page_data[idx]
        status_map = {"committed": "In DB", "uncommitted": "WAL Only",
                      "old": "Older Version"}
        src = f"WAL ({status_map.get(rec['category'], rec['category'])})"
        self._show_wal_row_detail(
            source=src, table=rec["table"],
            match_col="", rowid=rec["rowid"], match_val="",
            row_data=rec.get("values_dict", {}),
            frame_idx=rec["frame_idx"], page_num=rec["page_num"])

    def _ar_get_selected_record(self):
        """Get the selected record from All Records view."""
        tree = getattr(self, '_ar_tree_ref', None)
        page_data = getattr(self, '_ar_page_data_ref', [])
        if not tree or not page_data:
            return None, None, None
        sel = tree.selection()
        if not sel:
            return None, None, None
        idx = tree.index(sel[0])
        if idx >= len(page_data):
            return None, None, None
        sample_cols = getattr(self, '_ar_sample_cols_ref', [])
        return page_data[idx], sample_cols, tree.item(sel[0], "values")

    def _ar_copy_row_json(self):
        """Copy selected All Records row as JSON."""
        rec, sample_cols, _ = self._ar_get_selected_record()
        if not rec:
            return
        d = {"table": rec["table"], "rowid": rec["rowid"],
             "status": rec["category"], "frame": rec["frame_idx"],
             "page": rec["page_num"],
             "diff_status": rec.get("_diff_status", "unknown"),
             "changed_columns": sorted(rec.get("_diff_cols", set()))}
        d["data"] = dict(rec.get("values_dict", {}))
        self.clipboard_clear()
        self.clipboard_append(json.dumps(d, indent=2, default=str))

    def _ar_copy_row_csv(self):
        """Copy selected All Records row as CSV."""
        import io
        rec, sample_cols, vals = self._ar_get_selected_record()
        if not rec or not vals:
            return
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["Diff", "RowID", "Table", "Status"] + sample_cols
        w.writerow(cols)
        w.writerow(list(vals))
        self.clipboard_clear()
        self.clipboard_append(buf.getvalue())

    def _copy_wal_hex(self):
        """Copy the formatted hex dump (with offsets and ASCII)."""
        try:
            txt = self._wal_hex_view.get("1.0", "end-1c")
            if txt.strip():
                self.clipboard_clear()
                self.clipboard_append(txt)
        except Exception:
            pass

    def _copy_wal_hex_raw(self):
        """Copy raw hex bytes only (e.g. 0D098C000D01A4000A5C...)."""
        try:
            data = getattr(self, '_wal_selected_page_data', None)
            if data:
                self.clipboard_clear()
                self.clipboard_append(data.hex().upper())
        except Exception:
            pass

    def _copy_wal_hex_b64(self):
        """Copy page data as Base64."""
        import base64
        try:
            data = getattr(self, '_wal_selected_page_data', None)
            if data:
                self.clipboard_clear()
                self.clipboard_append(base64.b64encode(data).decode("ascii"))
        except Exception:
            pass

    def _filter_wal_frames(self):
        """Filter WAL frames by table, category, page type, and page number.
        Also triggers All Records reload when in that view mode."""
        if self._wal_view_mode == "records":
            self._load_all_wal_records()
            return
        cat = self._wal_cat_var.get()
        pt = self._wal_pt_var.get()
        tbl = self._wal_table_var.get()
        # Strip ★ prefix and "(WAL-only)" suffix for filtering
        if tbl.startswith("★ "):
            tbl = tbl[2:].split("  (WAL-only)")[0].strip()
        page_filter = self._wal_page_var.get().strip()

        page_map = getattr(self.db.wal, 'page_map', {})
        frames = self._wal_all_frames
        if tbl != "All":
            frames = [f for f in frames
                      if page_map.get(f.page_num, f"page_{f.page_num}") == tbl]
        if cat != "All":
            frames = [f for f in frames if f.category == cat]
        if pt != "All":
            frames = [f for f in frames if f.page_type == pt]
        if page_filter:
            try:
                pn = int(page_filter)
                frames = [f for f in frames if f.page_num == pn]
            except ValueError:
                pass

        self._wal_filtered_frames = frames
        self._display_wal_frames()

    def _wal_status_label(self, category):
        """Convert internal category to user-friendly status label.
        Note: In forensic extraction scenarios, the committed/uncommitted
        distinction depends on extraction timing and may not be reliable."""
        return {"committed": "In DB", "uncommitted": "WAL Only",
                "old": "Older Version"}.get(category, category)

    def _display_wal_frames(self):
        """Render filtered WAL frames into the treeview with table names."""
        tree = self._wal_tree
        tree.delete(*tree.get_children())

        wp = self.db.wal
        page_map = getattr(wp, 'page_map', {})

        for i, f in enumerate(self._wal_filtered_frames):
            tag = f.category
            status = self._wal_status_label(f.category)
            table_name = page_map.get(f.page_num, f"page_{f.page_num}")

            # Count records for table leaf pages
            rec_count = ""
            if f.page_type_byte == 0x0D:
                try:
                    pd = wp.get_page_data(f.index)
                    info = wp.parse_btree_page(pd)
                    if info:
                        rec_count = str(info['cell_count'])
                except Exception:
                    pass

            tree.insert("", "end", iid=str(f.index),
                        values=(table_name, status, f.page_type,
                                rec_count),
                        tags=(tag,))

        tree.tag_configure("committed", foreground=C["wal_committed"],
                          background="#f0faf5")
        tree.tag_configure("uncommitted", foreground="#7a4100",
                          background="#fff8e6")
        tree.tag_configure("old", foreground=C["wal_old"],
                          background="#fff0ed")

    def _sort_wal_tree(self, col):
        """Sort WAL frame list by clicked column."""
        reverse = (self._wal_sort_col == col and not self._wal_sort_reverse)
        self._wal_sort_col = col
        self._wal_sort_reverse = reverse

        page_map = getattr(self.db.wal, 'page_map', {})
        key_map = {
            "Table": lambda f: page_map.get(f.page_num, ""),
            "Status": lambda f: f.category,
            "Data Type": lambda f: f.page_type,
            "Records": lambda f: f.page_num,  # approx sort
        }
        key_fn = key_map.get(col, lambda f: f.index)
        self._wal_filtered_frames.sort(key=key_fn, reverse=reverse)
        self._display_wal_frames()

    def _on_wal_select(self, event=None):
        """Handle selection of a WAL frame — show summary, recovered data, hex."""
        sel = self._wal_tree.selection()
        if not sel:
            return
        try:
            frame_idx = int(sel[0])
        except (ValueError, IndexError):
            return

        if not self.db.has_wal:
            return
        wp = self.db.wal
        page_data = wp.get_page_data(frame_idx)
        self._wal_selected_page_data = page_data  # Store for copy operations
        frame = wp.frames[frame_idx]
        status = self._wal_status_label(frame.category)
        page_map = getattr(wp, 'page_map', {})
        col_map = getattr(wp, 'col_map', {})
        table_name = page_map.get(frame.page_num, f"page_{frame.page_num}")

        # ── Summary tab ──
        info = wp.parse_btree_page(page_data)
        self._wal_page_info.configure(state="normal")
        self._wal_page_info.delete("1.0", "end")

        lines = [
            f"FRAME #{frame.index}  —  Table: {table_name}",
            f"{'='*50}",
            f"",
            f"Table:       {table_name}",
            f"Status:      {status}",
        ]

        if frame.category == "committed":
            lines.append("             This data was saved to the database.")
        elif frame.category == "uncommitted":
            lines.append("             This data was NEVER saved! It may contain")
            lines.append("             drafts, crashed transactions, or deleted data.")
        else:
            lines.append("             This is an older version that was overwritten.")
            lines.append("             The current data may be different.")

        # Show column names if known
        known_cols = col_map.get(table_name, [])
        if known_cols:
            lines.extend([
                f"",
                f"Columns:     {', '.join(known_cols[:15])}{'...' if len(known_cols) > 15 else ''}",
            ])

        lines.extend([
            f"",
            f"Page Number: {frame.page_num}",
            f"Data Type:   {frame.page_type}",
            f"Page Size:   {len(page_data):,} bytes",
            f"Transaction: {'Final frame (commit marker)' if frame.commit_size > 0 else 'Mid-transaction or uncommitted'}",
        ])

        if info:
            type_desc = {
                0x0D: "Contains actual row data — check 'Recovered Data' tab",
                0x05: "Internal tree node — points to child pages with actual data",
                0x0A: "Index data — used for fast lookups, no row content",
                0x02: "Internal index node — points to child index pages",
            }.get(frame.page_type_byte, "Cannot determine page structure")
            lines.extend([
                f"",
                f"Page Structure:",
                f"  Type:  {frame.page_type} (0x{frame.page_type_byte:02X})",
                f"  Info:  {type_desc}",
                f"  Cells: {info['cell_count']} data entries on this page",
            ])
            if info.get("right_child"):
                lines.append(f"  Child: Points to page {info['right_child']}")
        else:
            lines.extend([
                f"",
                f"This page does not have a standard B-tree structure.",
                f"It may be an overflow page (continuation of a large value)",
                f"or a free/empty page.",
            ])

        self._wal_page_info.insert("1.0", "\n".join(lines))
        self._wal_page_info.configure(state="disabled")

        # ── Raw Hex tab ──
        self._wal_hex_view.configure(state="normal")
        self._wal_hex_view.delete("1.0", "end")
        hex_lines = [
            f"Raw hex dump of page data ({len(page_data):,} bytes)",
            f"Showing first {min(len(page_data), 4096):,} bytes:",
            f"{'='*72}",
            f"Offset    Hexadecimal                                       ASCII",
            f"{'─'*72}",
        ]
        show_bytes = min(len(page_data), 4096)
        for off in range(0, show_bytes, 16):
            chunk = page_data[off:off + 16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            hex_lines.append(f"{off:08X}  {hex_part:<48s}  {ascii_part}")
        if len(page_data) > 4096:
            hex_lines.append(f"\n... ({len(page_data) - 4096:,} more bytes not shown)")
        self._wal_hex_view.insert("1.0", "\n".join(hex_lines))
        self._wal_hex_view.configure(state="disabled")

        # ── Recovered Data tab ──
        for w in self._wal_rec_border.winfo_children():
            w.destroy()

        if frame.page_type_byte == 0x0D:
            cells = wp.parse_leaf_cells(page_data)
            if cells:
                # Header with status, table name, and count
                header = tk.Frame(self._wal_rec_border, bg="#e8e0f0")
                header.pack(fill="x")
                cat_colors = {"committed": "#00875a", "uncommitted": "#c25100", "old": "#de350b"}
                tk.Label(header,
                         text=f"  Table: {table_name}  |  {len(cells)} records recovered  |  "
                              f"Status: {status}  |  Frame #{frame.index}",
                         font=("Segoe UI", 9, "bold"),
                         fg=cat_colors.get(frame.category, "#1a1a2e"),
                         bg="#e8e0f0", anchor="w").pack(fill="x", padx=4, pady=3)

                # Determine max column count and use real column names
                max_cols = max(len(c["values"]) for c in cells)
                # Use real column names from col_map if available
                if known_cols and len(known_cols) >= max_cols:
                    rec_cols = ["RowID"] + known_cols[:max_cols]
                else:
                    # Fallback: use col_map names where available, col{i} for rest
                    rec_cols = ["RowID"] + [
                        known_cols[i] if i < len(known_cols) else f"col{i}"
                        for i in range(max_cols)
                    ]
                rec_tree = ttk.Treeview(self._wal_rec_border, columns=rec_cols,
                                         show="headings", selectmode="browse")
                for c in rec_cols:
                    rec_tree.heading(c, text=c)
                    rec_tree.column(c, width=120, minwidth=60, stretch=True)
                rec_tree.column("RowID", width=70, minwidth=50, stretch=False)

                rsb = ttk.Scrollbar(self._wal_rec_border, orient="vertical",
                                     command=rec_tree.yview)
                rec_tree.configure(yscrollcommand=rsb.set)
                rsb.pack(side="right", fill="y")
                rec_tree.pack(fill="both", expand=True)

                pk_idx = getattr(wp, 'pk_col_idx', {}).get(table_name, -1)
                for ci, cell in enumerate(cells):
                    vals = []
                    for vi, v in enumerate(cell["values"]):
                        # INTEGER PRIMARY KEY: use rowid
                        if vi == pk_idx and v is None:
                            vals.append(str(cell["rowid"]))
                        elif v is None:
                            vals.append("NULL")
                        elif isinstance(v, bytes):
                            bt = blob_type(v)
                            vals.append(f"[BLOB: {fmtb(len(v))}, {bt}]")
                        elif isinstance(v, float):
                            vals.append(f"{v:.6g}")
                        else:
                            sv = str(v)
                            vals.append(sv if len(sv) <= 200 else sv[:200] + "...")
                    # Pad if fewer values than max
                    while len(vals) < max_cols:
                        vals.append("")
                    tag = "odd" if ci % 2 else "even"
                    rec_tree.insert("", "end",
                                    values=(cell["rowid"], *vals),
                                    tags=(tag,))
                rec_tree.tag_configure("odd", background=C["alt"])
                rec_tree.tag_configure("even", background=C["bg"])
            else:
                ttk.Label(self._wal_rec_border,
                          text="This is a Table Leaf page but no cell data could be parsed.\n"
                               "The page may be empty or contain only overflow pointers.",
                          style="M.TLabel", wraplength=600).pack(padx=10, pady=10)
        else:
            type_help = {
                "Table Interior": "This page is an internal tree node. It contains pointers to "
                                  "child pages but no actual row data. The real data is on Table Leaf pages.",
                "Index Leaf": "This page contains index entries (used for fast lookups). "
                              "To see actual row data, filter by 'Table Leaf' data type.",
                "Index Interior": "This page is an internal index node. It helps SQLite navigate "
                                  "the index tree but doesn't contain user data.",
                "Overflow / Free": "This page stores the continuation of a large value that didn't "
                                   "fit on a single page, or it's a free/unused page.",
            }
            help_text = type_help.get(frame.page_type,
                                       "This page type doesn't contain directly readable row data.")
            msg_frame = tk.Frame(self._wal_rec_border, bg=C["bg2"])
            msg_frame.pack(fill="both", expand=True)
            tk.Label(msg_frame,
                     text=f"Page Type: {frame.page_type}",
                     font=("Segoe UI", 10, "bold"), bg=C["bg2"], fg=C["text"],
                     anchor="w").pack(fill="x", padx=15, pady=(15, 4))
            tk.Label(msg_frame,
                     text=help_text,
                     font=("Segoe UI", 9), bg=C["bg2"], fg=C["text2"],
                     wraplength=600, justify="left", anchor="w").pack(fill="x", padx=15, pady=(0, 4))
            tk.Label(msg_frame,
                     text="Tip: Use the 'Data Type' filter above and select 'Table Leaf' "
                          "to see only pages with recoverable row data.",
                     font=("Segoe UI", 8, "italic"), bg=C["bg2"], fg="#0052cc",
                     wraplength=600, justify="left", anchor="w").pack(fill="x", padx=15, pady=(0, 15))

    def _export_wal_all_records(self):
        """Export all recovered WAL records to JSON."""
        if not self.db.has_wal:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile="wal_all_records.json",
            filetypes=[("JSON", "*.json"), ("CSV", "*.csv")])
        if not path:
            return
        try:
            records = list(self.db.wal.recover_all_records())
            status_map = {"committed": "In DB", "uncommitted": "WAL Only",
                          "old": "Older Version"}
            if path.lower().endswith(".csv"):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["Table", "RowID", "Frame#", "Page#", "Status",
                                "Data"])
                    for rec in records:
                        w.writerow([rec["table"], rec["rowid"],
                                    rec["frame_idx"], rec["page_num"],
                                    status_map.get(rec["category"], rec["category"]),
                                    json.dumps(rec["values_dict"], default=str)])
            else:
                out = []
                for rec in records:
                    out.append({
                        "table": rec["table"],
                        "rowid": rec["rowid"],
                        "frame_idx": rec["frame_idx"],
                        "page_num": rec["page_num"],
                        "status": status_map.get(rec["category"], rec["category"]),
                        "category": rec["category"],
                        "data": rec["values_dict"],
                    })
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"wal_records": out, "total": len(out)},
                              f, indent=2, default=str)
            messagebox.showinfo("Export Complete",
                                f"Exported {len(records)} WAL records to:\n"
                                f"{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _export_wal_blobs(self):
        """Export all BLOB data found in WAL records to individual files."""
        if not self.db.has_wal:
            return
        folder = filedialog.askdirectory(title="Select folder for WAL BLOBs")
        if not folder:
            return
        try:
            count = 0
            errors = 0
            for rec in self.db.wal.recover_all_records():
                for vi, v in enumerate(rec["raw_values"]):
                    if isinstance(v, bytes) and len(v) > 0:
                        bt = blob_type(v)
                        ext = _EXT_MAP.get(bt, ".bin")
                        col_names = self.db.wal.col_map.get(rec["table"], [])
                        cn = col_names[vi] if vi < len(col_names) else f"col{vi}"
                        fname = f"{rec['table']}_r{rec['rowid']}_f{rec['frame_idx']}_{cn}{ext}"
                        try:
                            with open(os.path.join(folder, fname), "wb") as f:
                                f.write(v)
                            count += 1
                        except Exception:
                            errors += 1
            msg = f"Exported {count} BLOB(s) from WAL to:\n{folder}"
            if errors:
                msg += f"\n({errors} error(s))"
            messagebox.showinfo("Export Complete", msg)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _show_wal_header(self):
        """Show WAL header technical details in a proper window."""
        if not self.db.has_wal:
            messagebox.showinfo("WAL Details", "No WAL file loaded.")
            return
        h = self.db.wal.header
        s = self.db.wal.summary()
        info = (
            f"WAL File Technical Details\n{'='*45}\n\n"
            f"File Header:\n"
            f"  Magic Number: 0x{h.magic:08X} ({'Big-endian' if h.magic == 0x377f0682 else 'Little-endian'})\n"
            f"  Format Version: {h.version}\n"
            f"  Page Size: {h.page_size:,} bytes\n"
            f"  Checkpoint Sequence: {h.checkpoint_seq}\n"
            f"  Salt-1: 0x{h.salt1:08X} ({h.salt1})\n"
            f"  Salt-2: 0x{h.salt2:08X} ({h.salt2})\n"
            f"  Checksum-1: 0x{h.checksum1:08X}\n"
            f"  Checksum-2: 0x{h.checksum2:08X}\n"
            f"\nFrame Summary:\n"
            f"  Total Frames: {s['total_frames']}\n"
            f"  In DB (committed): {s['committed']}\n"
            f"  WAL Only (uncommitted): {s['uncommitted']}\n"
            f"  Older Version (old): {s['old']}\n"
            f"  Unique Pages Modified: {s['unique_pages']}\n"
            f"  WAL File Size: {fmtb(s['wal_size'])}\n"
            f"\nPage Types:\n"
        )
        for pt, count in sorted(s.get("page_types", {}).items()):
            info += f"  {pt}: {count} frames\n"
        # Show in a proper window
        win = tk.Toplevel(self)
        win.title("WAL Technical Details")
        win.geometry("520x460")
        win.configure(bg=C["bg"])
        win.transient(self)
        txt = tk.Text(win, wrap="word", bg=C["bg2"], fg=C["text"],
                      font=("Consolas", 10), relief="flat", padx=10, pady=8)
        txt.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        txt.insert("1.0", info)
        txt.configure(state="disabled")
        bot = tk.Frame(win, bg=C["bg"])
        bot.pack(fill="x", padx=8, pady=6)
        btn_cfg = dict(font=("Segoe UI", 9), relief="flat", bd=0, padx=10, pady=4, cursor="hand2")
        def _copy():
            win.clipboard_clear()
            win.clipboard_append(info)
        tk.Button(bot, text="Copy", command=_copy, bg=C["acl"], fg=C["accent"], **btn_cfg).pack(side="left", padx=2)
        tk.Button(bot, text="Close", command=win.destroy, bg=C["bg3"], fg=C["text2"], **btn_cfg).pack(side="right", padx=2)

    def _export_wal_summary(self):
        """Export WAL frame summary to CSV."""
        if not self.db.has_wal:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            title="Export WAL Summary")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Frame#", "Page#", "Status", "Category", "Data Type",
                            "Transaction", "Salt1", "Salt2"])
                for frame in self.db.wal.frames:
                    w.writerow([frame.index, frame.page_num,
                                self._wal_status_label(frame.category),
                                frame.category, frame.page_type, frame.commit_size,
                                f"0x{frame.salt1:08X}", f"0x{frame.salt2:08X}"])
            messagebox.showinfo("Export Complete",
                                f"WAL summary exported to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Error", str(e))
