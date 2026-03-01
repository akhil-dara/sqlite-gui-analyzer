"""Dialog windows for SQLite GUI Analyzer."""

import sys
import os
import io
import csv
import json
import binascii
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from constants import C, HAS_PIL, VERSION, _EXT_MAP
if HAS_PIL:
    from constants import PILImage, ImageTk
from utils import blob_type, is_image, fmtb, try_decode_timestamp, _build_schema_text, fmt_count, _int_count


# ── HelpDialog ───────────────────────────────────────────────────────────
class HelpDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("SQLite GUI Analyzer - Help")
        self.geometry("750x600")
        self.configure(bg=C["bg"])
        self.transient(parent)

        txt = tk.Text(self, wrap="word", font=("Segoe UI", 10), bg=C["bg"],
                      fg=C["text"], relief="flat", padx=16, pady=12)
        sb = ttk.Scrollbar(self, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        txt.tag_configure("h1", font=("Segoe UI", 14, "bold"), foreground=C["accent"],
                          spacing1=12, spacing3=6)
        txt.tag_configure("h2", font=("Segoe UI", 11, "bold"), foreground=C["text"],
                          spacing1=10, spacing3=4)
        txt.tag_configure("code", font=("Consolas", 10), foreground=C["purple"],
                          background=C["bg2"])
        txt.tag_configure("tip", foreground=C["green"], font=("Segoe UI", 10, "italic"))
        txt.tag_configure("warn", foreground=C["red"], font=("Segoe UI", 10, "bold"))

        def h1(t): txt.insert("end", t + "\n", "h1")
        def h2(t): txt.insert("end", t + "\n", "h2")
        def p(t): txt.insert("end", t + "\n\n")
        def code(t): txt.insert("end", t + "\n", "code"); txt.insert("end", "\n")
        def tip(t): txt.insert("end", t + "\n\n", "tip")
        def warn(t): txt.insert("end", t + "\n\n", "warn")

        h1("SQLite GUI Analyzer v" + VERSION)

        h2("OVERVIEW")
        p("SQLite GUI Analyzer is a desktop tool for quickly searching, browsing, "
          "and analyzing SQLite databases. Designed for developers, data analysts, "
          "investigators, and anyone who needs to inspect database contents "
          "and understand schema structure efficiently — without writing SQL queries.")

        h2("SEARCH TAB")
        p("The Search tab scans all cell values across selected tables. Results show "
          "the table, column, row ID, matched value, and data type for each hit. "
          "Double-click any result to open the full row in a detail window.\n\n"
          "Use the Scope button to select which tables to search. Use Reset Scope "
          "to restore the selection to all tables. Filter results by table, column, "
          "or type using the drop-downs above the results list.")

        h2("SEARCH MODES")
        p("Case-Insensitive: Default. Matches text regardless of case.\n"
          "Case-Sensitive: Matches text with exact case.\n"
          "Exact Match: Cell value must equal the search term exactly.\n"
          "Starts With / Ends With: Value must begin or end with the term.\n"
          "Regex: Full Python regular expression matching.\n"
          "BLOB/Hex: Search within binary data (text cast). Enable Deep BLOB for hex-level matching.\n"
          "Column Name: Find columns whose name contains the search text.")

        h2("REGEX SEARCH")
        warn("Type a SINGLE backslash in the search entry: \\d not \\\\d")
        p("The search entry passes text directly to Python's re module. "
          "Omit ^ and $ anchors to find patterns anywhere within values.\n\n"
          "Common patterns:\n"
          "  \\d  any digit (0-9)\n"
          "  \\w  word character (letter, digit, underscore)\n"
          "  \\s  whitespace (space, tab, newline)\n"
          "  \\b  word boundary (between \\w and non-\\w)\n"
          "  \\.  literal dot\n"
          "  [6-9]  character in range 6 to 9\n"
          "  {N}  repeat exactly N times\n"
          "  (?<!X)  not preceded by X (negative lookbehind)\n"
          "  (?!X)   not followed by X (negative lookahead)")

        h2("REGEX EXAMPLES")
        tip("Indian mobile number:  \\b[6-9]\\d{9}\\b\n"
            "  Finds exactly 10 digits starting with 6/7/8/9.\n"
            "  \\b = word boundary — rejects hex strings and UUIDs.\n\n"
            "Email address:  (?i)\\b[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}\\b\n\n"
            "URL:  https?://[^\\s<>\"]+\n\n"
            "US phone:  \\d{3}-\\d{3}-\\d{4}\n\n"
            "IP address:  \\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}")

        h2("REGEX BOUNDARY GUIDE")
        p("When searching for numbers, choosing the right boundary matters:\n\n"
          "  (?<!\\d)..(?!\\d)  — only checks adjacent digits.\n"
          "    Matches digits inside hex strings (e.g. 8906041844 in\n"
          "    8906041844C7D671...). Use when you want ANY digit sequence.\n\n"
          "  \\b...\\b  — checks adjacent letters AND digits.\n"
          "    Rejects hex/UUID strings. Use for phone numbers.\n\n"
          "  (?<![\\w/=&?#%])..(?![\\w/=&?#%])  — also excludes URL chars.\n"
          "    Strictest filter. Use when data has many URLs.")

        h2("HOW REGEX SEARCH WORKS")
        p("The regex pipeline uses a two-phase approach for speed:\n\n"
          "1. Literal hint extraction — uses Python's regex parser (sre_parse) to\n"
          "   extract guaranteed literal substrings from the pattern. For example,\n"
          "   'foo@bar\\.com' extracts 'foo@bar.com'; '\\b[6-9]\\d{9}\\b' extracts\n"
          "   nothing (no literals).\n\n"
          "2. SQL pre-filter — if a literal hint exists, uses LIKE '%hint%' to\n"
          "   narrow candidate rows at the database level before applying regex.\n"
          "   This can be 4-10x faster on large tables.\n\n"
          "3. Full scan — if no literal hint exists, scans all rows and applies\n"
          "   Python regex on every non-BLOB cell. Uses batched fetching (5,000\n"
          "   rows) with cancel support.\n\n"
          "All regex syntax is supported: lookarounds, groups, flags, named\n"
          "groups, character classes, quantifiers, alternation, etc.")

        h2("BROWSE TAB")
        p("Select a table from the drop-down to view its rows. Features include:\n"
          "- Click column headers to sort ascending/descending.\n"
          "- Filter rows by typing in the filter box; select a specific column or All Columns.\n"
          "- Export current page to CSV or export all BLOBs to a folder.\n"
          "- Click a row to see a preview; double-click to open the full row detail.")

        h2("SCHEMA SIDEBAR")
        p("The left sidebar shows the database schema: tables, views, indexes, and triggers. "
          "Expand a table to see its columns, indexes, and foreign keys. "
          "Right-click a table for quick actions: browse, copy CREATE SQL, copy schema, "
          "or set it as the search scope. Use the filter box and Cols checkbox to "
          "search table and column names.")

        h2("KEYBOARD SHORTCUTS")
        p("Ctrl+O  Open a database file\n"
          "Ctrl+F  Focus the search entry\n"
          "Escape  Cancel a running search\n"
          "Enter   Start search (when search entry is focused)")

        h2("WAL FORENSIC ANALYSIS")
        p("When a database uses WAL (Write-Ahead Logging) mode and a .db-wal file exists, "
          "a WAL tab appears automatically. This uses a pure binary parser to read ALL data "
          "in the WAL file, including:\n\n"
          "- Committed: data from completed transactions (green)\n"
          "- Uncommitted: data from pending/incomplete transactions (orange)\n"
          "- Old/Pre-checkpoint: data from before the last checkpoint (red)\n\n"
          "Search WAL: Enable the 'Search WAL' checkbox in the Search tab to include WAL "
          "data in search results. Results show the source (DB or WAL) and category.\n\n"
          "The WAL tab shows all frames with page type, category, and salt values. "
          "Click a frame to see its page info, hex dump, and parsed records.")

        h2("TIPS FOR EFFICIENT USAGE")
        p("- Use Scope to limit searches to relevant tables for faster results.\n"
          "- Increase the Max/table limit for thorough searches; decrease it for quick scans.\n"
          "- Right-click tables in the schema sidebar to quickly search or browse them.\n"
          "- Copy Schema includes PRIMARY KEY, NOT NULL, DEFAULT, UNIQUE constraints.\n"
          "- Row Detail values are selectable (click and Ctrl+C to copy any value).\n"
          "- Install Pillow (pip install Pillow) for JPEG and WEBP image previews in BLOB viewer.\n"
          "- Enable 'Search WAL' to find data in uncommitted transactions and old WAL frames.")

        h2("ABOUT")
        p(f"SQLite GUI Analyzer v{VERSION}\n"
          f"Python {sys.version.split()[0]}\n"
          f"Pillow: {'Installed' if HAS_PIL else 'Not installed (optional, for JPEG/WEBP images)'}")

        txt.configure(state="disabled")

        btnf = ttk.Frame(self)
        btnf.pack(fill="x", padx=8, pady=8)

        def copy_help():
            txt.configure(state="normal")
            self.clipboard_clear()
            self.clipboard_append(txt.get("1.0", "end"))
            txt.configure(state="disabled")

        ttk.Button(btnf, text="Copy", command=copy_help).pack(side="left", padx=4)
        ttk.Button(btnf, text="Close", command=self.destroy).pack(side="right", padx=4)


# ── ScopeDlg ─────────────────────────────────────────────────────────────
class ScopeDlg(tk.Toplevel):
    def __init__(self, parent, tables, counts, selected):
        super().__init__(parent)
        self.title("Search Scope")
        self.geometry("450x550")
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.grab_set()
        self.result = None
        self._tables = tables
        self._counts = counts
        self._vars = {}
        self._visible_tables = []

        # Filter row
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))

        self._filter_var = tk.StringVar()
        fe = ttk.Entry(top, textvariable=self._filter_var, width=25)
        fe.pack(side="left", padx=(0, 6))
        self._filter_var.trace_add("write", lambda *_: self._rebuild_list())

        self._hide_empty = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Hide empty", variable=self._hide_empty,
                         command=self._rebuild_list).pack(side="left")

        # Buttons - operate on VISIBLE tables only
        btnrow = ttk.Frame(self)
        btnrow.pack(fill="x", padx=8, pady=4)
        ttk.Button(btnrow, text="All Visible", command=self._sel_all, style="Sm.TButton").pack(side="left", padx=2)
        ttk.Button(btnrow, text="None Visible", command=self._sel_none, style="Sm.TButton").pack(side="left", padx=2)
        ttk.Button(btnrow, text="Invert", command=self._sel_invert, style="Sm.TButton").pack(side="left", padx=2)
        ttk.Button(btnrow, text="Non-Empty", command=self._sel_nonempty, style="Sm.TButton").pack(side="left", padx=2)

        # Stats
        empty_count = sum(1 for t in tables if self._get_count(t) == 0)
        nonempty_count = len(tables) - empty_count
        self._stats_label = ttk.Label(
            self, text=f"Total: {len(tables)} tables  |  {nonempty_count} non-empty  |  {empty_count} empty",
            style="M.TLabel")
        self._stats_label.pack(fill="x", padx=8, pady=(0, 2))

        # Scrollable checkbox list
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=8, pady=4)
        self._canvas = tk.Canvas(container, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas_win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._canvas_win, width=e.width))
        def _scope_scroll(e):
            try:
                self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        self._scope_scroll_fn = _scope_scroll
        self._canvas.bind("<MouseWheel>", _scope_scroll)
        self._inner.bind("<MouseWheel>", _scope_scroll)

        # Create vars for ALL tables (persist across rebuilds)
        for t in tables:
            self._vars[t] = tk.BooleanVar(value=(t in selected))

        self._summary = ttk.Label(self, style="M.TLabel")
        self._summary.pack(fill="x", padx=8)

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=8)
        ttk.Button(bot, text="Apply", style="P.TButton", command=self._apply).pack(side="right", padx=4)
        ttk.Button(bot, text="Cancel", command=self.destroy).pack(side="right", padx=4)

        self._rebuild_list()

    def _get_count(self, t):
        return _int_count(self._counts.get(t, 0))

    def _rebuild_list(self):
        """Destroy and recreate checkbuttons based on filter + hide-empty."""
        for w in self._inner.winfo_children():
            w.destroy()
        filt = self._filter_var.get().lower().strip()
        he = self._hide_empty.get()
        self._visible_tables = []
        for t in self._tables:
            if filt and filt not in t.lower():
                continue
            cnt = self._get_count(t)
            if he and cnt == 0:
                continue
            self._visible_tables.append(t)
        for t in self._visible_tables:
            cnt = self._get_count(t)
            cnt_str = fmt_count(cnt)
            cb = ttk.Checkbutton(self._inner, text=f"{t}  ({cnt_str})",
                                  variable=self._vars[t], command=self._update_summary)
            cb.pack(anchor="w", pady=1, padx=4)
            cb.bind("<MouseWheel>", self._scope_scroll_fn)
        self._update_summary()

    def _sel_all(self):
        for t in self._visible_tables:
            self._vars[t].set(True)
        self._update_summary()

    def _sel_none(self):
        for t in self._visible_tables:
            self._vars[t].set(False)
        self._update_summary()

    def _sel_invert(self):
        for t in self._visible_tables:
            self._vars[t].set(not self._vars[t].get())
        self._update_summary()

    def _sel_nonempty(self):
        for t in self._visible_tables:
            self._vars[t].set(self._get_count(t) > 0)
        self._update_summary()

    def _update_summary(self):
        sel = sum(1 for v in self._vars.values() if v.get())
        total = len(self._vars)
        vis = len(self._visible_tables)
        vis_sel = sum(1 for t in self._visible_tables if self._vars[t].get())
        self._summary.configure(
            text=f"{vis_sel} of {vis} visible selected  |  {sel} of {total} total selected")

    def _apply(self):
        self.result = [t for t, v in self._vars.items() if v.get()]
        self.destroy()


# ── BlobViewer ───────────────────────────────────────────────────────────
class BlobViewer(tk.Toplevel):
    def __init__(self, parent, data, col_name="BLOB"):
        super().__init__(parent)
        self.title(f"BLOB Viewer - {col_name} ({fmtb(len(data))})")
        self.geometry("700x550")
        self.configure(bg=C["bg"])
        self._data = data
        self._pil_img = None
        self._tk_img = None
        self._zoom = 1.0

        # Pack buttons FIRST at bottom so they're always visible
        btnf = ttk.Frame(self)
        btnf.pack(side="bottom", fill="x", padx=8, pady=6)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        # Hex tab
        hex_frame = ttk.Frame(nb)
        nb.add(hex_frame, text="Hex Dump")
        hex_txt = tk.Text(hex_frame, font=("Consolas", 9), wrap="none", bg=C["bg2"])
        hex_sb = ttk.Scrollbar(hex_frame, orient="vertical", command=hex_txt.yview)
        hex_txt.configure(yscrollcommand=hex_sb.set)
        hex_sb.pack(side="right", fill="y")
        hex_txt.pack(fill="both", expand=True)
        self._fill_hex(hex_txt, data[:16384])

        # Text tab
        txt_frame = ttk.Frame(nb)
        nb.add(txt_frame, text="Text View")
        txt_txt = tk.Text(txt_frame, font=("Consolas", 9), wrap="word", bg=C["bg2"])
        txt_sb = ttk.Scrollbar(txt_frame, orient="vertical", command=txt_txt.yview)
        txt_txt.configure(yscrollcommand=txt_sb.set)
        txt_sb.pack(side="right", fill="y")
        txt_txt.pack(fill="both", expand=True)
        try:
            decoded = data[:65536].decode("utf-8", errors="replace")
        except Exception:
            decoded = "(cannot decode)"
        txt_txt.insert("1.0", decoded)
        txt_txt.configure(state="disabled")

        # Image tab
        if is_image(data):
            img_frame = ttk.Frame(nb)
            nb.add(img_frame, text="Image Preview")
            self._setup_image_tab(img_frame, data)

        def save_blob():
            bt = blob_type(data)
            ext = _EXT_MAP.get(bt, ".bin")
            path = filedialog.asksaveasfilename(defaultextension=ext,
                                                 filetypes=[("All files", "*.*")])
            if path:
                try:
                    with open(path, "wb") as f:
                        f.write(data)
                except Exception as e:
                    messagebox.showerror("Error", str(e))

        def copy_hex():
            self.clipboard_clear()
            self.clipboard_append(binascii.hexlify(data).decode())

        def copy_b64():
            import base64
            self.clipboard_clear()
            self.clipboard_append(base64.b64encode(data).decode())

        ttk.Button(btnf, text="Save", command=save_blob).pack(side="left", padx=4)
        ttk.Button(btnf, text="Copy Hex", command=copy_hex).pack(side="left", padx=4)
        ttk.Button(btnf, text="Copy Base64", command=copy_b64).pack(side="left", padx=4)
        ttk.Button(btnf, text="Close", command=self.destroy).pack(side="right", padx=4)

    def _fill_hex(self, txt, data):
        lines = []
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{i:08x}  {hex_part:<48s}  {ascii_part}")
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")

    def _setup_image_tab(self, frame, data):
        if HAS_PIL:
            try:
                self._pil_img = PILImage.open(io.BytesIO(data))
                info_bar = ttk.Frame(frame)
                info_bar.pack(fill="x", padx=4, pady=2)
                fmt = self._pil_img.format or "Unknown"
                w, h = self._pil_img.size
                mode = self._pil_img.mode
                ttk.Label(info_bar, text=f"Format: {fmt}  |  Size: {w}x{h}  |  Mode: {mode}",
                          style="M.TLabel").pack(side="left")

                ctrl = ttk.Frame(frame)
                ctrl.pack(fill="x", padx=4, pady=2)
                ttk.Button(ctrl, text="Fit", style="Sm.TButton",
                           command=lambda: self._zoom_fit()).pack(side="left", padx=2)
                ttk.Button(ctrl, text="100%", style="Sm.TButton",
                           command=lambda: self._set_zoom(1.0)).pack(side="left", padx=2)
                ttk.Button(ctrl, text="Zoom +", style="Sm.TButton",
                           command=lambda: self._set_zoom(self._zoom * 1.25)).pack(side="left", padx=2)
                ttk.Button(ctrl, text="Zoom -", style="Sm.TButton",
                           command=lambda: self._set_zoom(self._zoom / 1.25)).pack(side="left", padx=2)
                self._zoom_lbl = ttk.Label(ctrl, text="100%", style="M.TLabel")
                self._zoom_lbl.pack(side="left", padx=8)

                cvs_frame = ttk.Frame(frame)
                cvs_frame.pack(fill="both", expand=True)
                self._img_canvas = tk.Canvas(cvs_frame, bg=C["bg3"], highlightthickness=0)
                xsb = ttk.Scrollbar(cvs_frame, orient="horizontal", command=self._img_canvas.xview)
                ysb = ttk.Scrollbar(cvs_frame, orient="vertical", command=self._img_canvas.yview)
                self._img_canvas.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
                ysb.pack(side="right", fill="y")
                xsb.pack(side="bottom", fill="x")
                self._img_canvas.pack(fill="both", expand=True)
                self._img_canvas.bind("<MouseWheel>",
                    lambda e: self._set_zoom(self._zoom * (1.1 if e.delta > 0 else 0.9)))

                self.after(100, self._zoom_fit)
            except Exception as e:
                ttk.Label(frame, text=f"Cannot load image: {e}").pack(padx=20, pady=20)
        else:
            # Check for PNG/GIF for basic tk.PhotoImage
            if data[:8] == b'\x89PNG\r\n\x1a\n' or data[:4] in (b'GIF8',):
                try:
                    self._tk_img = tk.PhotoImage(data=data)
                    lbl = ttk.Label(frame, image=self._tk_img)
                    lbl.pack(padx=10, pady=10)
                except Exception:
                    ttk.Label(frame, text="Install Pillow for full image support:\npip install Pillow",
                              foreground=C["text2"]).pack(padx=20, pady=20)
            else:
                ttk.Label(frame, text="Install Pillow for JPEG/WEBP image support:\npip install Pillow",
                          foreground=C["text2"]).pack(padx=20, pady=20)

    def _zoom_fit(self):
        if not self._pil_img:
            return
        self._img_canvas.update_idletasks()
        cw = max(self._img_canvas.winfo_width(), 100)
        ch = max(self._img_canvas.winfo_height(), 100)
        iw, ih = self._pil_img.size
        z = min(cw / iw, ch / ih, 1.0)
        self._set_zoom(z)

    def _set_zoom(self, z):
        if not self._pil_img:
            return
        self._zoom = max(0.05, min(z, 10.0))
        iw, ih = self._pil_img.size
        nw = max(1, int(iw * self._zoom))
        nh = max(1, int(ih * self._zoom))
        resized = self._pil_img.resize((nw, nh), PILImage.LANCZOS if hasattr(PILImage, 'LANCZOS') else PILImage.BILINEAR)
        self._tk_img = ImageTk.PhotoImage(resized)
        self._img_canvas.delete("all")
        self._img_canvas.create_image(0, 0, anchor="nw", image=self._tk_img)
        self._img_canvas.configure(scrollregion=(0, 0, nw, nh))
        self._zoom_lbl.configure(text=f"{int(self._zoom * 100)}%")


# ── RowWin ───────────────────────────────────────────────────────────────
class RowWin(tk.Toplevel):
    _pool = {}

    @classmethod
    def show(cls, parent, db, tbl, rid, search_term="", match_col=""):
        key = (tbl, rid)
        if key in cls._pool:
            try:
                w = cls._pool[key]
                w._search_term = search_term
                w._match_col = match_col
                w.lift()
                # Re-highlight if search term changed
                if search_term:
                    w._highlight_match()
                return w
            except Exception:
                pass
        w = cls(parent, db, tbl, rid, search_term, match_col)
        cls._pool[key] = w
        return w

    def __init__(self, parent, db, tbl, rid, search_term="", match_col=""):
        super().__init__(parent)
        self.title(f"{tbl} [rowid={rid}]")
        self.geometry("720x500")
        self.configure(bg=C["bg"])
        self._db = db
        self._tbl = tbl
        self._rid = rid
        self._search_term = search_term
        self._match_col = match_col
        self._tk_imgs = []
        self._col_widgets = {}  # col_name -> (row_frame, value_widget)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Bottom toolbar — grouped, styled buttons
        bot = tk.Frame(self, bg=C["bg3"], bd=0)
        bot.pack(side="bottom", fill="x")
        tk.Frame(bot, bg=C["border"], height=1).pack(fill="x", side="top")

        bot_inner = tk.Frame(bot, bg=C["bg3"])
        bot_inner.pack(fill="x", padx=6, pady=4)

        btn_cfg = dict(font=("Segoe UI", 8), relief="flat", bd=0,
                       cursor="hand2", padx=8, pady=3)

        # Copy group
        grp1 = tk.Frame(bot_inner, bg=C["bg3"])
        grp1.pack(side="left")
        tk.Label(grp1, text="Copy:", font=("Segoe UI", 7), fg=C["text2"],
                 bg=C["bg3"]).pack(side="left", padx=(0, 2))
        for txt, cmd in [("JSON", self._copy_json), ("CSV", self._copy_csv),
                         ("Text", self._copy_text)]:
            b = tk.Button(grp1, text=txt, command=cmd, bg=C["bg"], fg=C["accent"],
                          activebackground=C["acl"], **btn_cfg)
            b.pack(side="left", padx=1)

        # Separator
        tk.Frame(bot_inner, bg=C["border"], width=1).pack(side="left", fill="y", padx=6, pady=2)

        # Schema group
        grp2 = tk.Frame(bot_inner, bg=C["bg3"])
        grp2.pack(side="left")
        tk.Label(grp2, text="Schema:", font=("Segoe UI", 7), fg=C["text2"],
                 bg=C["bg3"]).pack(side="left", padx=(0, 2))
        for txt, cmd in [("CREATE SQL", self._copy_create_sql), ("Schema", self._copy_schema_text)]:
            b = tk.Button(grp2, text=txt, command=cmd, bg=C["bg"], fg=C["purple"],
                          activebackground=C["bg2"], **btn_cfg)
            b.pack(side="left", padx=1)

        # Separator
        tk.Frame(bot_inner, bg=C["border"], width=1).pack(side="left", fill="y", padx=6, pady=2)

        # Export group
        b = tk.Button(bot_inner, text="Export BLOBs", command=self._export_blobs,
                      bg=C["bg"], fg=C["green"], activebackground=C["gl"], **btn_cfg)
        b.pack(side="left", padx=1)

        # Close — right side
        tk.Button(bot_inner, text="Close", command=self._on_close,
                  bg=C["bg4"], fg=C["text"], activebackground=C["border"],
                  **btn_cfg).pack(side="right", padx=1)

        # Scrollable body
        outer = tk.Frame(self, bg=C["bg"])
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self._body = tk.Frame(canvas, bg=C["bg"])
        self._body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._canvas_win = canvas.create_window((0, 0), window=self._body, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Configure>", lambda e, c=canvas: c.itemconfigure(self._canvas_win, width=e.width))
        self._rw_canvas = canvas
        def _rw_scroll(e):
            try:
                canvas.yview_scroll(int(-3 * (e.delta / 120)), "units")
            except Exception:
                pass
            return "break"
        self._scroll_fn = _rw_scroll

        self._populate()

    def _populate(self):
        data, cols = self._db.full_row(self._tbl, self._rid)
        if not data:
            tk.Label(self._body, text="Row not found", fg=C["red"], bg=C["bg"]).pack(padx=6, pady=6)
            return
        self._row_data = data
        self._row_cols = cols
        for i, col in enumerate(cols):
            val = data.get(col)
            bg = C["bg"] if i % 2 == 0 else C["alt"]
            row_f = tk.Frame(self._body, bg=bg, bd=0)
            row_f.pack(fill="x", padx=0, pady=0)
            row_f.columnconfigure(1, weight=1)

            # Column name - full name, no truncation
            col_lbl = tk.Label(row_f, text=col, font=("Consolas", 8, "bold"),
                     fg=C["accent"], bg=bg, anchor="nw")
            col_lbl.grid(row=0, column=0, sticky="nw", padx=(4, 2), pady=1)

            # Value
            vf = tk.Frame(row_f, bg=bg)
            vf.grid(row=0, column=1, sticky="nsew", padx=(0, 2), pady=1)
            val_widget = None  # Track widget for highlighting

            if val is None:
                tk.Label(vf, text="NULL", fg=C["text2"], bg=bg,
                         font=("Segoe UI", 9, "italic")).pack(side="left")
            elif isinstance(val, bytes):
                bt = blob_type(val)
                tk.Label(vf, text=f"{bt} ({fmtb(len(val))})",
                         fg=C["orange"], bg=bg, font=("Segoe UI", 8, "bold")).pack(side="left")
                tk.Button(vf, text="View", font=("Segoe UI", 7), padx=2, pady=0,
                          command=lambda v=val, c=col: BlobViewer(self, v, c)).pack(side="left", padx=2)
                tk.Button(vf, text="Export", font=("Segoe UI", 7), padx=2, pady=0,
                          command=lambda v=val, c=col: self._export_single(v, c)).pack(side="left", padx=2)
                if is_image(val) and HAS_PIL:
                    try:
                        pimg = PILImage.open(io.BytesIO(val))
                        pimg.thumbnail((80, 80))
                        tkimg = ImageTk.PhotoImage(pimg)
                        self._tk_imgs.append(tkimg)
                        tk.Label(vf, image=tkimg, bg=bg).pack(side="left", padx=4)
                    except Exception:
                        pass
            else:
                sv = str(val)
                is_multiline = '\n' in sv or '\r' in sv
                if is_multiline or len(sv) > 300:
                    # Multi-line or long text: Text widget with scrollbar
                    txt_frame = tk.Frame(vf, bg=bg)
                    txt_frame.pack(fill="x", expand=True)
                    line_count = sv.count('\n') + 1
                    h = min(8, max(2, line_count)) if is_multiline else min(6, max(2, len(sv) // 80))
                    t = tk.Text(txt_frame, height=h, wrap="word",
                                font=("Consolas", 9), bg=bg, relief="groove", bd=1)
                    tsb = ttk.Scrollbar(txt_frame, orient="vertical", command=t.yview)
                    t.configure(yscrollcommand=tsb.set)
                    t.insert("1.0", sv)
                    # Read-only but selectable: block keys except Ctrl+C, Ctrl+A, arrows
                    t.bind("<Key>", lambda e: None if (e.state & 4 and e.keysym.lower() in ('c', 'a')) else "break")
                    tsb.pack(side="right", fill="y")
                    t.pack(side="left", fill="both", expand=True)
                    val_widget = t
                else:
                    # Short single-line text: Entry widget (selectable, copyable, read-only)
                    e = tk.Entry(vf, font=("Segoe UI", 9), bg=bg, fg=C["text"],
                                 relief="flat", bd=0, readonlybackground=bg)
                    e.insert(0, sv)
                    e.configure(state="readonly")
                    e.pack(side="left", fill="x", expand=True)
                    val_widget = e
                if isinstance(val, (int, float)):
                    ts = try_decode_timestamp(val)
                    if ts:
                        for fmt_name, decoded in ts:
                            tk.Label(vf, text=f"{fmt_name}: {decoded}",
                                     fg=C["green"], bg=bg, font=("Segoe UI", 7)).pack(side="left", padx=4)

            # Copy button
            cpb = tk.Button(row_f, text="Copy", font=("Segoe UI", 7, "bold"),
                            fg=C["accent"], bg=C["bg2"], activebackground=C["acl"],
                            relief="flat", bd=0, cursor="hand2", padx=4, pady=1,
                            command=lambda v=val: self._copy_val(v))
            cpb.grid(row=0, column=2, sticky="ne", padx=2, pady=1)

            # Track widget for search highlight
            self._col_widgets[col] = (row_f, col_lbl, val_widget)

        # Bind mousewheel to ALL widgets in this window for smooth scrolling
        def _bind_scroll_all(w):
            w.bind("<MouseWheel>", self._scroll_fn)
            for child in w.winfo_children():
                _bind_scroll_all(child)
        _bind_scroll_all(self)

        # Highlight search match if opened from search results
        if self._search_term and self._match_col:
            self.after(150, self._highlight_match)

    def _highlight_match(self):
        """Scroll to and highlight the matched column/term from search."""
        col = self._match_col
        term = self._search_term
        if not col or not term or col not in self._col_widgets:
            return
        row_f, col_lbl, val_widget = self._col_widgets[col]

        # Highlight column name label with accent background
        col_lbl.configure(bg=C["hl"], fg=C["hbg"])

        # Scroll canvas so matched column row is visible
        self._body.update_idletasks()
        try:
            # Get position of row_f within the canvas scrollregion
            ry = row_f.winfo_y()
            canvas_h = self._rw_canvas.winfo_height()
            scroll_h = self._body.winfo_reqheight()
            if scroll_h > canvas_h:
                # Scroll so row is near top (with small offset)
                frac = max(0.0, min(1.0, (ry - 30) / scroll_h))
                self._rw_canvas.yview_moveto(frac)
        except Exception:
            pass

        # Highlight search term within the value widget
        if val_widget is None:
            return
        if isinstance(val_widget, tk.Text):
            # Tag-based highlight in Text widget
            val_widget.tag_configure("search_hl", background="#ff6b00", foreground="white")
            content = val_widget.get("1.0", "end-1c")
            tl = term.lower()
            cl = content.lower()
            start_idx = 0
            first_pos = None
            while True:
                pos = cl.find(tl, start_idx)
                if pos == -1:
                    break
                # Convert char offset to Text index
                line = content[:pos].count('\n') + 1
                col_off = pos - content[:pos].rfind('\n') - 1
                end_pos = pos + len(term)
                end_line = content[:end_pos].count('\n') + 1
                end_col = end_pos - content[:end_pos].rfind('\n') - 1
                tag_start = f"{line}.{col_off}"
                tag_end = f"{end_line}.{end_col}"
                val_widget.tag_add("search_hl", tag_start, tag_end)
                if first_pos is None:
                    first_pos = tag_start
                start_idx = pos + 1
            # Scroll Text widget to first match
            if first_pos:
                val_widget.see(first_pos)
        elif isinstance(val_widget, tk.Entry):
            # Entry widget: select the matched text
            content = val_widget.get()
            pos = content.lower().find(term.lower())
            if pos >= 0:
                val_widget.configure(state="normal")
                val_widget.selection_range(pos, pos + len(term))
                val_widget.icursor(pos)
                val_widget.xview(pos)
                val_widget.configure(state="readonly")

    def _copy_val(self, v):
        self.clipboard_clear()
        if isinstance(v, bytes):
            self.clipboard_append(binascii.hexlify(v).decode())
        elif v is None:
            self.clipboard_append("NULL")
        else:
            self.clipboard_append(str(v))

    def _copy_create_sql(self):
        sql = self._db.create_sql(self._tbl)
        if sql:
            self.clipboard_clear()
            self.clipboard_append(sql)
            self._flash("Copied CREATE SQL")
        else:
            messagebox.showinfo("Info", "No CREATE SQL found")

    def _copy_schema_text(self):
        self.clipboard_clear()
        self.clipboard_append(_build_schema_text(self._db, self._tbl))
        self._flash("Copied Schema")

    def _export_single(self, data, col):
        bt = blob_type(data)
        ext = _EXT_MAP.get(bt, ".bin")
        path = filedialog.asksaveasfilename(defaultextension=ext,
                                             initialfile=f"{self._tbl}_{col}{ext}")
        if path:
            try:
                with open(path, "wb") as f:
                    f.write(data)
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _flash(self, msg):
        """Show brief confirmation in title bar, then restore."""
        orig = self.title()
        self.title(msg)
        self.after(1500, lambda: self.title(orig))

    def _copy_json(self):
        self.clipboard_clear()
        d = {}
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes):
                d[c] = f"[BLOB {fmtb(len(v))}]"
            else:
                d[c] = v
        self.clipboard_append(json.dumps(d, indent=2, default=str))
        self._flash("Copied JSON")

    def _copy_csv(self):
        self.clipboard_clear()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(self._row_cols)
        vals = []
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes):
                vals.append(f"[BLOB {fmtb(len(v))}]")
            elif v is None:
                vals.append("")
            else:
                vals.append(str(v))
        w.writerow(vals)
        self.clipboard_append(out.getvalue())
        self._flash("Copied CSV")

    def _copy_text(self):
        self.clipboard_clear()
        lines = []
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes):
                lines.append(f"{c}: [BLOB {fmtb(len(v))}]")
            elif v is None:
                lines.append(f"{c}: NULL")
            else:
                lines.append(f"{c}: {v}")
        self.clipboard_append("\n".join(lines))
        self._flash("Copied Text")

    def _export_blobs(self):
        folder = filedialog.askdirectory(title="Select folder for BLOBs")
        if not folder:
            return
        count = 0
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes) and len(v) > 0:
                bt = blob_type(v)
                ext = _EXT_MAP.get(bt, ".bin")
                fname = f"{self._tbl}_r{self._rid}_{c}{ext}"
                try:
                    with open(os.path.join(folder, fname), "wb") as f:
                        f.write(v)
                    count += 1
                except Exception:
                    pass
        if count > 0:
            self._flash(f"Exported {count} BLOB(s)")
        else:
            messagebox.showinfo("Export", "No BLOBs found in this row")

    def _on_close(self):
        key = (self._tbl, self._rid)
        if key in RowWin._pool:
            del RowWin._pool[key]
        self.destroy()
